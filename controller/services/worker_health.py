from datetime import datetime
from collections.abc import Mapping

from common.schemas.states import WorkerState
from common.schemas.workers import WorkerStatus
from controller.models.worker import Worker


def effective_worker_state(
    last_heartbeat: datetime | None,
    reported_state: WorkerState,
    executors: Mapping[str, bool],
    now: datetime,
) -> WorkerState:
    """Shared read-time health policy for APIs and future scheduling.

    Persisted state alone is not an eligibility check. Call with the current UTC
    time; stale and participation states must never be treated as HEALTHY.
    """
    if last_heartbeat is None:
        return WorkerState.REGISTERING
    age = (now - last_heartbeat).total_seconds()
    if age > 30:
        return WorkerState.OFFLINE
    if age >= 15:
        return WorkerState.DEGRADED
    if reported_state in (WorkerState.PAUSED, WorkerState.DRAINING):
        return reported_state
    if not any(executors.values()) or reported_state == WorkerState.DEGRADED:
        return WorkerState.DEGRADED
    return WorkerState.HEALTHY


def worker_status(worker: Worker, now: datetime) -> WorkerStatus:
    result = WorkerStatus.model_validate(worker)
    result.state = effective_worker_state(
        worker.last_heartbeat, worker.state, worker.executors, now,
    )
    return result
