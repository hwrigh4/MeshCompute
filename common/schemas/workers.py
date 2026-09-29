from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class WorkerRegistration(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    name: str = Field(min_length=1, max_length=128)
    agent_version: str = Field(min_length=1, max_length=64)


class WorkerView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    agent_version: str
    state: Literal["REGISTERING"]
    created_at: datetime
    updated_at: datetime


class RegisteredWorker(WorkerView):
    token: str
