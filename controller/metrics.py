"""Committed events plus bounded SQL aggregates for current state."""
import logging
from time import monotonic
from types import SimpleNamespace

from fastapi import APIRouter, Response
from prometheus_client import CollectorRegistry, CONTENT_TYPE_LATEST, generate_latest
from prometheus_client.core import GaugeMetricFamily
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from common.logging import attempt_context, event
from common.metrics import counter, histogram
from controller.database import get_engine

registry = CollectorRegistry()
logger = logging.getLogger(__name__)
submitted = counter(registry, 'jobs_submitted_total', 'Committed job submissions.')
completed = counter(registry, 'jobs_completed_total', 'Committed terminal job transitions.', ['state'])
queue_time = histogram(registry, 'job_queue_duration_seconds', 'Creation to first committed assignment; observed once per job.')
total_time = histogram(registry, 'job_total_duration_seconds', 'Creation to terminal job transition.', ['state'])
attempts = counter(registry, 'attempts_total', 'Committed terminal attempt outcomes.', ['state', 'engine'])
started = counter(registry, 'attempts_started_total', 'Committed first attempt starts.', ['engine'])
runtime = histogram(registry, 'attempt_runtime_seconds', 'Confirmed start to terminal recording, including cleanup/report latency.', ['state', 'engine'])
claims = counter(registry, 'claim_requests_total', 'Claim HTTP requests, including rejected authentication.')
assignments = counter(registry, 'claim_assignments_total', 'Committed assignments.')
no_work = counter(registry, 'claim_no_work_total', 'Authenticated claims returning no assignment.', ['reason'])
claim_time = histogram(registry, 'claim_duration_seconds', 'Claim HTTP duration.')
renewals = counter(registry, 'lease_renewals_total', 'Committed lease renewals.')
renewal_failures = counter(registry, 'lease_renewal_failures_total', 'Rejected or failed renewal HTTP requests.', ['reason'])
lost = counter(registry, 'attempts_lost_total', 'Controller-generated LOST transitions.', ['reason'])
recovery_runs = counter(registry, 'recovery_runs_total', 'Recovery scans attempted.')
recovery_failures = counter(registry, 'recovery_failures_total', 'Recovery scan/transaction failures.')
recovery_attempts = counter(registry, 'recovery_attempts_total', 'Lease-expired attempts recovered.', ['outcome'])
invariants = counter(registry, 'invariant_issues_total', 'Detected reservation invariant issues.', ['operation'])
preemptions = counter(registry, 'preemptions_total', 'Committed provider preemptions.', ['outcome'])
reconciliation_runs = counter(registry, 'reconciliation_runs_total', 'Authenticated or rejected snapshot HTTP requests.')
reconciliation_failures = counter(registry, 'reconciliation_failures_total', 'Failed snapshot HTTP requests.')
reconciliation_time = histogram(registry, 'reconciliation_duration_seconds', 'Controller snapshot HTTP duration.')
missing = counter(registry, 'reconciliation_missing_workloads_total', 'Committed missing-workload LOST transitions.', ['outcome'])
executions = counter(registry, 'container_executions_total', 'Recorded container exits; not an engine launch count.', ['engine', 'state'])
execution_time = histogram(registry, 'container_execution_seconds', 'Start-report to recorded container exit result; includes reporting/cleanup.', ['engine', 'state'])


def terminal_snapshot(attempt, job):
    # Capture under the existing row locks. ORM instances expire on commit;
    # reading them afterward could observe a concurrent retry's newer job state.
    return (SimpleNamespace(**{key: getattr(attempt, key) for key in (
        'id', 'worker_id', 'job_id', 'attempt_number', 'container_engine', 'state',
        'failure_reason', 'started_at', 'completed_at', 'exit_code')}),
        SimpleNamespace(**{key: getattr(job, key) for key in (
            'id', 'state', 'created_at', 'completed_at')}))


def job_terminal(job):
    if job.state in ('SUCCEEDED', 'FAILED', 'CANCELLED'):
        completed.labels(job.state).inc()
        total_time.labels(job.state).observe(max(0, (job.completed_at - job.created_at).total_seconds()))
        event(logger, 'job.completed', job_id=job.id, state=job.state)


def attempt_terminal(attempt, job):
    engine = attempt.container_engine if attempt.container_engine in ('podman', 'docker') else 'none'
    attempts.labels(attempt.state, engine).inc()
    duration = None
    if attempt.started_at is not None:
        duration = max(0, (attempt.completed_at - attempt.started_at).total_seconds())
        runtime.labels(attempt.state, engine).observe(duration)
    outcome = 'requeued' if job.state == 'QUEUED' else 'exhausted'
    if attempt.state == 'PREEMPTED':
        preemptions.labels(outcome).inc()
    if attempt.state == 'LOST':
        reason = 'WORKLOAD_MISSING' if attempt.failure_reason == 'WORKLOAD_MISSING' else 'LEASE_EXPIRED'
        lost.labels(reason).inc()
    if attempt.exit_code is not None and engine != 'none' and duration is not None:
        executions.labels(engine, attempt.state).inc()
        execution_time.labels(engine, attempt.state).observe(duration)
    event(logger, 'attempt.terminal', **attempt_context(attempt))
    job_terminal(job)


