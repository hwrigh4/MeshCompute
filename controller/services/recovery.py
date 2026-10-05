"""Persisted lease recovery; safe with multiple controller processes."""
import asyncio
from contextlib import suppress
import logging
import threading

from fastapi import HTTPException
from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from common.schemas.states import AttemptState, JobState
from controller.database import get_engine
from controller.models.job_attempt import JobAttempt
from controller.models.worker_allocation import WorkerAllocation
from controller.services.attempts import locked
from controller.services.lease_policy import ACTIVE_ATTEMPTS, RECOVERY_BATCH_SIZE, RECOVERY_INTERVAL_SECONDS, controller_now

logger = logging.getLogger(__name__)


def recover_one(engine, worker_id, attempt_id):
    with Session(engine) as session:
        # Bound contention and shutdown latency; a skipped scan retries next time.
        session.execute(text("SET LOCAL lock_timeout = '1s'"))
        session.execute(text("SET LOCAL statement_timeout = '5s'"))
        try:
            attempt, job = locked(session, worker_id, attempt_id)
        except HTTPException as exc:
            if exc.status_code == 404:  # A local lab reset may remove a candidate.
                return False
            raise
        now = controller_now(session)  # Recheck AFTER worker -> job -> attempt locks.
        if (attempt.state not in ACTIVE_ATTEMPTS or attempt.lease_expires_at is None
                or attempt.lease_expires_at > now):
            return False
        attempt.state = AttemptState.LOST
        attempt.failure_reason = 'LEASE_EXPIRED'
        attempt.completed_at = now
        allocation = session.get(WorkerAllocation, attempt.id)
        if allocation is not None:
            session.delete(allocation)
        if job.state == JobState.RUNNING:
            used = session.scalar(select(func.max(JobAttempt.attempt_number)).where(JobAttempt.job_id == job.id))
            if used < job.max_attempts:
                job.state, job.completed_at = JobState.QUEUED, None
            else:
                job.state, job.completed_at = JobState.FAILED, now
        session.commit()  # LOST, job retry decision, and ledger release are atomic.
        return True


def recover_expired(stop=None):
    engine = get_engine()
    with Session(engine) as session:
        session.execute(text("SET LOCAL statement_timeout = '5s'"))
        candidates = list(session.execute(select(JobAttempt.worker_id, JobAttempt.id).where(
            JobAttempt.state.in_(ACTIVE_ATTEMPTS), JobAttempt.lease_expires_at <= func.clock_timestamp(),
        ).order_by(JobAttempt.lease_expires_at, JobAttempt.id).limit(RECOVERY_BATCH_SIZE)))
    recovered = 0
    for worker_id, attempt_id in candidates:
        if stop is not None and stop.is_set():
            break
        try:
            recovered += recover_one(engine, worker_id, attempt_id)
        except SQLAlchemyError:
            logger.warning('Lease recovery transaction unavailable; will retry on a later scan')
    return recovered


async def recovery_loop():
    stop = threading.Event()
    while True:
        scan = asyncio.create_task(asyncio.to_thread(recover_expired, stop))
        try:
            count = await asyncio.shield(scan)
            if count:
                logger.info('Recovered %s expired attempt(s)', count)
        except asyncio.CancelledError:
            stop.set()
            with suppress(SQLAlchemyError):
                await scan  # Do not leave a DB thread mutating after shutdown.
            raise
        except SQLAlchemyError:
            logger.warning('Lease recovery scan unavailable; will retry')
        await asyncio.sleep(RECOVERY_INTERVAL_SECONDS)
