"""Focused Phase 9 checks; reuses the owned-controller/real-engine lab, no reset."""
import argparse
import asyncio
from contextlib import ExitStack
import json
import logging
import os
import socket
import subprocess
import sys
from uuid import uuid4

import httpx
from prometheus_client import generate_latest
from prometheus_client.parser import text_string_to_metric_families
from sqlalchemy.exc import SQLAlchemyError

from common.logging import EventFormatter
from local_test.lab import Lab, LabError, STATE_DIR
from local_test.real_worker import acquire, lock_file
from local_test.recovery import RecoveryLab, stop, until
from local_test.reconciliation import claimed, container, exists
from local_test.provider import environment, control, running
from worker import metrics as worker_metrics
from worker.agent.reconciliation import reconcile
from worker.config import WorkerSettings
from worker.executors.container import ContainerExecutor
from worker.executors.compatible import ExecutionError


def samples(body, secrets=()):
    for secret in secrets:
        assert str(secret) not in body, 'Sensitive/high-cardinality value in metrics'
    result = {}
    allowed = {'state', 'engine', 'reason', 'outcome', 'operation', 'le'}
    domains = {
        'state': {'REGISTERING', 'HEALTHY', 'DEGRADED', 'PAUSED', 'DRAINING', 'OFFLINE',
                  'QUEUED', 'RUNNING', 'SUCCEEDED', 'FAILED', 'CANCELLED', 'TIMED_OUT', 'LOST', 'PREEMPTED'},
        'engine': {'podman', 'docker', 'none'},
        'reason': {'worker_ineligible', 'insufficient_capacity', 'no_fitting_job', 'attempt_limit',
                   'auth', 'conflict', 'server', 'invalid', 'WORKLOAD_MISSING', 'LEASE_EXPIRED'},
        'outcome': {'requeued', 'exhausted'}, 'operation': {'recovery', 'result'},
    }
    for family in text_string_to_metric_families(body):
        for sample in family.samples:
            assert sample.labels.keys() <= allowed
            for key, value in sample.labels.items():
                if key != 'le':
                    assert value in domains[key], 'Unbounded metric label'
            result[sample.name, tuple(sorted(sample.labels.items()))] = sample.value
    return result


def value(data, name, **labels):
    return data.get(('meshcompute_' + name, tuple(sorted(labels.items()))), 0)


def scrape(h, secrets=()):
    r = h.lab.client.get('/metrics')
    assert r.status_code == 200, 'Metrics scrape failed'
    assert 'text/plain' in r.headers['content-type']
    return samples(r.text, secrets)


def check_capacity(h):
    data = scrape(h)
    rows = h.lab.snapshot()['workers']
    def total(name):
        return sum(v for (key, _), v in data.items() if key == 'meshcompute_' + name)
    assert total('workers') == len(rows), 'Worker count differs from database'
    for metric, field, multiplier in (
        ('worker_cpu_contributed', 'cpu_contributed', 1),
        ('worker_cpu_reserved', 'cpu_reserved', 1),
        ('worker_cpu_allocatable', 'cpu_allocatable', 1),
        ('worker_memory_contributed_bytes', 'memory_contributed_mb', 1048576),
        ('worker_memory_reserved_bytes', 'memory_reserved_mb', 1048576),
        ('worker_memory_allocatable_bytes', 'memory_allocatable_mb', 1048576),
    ):
        expected = sum(float(w['resources'][field] or 0) * multiplier for w in rows)
        assert abs(total(metric) - expected) < .00001, metric + ' differs from allocation ledger'


