from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from common.schemas.jobs import JobSubmission
from common.schemas.states import JobState
from controller.models.job import Job
from controller import metrics
from common.logging import event


class JobNotFound(Exception):
    pass


class JobCancellationConflict(Exception):
    pass


def create_job(session: Session, submission: JobSubmission) -> Job:
    job = Job(
        **submission.model_dump(exclude={"resources"}),
        cpu_requested=submission.resources.cpu,
        memory_requested_mb=submission.resources.memory_mb,
        state=JobState.QUEUED,
    )
    session.add(job)
    session.commit()
    session.refresh(job)
    metrics.submitted.inc()
    event(metrics.logger, "job.submitted", job_id=job.id, state=JobState.QUEUED)
    return job


def cancel_job(session: Session, job_id: UUID) -> Job:
    # Serialize cancellation so repeated/concurrent requests preserve timestamps.
    job = session.scalar(select(Job).where(Job.id == job_id).with_for_update())
    if job is None:
        raise JobNotFound
    if job.state == JobState.CANCELLED:
        return job
    if job.state != JobState.QUEUED:
        raise JobCancellationConflict(f"Cannot cancel a job in state {job.state}")
    job.state = JobState.CANCELLED
    job.completed_at = datetime.now(timezone.utc)
    session.commit()
    session.refresh(job)
    metrics.job_terminal(job)
    return job
