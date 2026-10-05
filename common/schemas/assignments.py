from uuid import UUID

from pydantic import BaseModel, ConfigDict, PrivateAttr

from common.schemas.leases import LeaseTiming
from common.schemas.jobs import JobResources


class ClaimRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WorkAssignment(LeaseTiming):
    # Local-only conservative clock; never serialized into the wire protocol.
    _provider_stop_sequence: int | None = PrivateAttr(default=None)
    _lease_deadline: float | None = PrivateAttr(default=None)
    attempt_id: UUID
    job_id: UUID
    runtime: str
    image: str
    command: list[str]
    resources: JobResources
    timeout_seconds: int
