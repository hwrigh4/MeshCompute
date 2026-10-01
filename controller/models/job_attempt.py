from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, DateTime, Enum, ForeignKey, Integer, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from common.schemas.states import AttemptState
from controller.models.base import Base


class JobAttempt(Base):
    __tablename__ = "job_attempts"
    __table_args__ = (
        UniqueConstraint("job_id", "attempt_number", name="uq_job_attempt_number"),
        CheckConstraint("attempt_number >= 1", name="ck_attempt_number_positive"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    job_id: Mapped[UUID] = mapped_column(ForeignKey("jobs.id"))
    worker_id: Mapped[UUID] = mapped_column(ForeignKey("workers.id"), index=True)
    attempt_number: Mapped[int] = mapped_column(Integer)
    state: Mapped[AttemptState] = mapped_column(
        Enum(AttemptState, native_enum=False, length=16), default=AttemptState.LEASED
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
