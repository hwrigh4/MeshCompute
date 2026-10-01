from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, Numeric, func
from sqlalchemy.orm import Mapped, mapped_column

from controller.models.base import Base


class WorkerAllocation(Base):
    __tablename__ = "worker_allocations"
    __table_args__ = (
        CheckConstraint("cpu_reserved > 0 AND cpu_reserved < 'Infinity'::numeric", name="ck_allocation_cpu_positive_finite"),
        CheckConstraint("memory_reserved_mb > 0", name="ck_allocation_memory_positive"),
    )

    # The primary key guarantees at most one allocation per attempt.
    job_attempt_id: Mapped[UUID] = mapped_column(ForeignKey("job_attempts.id"), primary_key=True)
    worker_id: Mapped[UUID] = mapped_column(ForeignKey("workers.id"), index=True)
    cpu_reserved: Mapped[Decimal] = mapped_column(Numeric)
    memory_reserved_mb: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
