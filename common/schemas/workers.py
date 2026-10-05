from datetime import datetime
from math import isclose
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from common.schemas.states import WorkerState
from common.schemas.reconciliation import MAX_ATTEMPTS


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
    cpu_reserved: float = Field(default=0, ge=0)
    cpu_allocatable: float = Field(ge=0)
    memory_physical_mb: int = Field(gt=0)
    memory_contributed_mb: int = Field(ge=0)
    memory_reserved_mb: int = Field(default=0, ge=0)
    memory_allocatable_mb: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_capacity(self):
        if self.cpu_contributed > self.cpu_physical:
            raise ValueError("CPU contribution exceeds physical capacity")
        if self.memory_contributed_mb > self.memory_physical_mb:
            raise ValueError("Memory contribution exceeds physical capacity")
        if not isclose(self.cpu_allocatable, max(0, self.cpu_contributed - self.cpu_reserved), abs_tol=1e-9):
            raise ValueError("Allocatable CPU must equal unreserved contribution")
        if self.memory_allocatable_mb != max(0, self.memory_contributed_mb - self.memory_reserved_mb):
            raise ValueError("Allocatable memory must equal unreserved contribution")
        return self


class Telemetry(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    cpu_usage_percent: float = Field(ge=0, le=100)
    load_1m: float | None = Field(default=None, ge=0)
    memory_available_mb: int = Field(ge=0)


class ExecutorCapabilities(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    container: bool = False


class ContainerEngines(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    podman: bool = False
    docker: bool = False


class RuntimeCapabilities(BaseModel):
    executors: ExecutorCapabilities
    container_engines: ContainerEngines
    healthy: bool


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
    active_attempt_ids: list[UUID] = Field(default_factory=list, max_length=MAX_ATTEMPTS)
    container_engines: ContainerEngines | None = None

    @model_validator(mode="after")
    def validate_container_engines(self):
        if self.container_engines is not None:
            if self.executors.container != any(self.container_engines.model_dump().values()):
                raise ValueError("Container capability must match usable container engines")
        return self


class WorkerStatus(WorkerView):
    last_heartbeat: datetime | None
    cpu_architecture: str | None
    resources: ResourceSnapshot | None
    telemetry: Telemetry | None
    executors: ExecutorCapabilities
    container_engines: ContainerEngines | None
