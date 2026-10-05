from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from common.schemas.assignments import WorkAssignment
from common.schemas.jobs import JobResources
from common.schemas.states import AttemptState, JobState, WorkerState
from controller.models.job import Job
from controller.models.job_attempt import JobAttempt
from controller.models.worker import Worker
from controller.models.worker_allocation import WorkerAllocation
from controller.services.lease_policy import controller_now, deadline, timing
from controller.services.worker_health import effective_worker_state


def claim_job(session: Session, worker_id: UUID) -> WorkAssignment | None:
    """Claim the oldest fitting job. The worker lock serializes its reservations."""
    worker = session.scalar(
        select(Worker).where(Worker.id == worker_id).with_for_update()
        .execution_options(populate_existing=True)
    )
    # Authentication may have loaded this identity before waiting for the lock.
    # populate_existing ensures current contribution, participation and capabilities.
    if worker is None or effective_worker_state(
        worker.last_heartbeat, worker.state, worker.executors, datetime.now(timezone.utc),
    ) != WorkerState.HEALTHY:
        return None

    allocations = session.scalars(select(WorkerAllocation).where(WorkerAllocation.worker_id == worker_id))
    reserved_cpu = Decimal(0)
    reserved_memory = 0
    for allocation in allocations:
        # Decimal arithmetic avoids 0.3 - 0.1 rounding below a 0.2 CPU request.
        reserved_cpu += allocation.cpu_reserved
        reserved_memory += allocation.memory_reserved_mb
    available_cpu = Decimal(str(worker.cpu_contributed)) - reserved_cpu
    available_memory = worker.memory_contributed_mb - reserved_memory
    if available_cpu <= 0 or available_memory <= 0:
        return None

    runtimes = [runtime for runtime, usable in worker.executors.items() if usable]
    job = session.scalar(
        select(Job).where(
            Job.state == JobState.QUEUED,
            Job.runtime.in_(runtimes),
            Job.cpu_requested <= float(available_cpu),
            Job.memory_requested_mb <= available_memory,
        ).order_by(Job.created_at, Job.id).limit(1).with_for_update(skip_locked=True)
    )
    if job is None:
        return None
    if Decimal(str(job.cpu_requested)) > available_cpu:
        # Guard the boundary where conversion to float for SQL rounded upward.
        return None
    attempt_number = session.scalar(
        select(func.coalesce(func.max(JobAttempt.attempt_number), 0))
        .where(JobAttempt.job_id == job.id)
    ) + 1
    if attempt_number > job.max_attempts:
        # Repair inconsistent queued state without creating excess attempts.
        job.state = JobState.FAILED
        job.completed_at = controller_now(session)
        session.commit()
        return None
    expires_at = deadline(controller_now(session))
    attempt = JobAttempt(
        job_id=job.id, worker_id=worker.id, attempt_number=attempt_number,
        state=AttemptState.LEASED, lease_expires_at=expires_at,
    )
    session.add(attempt)
    session.flush()
    session.add(WorkerAllocation(
        job_attempt_id=attempt.id, worker_id=worker.id,
        cpu_reserved=Decimal(str(job.cpu_requested)), memory_reserved_mb=job.memory_requested_mb,
    ))
    job.state = JobState.RUNNING
    # Job RUNNING means assigned; the attempt begins with a real ownership lease.
    assignment = WorkAssignment(
        attempt_id=attempt.id, job_id=job.id, runtime=job.runtime,
        image=job.image, command=job.command,
        resources=JobResources(cpu=job.cpu_requested, memory_mb=job.memory_requested_mb),
        timeout_seconds=job.timeout_seconds, **timing(expires_at),
    )
    session.commit()
    return assignment
