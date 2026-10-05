"""Worker-scoped comparison, with the same locking and retry rules as recovery."""
from fastapi import HTTPException
from sqlalchemy import select

from common.schemas.reconciliation import MAX_ATTEMPTS, ReconcileAttempt, ReconcileView
from common.schemas.states import AttemptState, JobState
from controller.models.job_attempt import JobAttempt
from controller.models.worker_allocation import WorkerAllocation
from controller.services.attempts import locked, require_live_lease
from controller.services.lease_policy import ACTIVE_ATTEMPTS, controller_now
from controller.services.retry import retry_or_fail


def snapshot(session, worker_id, ids):
    allocated = select(WorkerAllocation.job_attempt_id).where(WorkerAllocation.worker_id == worker_id)
    rows = list(session.scalars(select(JobAttempt).where(
        JobAttempt.worker_id == worker_id,
        (JobAttempt.id.in_(ids)) | (JobAttempt.state.in_(ACTIVE_ATTEMPTS) & JobAttempt.id.in_(allocated)),
    ).limit(MAX_ATTEMPTS * 2 + 1)))
    if len(rows) > MAX_ATTEMPTS * 2:
        raise HTTPException(409, 'Too many attempts for bounded reconciliation; inspect worker state')
    allocation_ids = set(session.scalars(allocated.where(WorkerAllocation.job_attempt_id.in_([a.id for a in rows]))))
    now = controller_now(session)
    return ReconcileView(attempts=[ReconcileAttempt(
        id=a.id, job_id=a.job_id, state=a.state, lease_expires_at=a.lease_expires_at,
        container_engine=a.container_engine,
        active=(a.state in ACTIVE_ATTEMPTS and a.id in allocation_ids
                and a.lease_expires_at is not None and a.lease_expires_at > now),
    ) for a in rows], unknown=list(set(ids) - {a.id for a in rows}))


def missing(session, worker_id, attempt_id, payload):
    attempt, job = locked(session, worker_id, attempt_id)
    now = require_live_lease(session, attempt)
    # A stale scan cannot defeat a concurrent renewal/start/result. LEASED work
    # may still be preparing its image and is never inferred missing.
    allocation = session.get(WorkerAllocation, attempt.id)
    if (attempt.state != AttemptState.RUNNING or job.state != JobState.RUNNING
            or allocation is None or attempt.job_id != payload.job_id
            or attempt.container_engine != payload.container_engine
            or attempt.lease_expires_at != payload.lease_expires_at):
        raise HTTPException(409, 'Reconciliation snapshot changed; inspect again')
    attempt.state = AttemptState.LOST
    attempt.failure_reason = 'WORKLOAD_MISSING'
    attempt.completed_at = now
    session.delete(allocation)
    retry_or_fail(session, job, now)
    session.commit()
    session.refresh(attempt)
    return attempt
