from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, Enum, Float, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from common.schemas.states import JobState
from controller.models.base import Base


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    # Reserved for a future requester identity; never accepted from the public API.
    owner_id: Mapped[UUID | None]
    name: Mapped[str] = mapped_column(String(128))
    runtime: Mapped[str] = mapped_column(String(32))
    image: Mapped[str] = mapped_column(String(2048))
    command: Mapped[list[str]] = mapped_column(JSONB)
    cpu_requested: Mapped[float] = mapped_column(Float)
    memory_requested_mb: Mapped[int] = mapped_column(Integer)
    timeout_seconds: Mapped[int] = mapped_column(Integer)
    state: Mapped[JobState] = mapped_column(
        Enum(JobState, native_enum=False, length=16), default=JobState.QUEUED
    )
    max_attempts: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
