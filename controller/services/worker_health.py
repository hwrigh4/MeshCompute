from datetime import datetime

from common.schemas.states import WorkerState
from common.schemas.workers import WorkerStatus
from controller.models.worker import Worker


def worker_status(worker: Worker, now: datetime) -> WorkerStatus:
    """Derive freshness at read time, without rewriting reported participation state."""
    result = WorkerStatus.model_validate(worker)
    if worker.last_heartbeat is None:
        return result
    age = (now - worker.last_heartbeat).total_seconds()
    if age > 30:
        result.state = WorkerState.OFFLINE
    elif age >= 15:
        result.state = WorkerState.DEGRADED
    elif worker.state in (WorkerState.PAUSED, WorkerState.DRAINING):
        result.state = worker.state
    elif not worker.executors.get("container") or worker.state == WorkerState.DEGRADED:
        result.state = WorkerState.DEGRADED
    else:
        result.state = WorkerState.HEALTHY
    return result
