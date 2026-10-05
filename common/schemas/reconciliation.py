"""Small authenticated snapshots; heartbeat observations are not loss reports."""
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from common.schemas.states import AttemptState

MAX_ATTEMPTS = 128


class ReconcileRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    attempt_ids: list[UUID] = Field(default_factory=list, max_length=MAX_ATTEMPTS)


class ReconcileAttempt(BaseModel):
    id: UUID
    job_id: UUID
    state: AttemptState
    lease_expires_at: datetime | None
    container_engine: Literal['podman', 'docker'] | None
    active: bool


class ReconcileView(BaseModel):
    attempts: list[ReconcileAttempt]
    # Unknown and foreign IDs have the same answer: no unrelated worker data.
    unknown: list[UUID]


class MissingWorkload(BaseModel):
    model_config = ConfigDict(extra='forbid')
    job_id: UUID
    container_engine: Literal['podman', 'docker']
    lease_expires_at: datetime