class DatabaseState:
    def describe(self):
        return []  # Never open a DB connection during module import/registration.

    def collect(self):
        # Fixed number of aggregate queries and bounded result sets. PostgreSQL
        # still performs work proportional to table/index size; cap statement time.
        with get_engine().connect().execution_options(isolation_level='REPEATABLE READ') as db:
            with db.begin():
                db.execute(text("SET LOCAL statement_timeout = '2s'"))
                jobs = dict(db.execute(text('SELECT state, count(*) FROM jobs GROUP BY state')).all())
                workers = db.execute(text('''
                    WITH reserved AS (
                        SELECT worker_id, sum(cpu_reserved) cpu, sum(memory_reserved_mb) memory
                        FROM worker_allocations GROUP BY worker_id
                    ), current AS (
                        SELECT CASE
                            WHEN last_heartbeat IS NULL THEN 'REGISTERING'
                            WHEN last_heartbeat < transaction_timestamp()-interval '30 seconds' THEN 'OFFLINE'
                            WHEN last_heartbeat <= transaction_timestamp()-interval '15 seconds' THEN 'DEGRADED'
                            WHEN state IN ('PAUSED','DRAINING') THEN state
                            WHEN state='DEGRADED' OR NOT EXISTS (SELECT 1 FROM jsonb_each_text(executors::jsonb) e WHERE e.value='true') THEN 'DEGRADED'
                            ELSE 'HEALTHY' END effective_state,
                        greatest(0, extract(epoch FROM transaction_timestamp()-last_heartbeat)) age,
                        coalesce(cpu_contributed,0) cpu, coalesce(memory_contributed_mb,0)*1048576::bigint memory,
                        coalesce(r.cpu,0) reserved_cpu, coalesce(r.memory,0)*1048576::bigint reserved_memory
                        FROM workers w LEFT JOIN reserved r ON r.worker_id=w.id
                    ) SELECT effective_state, count(*), max(age), sum(cpu), sum(reserved_cpu),
                        sum(greatest(0,cpu-reserved_cpu)), sum(memory), sum(reserved_memory),
                        sum(greatest(0,memory-reserved_memory))
                      FROM current GROUP BY effective_state
                ''')).all()
        g = GaugeMetricFamily('meshcompute_jobs', 'Current jobs by persisted state.', labels=['state'])
        for state in ('QUEUED', 'RUNNING', 'SUCCEEDED', 'FAILED', 'CANCELLED'):
            g.add_metric([state], jobs.get(state, 0))
        yield g
        families = [GaugeMetricFamily('meshcompute_' + name, help, labels=['state']) for name, help in (
            ('workers', 'Current workers by effective state, including heartbeat staleness.'),
            ('worker_heartbeat_age_seconds', 'Maximum heartbeat age in each effective-state group; zero without a heartbeat.'),
            ('worker_cpu_contributed', 'Total contributed logical CPUs by worker effective state.'),
            ('worker_cpu_reserved', 'Ledger-reserved logical CPUs by worker effective state.'),
            ('worker_cpu_allocatable', 'Sum of per-worker unreserved CPU, floored at zero; not scheduler eligibility.'),
            ('worker_memory_contributed_bytes', 'Total contributed memory bytes by worker effective state.'),
            ('worker_memory_reserved_bytes', 'Ledger-reserved memory bytes by worker effective state.'),
            ('worker_memory_allocatable_bytes', 'Sum of per-worker unreserved bytes, floored at zero; not eligibility.'),
        )]
        values = {row[0]: row[1:] for row in workers}
        for state in ('REGISTERING', 'HEALTHY', 'DEGRADED', 'PAUSED', 'DRAINING', 'OFFLINE'):
            for family, value in zip(families, values.get(state, [0] * len(families))):
                family.add_metric([state], value or 0)
        yield from families


registry.register(DatabaseState())
router = APIRouter()


@router.get('/metrics', include_in_schema=False)
def metrics():
    try:
        return Response(generate_latest(registry), headers={'Content-Type': CONTENT_TYPE_LATEST})
    except SQLAlchemyError:
        event(logger, 'metrics.database_unavailable', level=logging.WARNING)
        return Response('Metrics temporarily unavailable\n', status_code=503, media_type='text/plain')


class RequestMetrics:
    """Measure only fixed route templates; never raw URLs, headers or bodies."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        begin, status = monotonic(), 500
        async def response(message):
            nonlocal status
            if message['type'] == 'http.response.start':
                status = message['status']
            await send(message)
        try:
            await self.app(scope, receive, response)
        finally:
            route = getattr(scope.get('route'), 'path', '')
            if scope['method'] == 'POST':
                if route == '/v1/workers/{worker_id}/claim':
                    claims.inc()
                    claim_time.observe(monotonic() - begin)
                elif route == '/v1/workers/{worker_id}/attempts/{attempt_id}/renew' and status >= 400:
                    reason = 'auth' if status in (401, 403) else 'conflict' if status == 409 else 'server' if status >= 500 else 'invalid'
                    renewal_failures.labels(reason).inc()
                elif route == '/v1/workers/{worker_id}/reconcile':
                    reconciliation_runs.inc()
                    reconciliation_time.observe(monotonic() - begin)
                    if status >= 400:
                        reconciliation_failures.inc()
