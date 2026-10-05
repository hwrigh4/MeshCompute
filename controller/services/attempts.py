from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from common.schemas.leases import LeaseRenewal
from controller.services.lease_policy import ACTIVE_ATTEMPTS, controller_now, deadline, timing
from controller.services.retry import retry_or_fail
from common.schemas.attempts import AttemptResult, AttemptStart
from common.schemas.states import AttemptState, JobState
from controller.models.job import Job
from controller.models.job_attempt import JobAttempt
from controller.models.worker import Worker
from controller.models.worker_allocation import WorkerAllocation

TERMINAL = {AttemptState.SUCCEEDED, AttemptState.FAILED, AttemptState.TIMED_OUT, AttemptState.LOST, AttemptState.PREEMPTED}


def locked(session: Session, worker_id: UUID, attempt_id: UUID):
    # Match claim/heartbeat lock order. Serializes result, start, and capacity changes.
    session.execute(select(Worker.id).where(Worker.id == worker_id).with_for_update())
    attempt = session.get(JobAttempt, attempt_id)
    if attempt is None:
        raise HTTPException(404, 'Attempt not found')
    if attempt.worker_id != worker_id:
        raise HTTPException(403, 'Attempt belongs to another worker')
    job = session.scalar(select(Job).where(Job.id == attempt.job_id).with_for_update())
    session.refresh(attempt, with_for_update=True)
    return attempt, job


def start(session, worker_id, attempt_id, payload: AttemptStart):
    attempt, job = locked(session, worker_id, attempt_id)
    now = require_live_lease(session, attempt)
    if attempt.state == AttemptState.RUNNING and attempt.container_engine == payload.container_engine:
        return attempt
    if attempt.state != AttemptState.LEASED or job.state != JobState.RUNNING:
        raise HTTPException(409, 'Attempt cannot start in its current state')
    attempt.state = AttemptState.RUNNING
    attempt.container_engine = payload.container_engine
    attempt.started_at = now
    if job.started_at is None:
        job.started_at = now
    session.commit()
    session.refresh(attempt)
    return attempt


def complete(session, worker_id, attempt_id, payload: AttemptResult):
    attempt, job = locked(session, worker_id, attempt_id)
    values = payload.model_dump()
    if attempt.state in TERMINAL:
        # A duplicate cannot rewrite even the output or reason of an existing result.
        if all(getattr(attempt, key) == value for key, value in values.items()):
            return attempt
        raise HTTPException(409, 'Conflicting terminal result')
    now = require_live_lease(session, attempt)
    if (job.state != JobState.RUNNING
            or attempt.state not in {AttemptState.LEASED, AttemptState.RUNNING}
            or (attempt.state == AttemptState.LEASED and payload.state not in {AttemptState.FAILED, AttemptState.PREEMPTED})
            or (attempt.container_engine is not None and attempt.container_engine != payload.container_engine)):
        raise HTTPException(409, 'Result incompatible with attempt state or engine')
    allocation = session.get(WorkerAllocation, attempt.id)
    if allocation is None:
        raise HTTPException(409, 'Active attempt has no allocation; inspect controller state')
    for key, value in values.items():
        setattr(attempt, key, value)
    attempt.completed_at = now
    if payload.state == AttemptState.PREEMPTED:
        retry_or_fail(session, job, now)
    else:
        job.state = JobState.SUCCEEDED if payload.state == AttemptState.SUCCEEDED else JobState.FAILED
        job.completed_at = now
    session.delete(allocation)
    session.commit()  # Result, job, and accounting are one transaction.
    session.refresh(attempt)
    return attempt


def require_live_lease(session, attempt):
    now = controller_now(session)
    if (attempt.state not in ACTIVE_ATTEMPTS or attempt.lease_expires_at is None
            or now >= attempt.lease_expires_at):
        raise HTTPException(409, 'Attempt lease is expired or no longer active')
    return now


def renew(session, worker_id, attempt_id):
    attempt, job = locked(session, worker_id, attempt_id)
    now = require_live_lease(session, attempt)
    if job.state != JobState.RUNNING:
        raise HTTPException(409, 'Job is no longer running')
    attempt.lease_expires_at = deadline(now)
    result = LeaseRenewal(attempt_id=attempt.id, **timing(attempt.lease_expires_at))
    session.commit()
    return result
