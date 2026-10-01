from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from common.schemas.states import JobState

NonemptyName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]
ImageReference = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2048)]


class JobResources(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    cpu: float = Field(gt=0)
    memory_mb: int = Field(gt=0, le=2**31 - 1)


class JobSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    name: NonemptyName
    runtime: Literal["container"]
    image: ImageReference
    # Preserve argv exactly, including empty arguments and meaningful whitespace.
    command: list[str] = Field(min_length=1)
    resources: JobResources
    timeout_seconds: int = Field(gt=0, le=2**31 - 1)
    max_attempts: int = Field(ge=1, le=2**31 - 1)


class JobView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    runtime: str
    image: str
    command: list[str]
    cpu_requested: float
    memory_requested_mb: int
    timeout_seconds: int
    state: JobState
    max_attempts: int
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
