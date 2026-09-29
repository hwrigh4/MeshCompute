from enum import StrEnum


class WorkerState(StrEnum):
    REGISTERING = "REGISTERING"
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    DRAINING = "DRAINING"
    PAUSED = "PAUSED"
    OFFLINE = "OFFLINE"