def run(h):
    if h.api.get('/images/localhost/meshcompute-failure:dev/json').status_code != 200:
        raise LabError(f'Build required images explicitly: make examples-build-{h.actual}')
    w = h.worker('observability')
    forbidden = [w['id'], w['token'], 'localhost/meshcompute-success:dev']
    base = scrape(h, forbidden)
    assert h.lab.client.get('/health').status_code == 200
    job = h.submit('metrics-success')
    forbidden.append(job['id'])
    queued = scrape(h, forbidden)
    assert value(queued, 'jobs_submitted_total') == value(base, 'jobs_submitted_total') + 1
    assert value(queued, 'jobs', state='QUEUED') == value(base, 'jobs', state='QUEUED') + 1
    a = h.finish(w, job)
    forbidden.append(a['id'])
    done = scrape(h, forbidden)
    assert value(done, 'claim_assignments_total') == value(base, 'claim_assignments_total') + 1
    assert value(done, 'claim_requests_total') > value(base, 'claim_requests_total')
    for name in ('attempts_total', 'container_executions_total'):
        assert value(done, name, engine=h.actual, state='SUCCEEDED') == value(base, name, engine=h.actual, state='SUCCEEDED') + 1
    assert value(done, 'job_queue_duration_seconds_count') == value(base, 'job_queue_duration_seconds_count') + 1
    assert value(done, 'attempts_started_total', engine=h.actual) == value(base, 'attempts_started_total', engine=h.actual) + 1
    # Real authenticated duplicate report: no transition and no metric increment.
    view = h.lab.request('GET', '/v1/attempts/' + a['id']).json()
    keys = ('state', 'container_engine', 'exit_code', 'failure_reason', 'stdout_tail', 'stderr_tail', 'stdout_truncated', 'stderr_truncated')
    assert h.post(w, a, 'result', {key: view[key] for key in keys}).status_code == 200
    assert value(scrape(h), 'attempts_total', engine=h.actual, state='SUCCEEDED') == value(done, 'attempts_total', engine=h.actual, state='SUCCEEDED')
    print('PASS Prometheus parsing, persisted gauges, real execution metrics, idempotent counters, bounded labels', flush=True)

    # Real timeout through work-once; expose optional worker target while alive.
    job = h.submit('metrics-timeout', seconds=20, maximum=1)
    # Test-only fixture sets a small timeout before claim (not an execution result).
    from sqlalchemy import text
    with h.lab.engine.begin() as db:
        db.execute(text('UPDATE jobs SET timeout_seconds=4 WHERE id=:id'), {'id': job['id']})
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        port = s.getsockname()[1]
    env = environment(h, w, credentials=True) | {'MESHCOMPUTE_WORKER_METRICS_PORT': str(port)}
    p = subprocess.Popen([sys.executable, '-m', 'worker.main', 'work-once'], env=env)
    h.children.append(p)
    running(h, job)
    with httpx.Client(trust_env=False, timeout=2) as client:
        local = until(lambda: samples(client.get(f'http://127.0.0.1:{port}/metrics').text, forbidden), 'Worker scrape missing')
    assert value(local, 'container_executions_total', engine=h.actual) == 1
    assert p.wait(timeout=30) == 0
    assert h.attempt(job)['state'] == 'TIMED_OUT'
    assert value(scrape(h), 'attempts_total', engine=h.actual, state='TIMED_OUT') >= 1
    print('PASS real timeout outcome and optional loopback worker scrape', flush=True)

    job = h.lab.request('POST', '/v1/jobs', json={
        'name': 'observability-nonzero', 'runtime': 'container',
        'image': 'localhost/meshcompute-failure:dev', 'command': ['python', '/app/main.py'],
        'resources': {'cpu': .5, 'memory_mb': 128}, 'timeout_seconds': 20, 'max_attempts': 1,
    }).json()
    p = h.spawn(w, execute=True)
    assert p.wait(timeout=35) == 0
    assert h.attempt(job)['exit_code'] == 7
    assert value(scrape(h), 'attempts_total', engine=h.actual, state='FAILED') >= 1
    print('PASS real nonzero execution bounded FAILED outcome', flush=True)

    # Diagnostic claim + DB backdating fixture; recovery runs in real controller.
    job, assignment, a = claimed(h, w, 'metrics-lost', maximum=1)
    before = scrape(h)
    check_capacity(h)
    h.backdate(a)
    h.lost(job, state='FAILED')
    assert h.post(w, a, 'renew').status_code == 409
    after = scrape(h)
    check_capacity(h)
    assert value(after, 'attempts_lost_total', reason='LEASE_EXPIRED') == value(before, 'attempts_lost_total', reason='LEASE_EXPIRED') + 1
    assert value(after, 'recovery_attempts_total', outcome='exhausted') == value(before, 'recovery_attempts_total', outcome='exhausted') + 1
    assert value(after, 'lease_renewal_failures_total', reason='conflict') >= 1

    job = h.submit('metrics-preemption', seconds=40, maximum=1)
    p = h.spawn(w, execute=True)
    running(h, job)
    control(h, w, 'stop-all')
    assert p.wait(timeout=30) == 0
    assert h.attempt(job)['state'] == 'PREEMPTED'
    assert value(scrape(h), 'preemptions_total', outcome='exhausted') >= 1
    control(h, w, 'resume')
    print('PASS recovery/LOST and real provider preemption metrics', flush=True)

    # Real engine fixtures, production reconciliation in this process so its
    # process-local counters remain inspectable after the pass returns.
    settings = WorkerSettings(id=w['id'], token=w['token'], controller_url=h.lab.url, container_engine=h.engine_name)
    job, assignment, a = claimed(h, w, 'metrics-missing', maximum=1)
    assert h.post(w, a, 'start', {'container_engine': h.actual}).status_code == 200
    asyncio.run(reconcile(settings))
    assert h.attempt(job)['failure_reason'] == 'WORKLOAD_MISSING'
    assert value(scrape(h), 'reconciliation_missing_workloads_total', outcome='exhausted') >= 1
    assignment['attempt_id'], assignment['job_id'] = str(uuid4()), str(uuid4())
    container(h, w, assignment)
    asyncio.run(reconcile(settings))
    assert not exists(h, {'id': assignment['attempt_id']})
    local = samples(generate_latest(worker_metrics.registry).decode(), forbidden)
    assert value(local, 'reconciliation_orphans_removed_total', engine=h.actual) == 1
    asyncio.run(reconcile(settings))
    assert value(samples(generate_latest(worker_metrics.registry).decode()), 'reconciliation_orphans_removed_total', engine=h.actual) == 1

    async def failed_pull():
        backend = await ContainerExecutor(settings.podman_socket, settings.docker_socket, h.engine_name).select()
        try:
            try:
                await backend.ensure_image('127.0.0.1:1/meshcompute-missing:observability')
            except ExecutionError as exc:
                assert exc.reason == 'IMAGE_PULL_FAILED'
            else:
                raise AssertionError('Expected local closed-port image pull failure')
        finally:
            await backend.client.aclose()
    asyncio.run(failed_pull())
    local = samples(generate_latest(worker_metrics.registry).decode(), forbidden)
    assert value(local, 'image_pull_total', engine=h.actual) == 1
    assert value(local, 'image_pull_failures_total', engine=h.actual) == 1
    print('PASS missing/orphan reconciliation, cleanup idempotency and real image-pull failure metrics', flush=True)

    # Counter reset vs persisted truth after restart.
    before = scrape(h)
    h.stop_controller()
    h.start_controller()
    after = scrape(h, forbidden)
    for state in ('QUEUED', 'RUNNING', 'SUCCEEDED', 'FAILED', 'CANCELLED'):
        assert value(after, 'jobs', state=state) == value(before, 'jobs', state=state)
    assert value(after, 'jobs_submitted_total') == 0
    record = logging.LogRecord('worker.test', logging.INFO, '', 0, 'attempt.test', (), None)
    record.worker_id, record.job_id, record.attempt_id = w['id'], job['id'], a['id']
    record.token = w['token']  # Formatter must drop fields outside its allowlist.
    previous = os.environ.get('MESHCOMPUTE_LOG_FORMAT')
    try:
        os.environ['MESHCOMPUTE_LOG_FORMAT'] = 'json'
        output = EventFormatter().format(record)
        assert json.loads(output)['attempt_id'] == a['id'] and w['token'] not in output
    finally:
        if previous is None:
            os.environ.pop('MESHCOMPUTE_LOG_FORMAT', None)
        else:
            os.environ['MESHCOMPUTE_LOG_FORMAT'] = previous
    print('PASS restart-safe gauges, process counter reset, structured log context/allowlist', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--engine', choices=['auto', 'podman', 'docker'], default=os.environ.get('MESHCOMPUTE_WORKER_CONTAINER_ENGINE', 'auto'))
    args = parser.parse_args()
    lab = None
    try:
        with ExitStack() as stack:
            for name in ('real-worker-test.lock', 'real-worker.lock'):
                if not acquire(stack.enter_context(lock_file(name))):
                    raise LabError('Stop helper agents/tests before test-observability')
            lab = Lab(state_file=STATE_DIR / 'observability-workers.json')
            h = RecoveryLab(lab, args.engine)
            try:
                h.start_controller()
                run(h)
            finally:
                h.close()
    except (LabError, AssertionError) as exc:
        raise SystemExit(f'FAIL {exc}; results retained, inspect make dev-state') from None
    except (SQLAlchemyError, httpx.HTTPError, OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        raise SystemExit(f'Observability checks failed ({type(exc).__name__}); raw response omitted') from None
    except KeyboardInterrupt:
        raise SystemExit('Interrupted; owned children stopped; inspect retained state') from None
    finally:
        if lab:
            lab.close()


if __name__ == '__main__':
    main()
