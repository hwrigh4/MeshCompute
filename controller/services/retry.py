"""Retry decision shared by LOST recovery and assigned-worker preemption."""
from sqlalchemy import func, select

from common.schemas.states import JobState
from controller.models.job_attempt import JobAttempt


def retry_or_fail(session, job, now):
    # Caller holds the job lock; the normal claim path creates the next attempt.
    used = session.scalar(select(func.max(JobAttempt.attempt_number)).where(JobAttempt.job_id == job.id))
    if used < job.max_attempts:
        job.state, job.completed_at = JobState.QUEUED, None
    else:
        job.state, job.completed_at = JobState.FAILED, now
