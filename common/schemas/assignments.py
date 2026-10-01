from uuid import UUID

from pydantic import BaseModel, ConfigDict

from common.schemas.jobs import JobResources


class ClaimRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WorkAssignment(BaseModel):
    attempt_id: UUID
    job_id: UUID
    runtime: str
    image: str
    command: list[str]
    resources: JobResources
    timeout_seconds: int
