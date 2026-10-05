"""Bounded worker reports; LOST/LEASE_EXPIRED are controller-only outcomes."""
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from common.schemas.states import AttemptState

LOG_LIMIT = 64 * 1024
Engine = Literal['podman', 'docker']
FailureReason = Literal[
    'ENGINE_UNAVAILABLE', 'IMAGE_PULL_FAILED', 'CONTAINER_CREATE_FAILED',
    'CONTAINER_START_FAILED', 'NONZERO_EXIT', 'TIMEOUT', 'RESULT_CAPTURE_FAILED',
    'SECURITY_POLICY_UNSUPPORTED', 'CONTAINER_CLEANUP_FAILED', 'WORKER_INTERRUPTED',
]


class AttemptStart(BaseModel):
    model_config = ConfigDict(extra='forbid')
    container_engine: Engine


class AttemptResult(BaseModel):
    model_config = ConfigDict(extra='forbid')
    state: Literal[AttemptState.SUCCEEDED, AttemptState.FAILED, AttemptState.TIMED_OUT]
    container_engine: Engine | None = None
    exit_code: int | None = Field(default=None, ge=-2147483648, le=2147483647)
    failure_reason: FailureReason | None = None
    stdout_tail: str = Field(default='', max_length=LOG_LIMIT)
    stderr_tail: str = Field(default='', max_length=LOG_LIMIT)
    stdout_truncated: bool = False
    stderr_truncated: bool = False

    @field_validator('stdout_tail', 'stderr_tail')
    @classmethod
    def bounded_bytes(cls, value):
        if '\x00' in value or len(value.encode('utf-8')) > LOG_LIMIT:
            raise ValueError('Log must contain at most 64 KiB UTF-8 and no NUL')
        return value

    @model_validator(mode='after')
    def outcome(self):
        if self.state == AttemptState.SUCCEEDED:
            if self.exit_code != 0 or self.failure_reason is not None or self.container_engine is None:
                raise ValueError('Success requires engine, exit 0, and no failure reason')
        elif self.failure_reason is None:
            raise ValueError('Failure requires a stable reason')
        if self.failure_reason == 'NONZERO_EXIT' and self.exit_code in (None, 0):
            raise ValueError('NONZERO_EXIT requires a nonzero exit code')
        if self.state == AttemptState.TIMED_OUT and self.container_engine is None:
            raise ValueError('Timeout requires an execution engine')
        if self.state == AttemptState.TIMED_OUT and self.failure_reason != 'TIMEOUT':
            raise ValueError('Timeout requires TIMEOUT reason')
        return self


class AttemptView(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    job_id: UUID
    worker_id: UUID
    attempt_number: int
    state: AttemptState
    container_engine: Engine | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    lease_expires_at: datetime | None
    exit_code: int | None
    failure_reason: str | None
    stdout_tail: str | None
    stderr_tail: str | None
    stdout_truncated: bool
    stderr_truncated: bool
