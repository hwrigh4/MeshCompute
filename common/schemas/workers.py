from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from common.schemas.states import WorkerState


class WorkerRegistration(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    name: str = Field(min_length=1, max_length=128)
    agent_version: str = Field(min_length=1, max_length=64)


class WorkerView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    agent_version: str
    state: WorkerState
    created_at: datetime
    updated_at: datetime


class RegisteredWorker(WorkerView):
    token: str


class ResourceSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    # CPU capacity uses logical CPUs; core count is informational.
    cpu_physical: int = Field(gt=0)
    cpu_physical_cores: int | None = Field(default=None, gt=0)
    cpu_contributed: float = Field(ge=0)
    cpu_reserved: Literal[0] = 0
    cpu_allocatable: float = Field(ge=0)
    memory_physical_mb: int = Field(gt=0)
    memory_contributed_mb: int = Field(ge=0)
    memory_reserved_mb: Literal[0] = 0
    memory_allocatable_mb: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_capacity(self):
        if self.cpu_contributed > self.cpu_physical:
            raise ValueError("CPU contribution exceeds physical capacity")
        if self.memory_contributed_mb > self.memory_physical_mb:
            raise ValueError("Memory contribution exceeds physical capacity")
        if self.cpu_allocatable != self.cpu_contributed:
            raise ValueError("Phase 2 allocatable CPU must equal contributed CPU")
        if self.memory_allocatable_mb != self.memory_contributed_mb:
            raise ValueError("Phase 2 allocatable memory must equal contributed memory")
        return self


class Telemetry(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    cpu_usage_percent: float = Field(ge=0, le=100)
    load_1m: float | None = Field(default=None, ge=0)
    memory_available_mb: int = Field(ge=0)


class ExecutorCapabilities(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    container: bool = False


class WorkerHeartbeat(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    agent_version: str = Field(min_length=1, max_length=64)
    state: Literal[
        WorkerState.HEALTHY, WorkerState.DEGRADED,
        WorkerState.PAUSED, WorkerState.DRAINING,
    ]
    cpu_architecture: str = Field(min_length=1, max_length=64)
    resources: ResourceSnapshot
    telemetry: Telemetry
    executors: ExecutorCapabilities


class WorkerStatus(WorkerView):
    last_heartbeat: datetime | None
    cpu_architecture: str | None
    resources: ResourceSnapshot | None
    telemetry: Telemetry | None
    executors: ExecutorCapabilities
