from datetime import datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import DateTime, Enum, Float, Integer, String, func, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, column_property

from controller.models.base import Base
from controller.models.worker_allocation import WorkerAllocation
from common.schemas.states import WorkerState
from common.schemas.workers import ResourceSnapshot


class Worker(Base):
    __tablename__ = "workers"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(128))
    token_hash: Mapped[str] = mapped_column(String(64))
    agent_version: Mapped[str] = mapped_column(String(64))
    # Registration alone does not establish worker health.
    state: Mapped[WorkerState] = mapped_column(
        Enum(WorkerState, native_enum=False, length=16), default=WorkerState.REGISTERING
    )
    last_heartbeat: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cpu_physical: Mapped[int | None] = mapped_column(Integer)
    cpu_physical_cores: Mapped[int | None] = mapped_column(Integer)
    cpu_contributed: Mapped[float | None] = mapped_column(Float)
    memory_physical_mb: Mapped[int | None] = mapped_column(Integer)
    memory_contributed_mb: Mapped[int | None] = mapped_column(Integer)
    cpu_architecture: Mapped[str | None] = mapped_column(String(64))
    executors: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")
    container_engines: Mapped[dict | None] = mapped_column(JSONB)
    telemetry: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    cpu_reserved = column_property(
        select(func.coalesce(func.sum(WorkerAllocation.cpu_reserved), 0))
        .where(WorkerAllocation.worker_id == id).correlate_except(WorkerAllocation).scalar_subquery()
    )
    memory_reserved_mb = column_property(
        select(func.coalesce(func.sum(WorkerAllocation.memory_reserved_mb), 0))
        .where(WorkerAllocation.worker_id == id).correlate_except(WorkerAllocation).scalar_subquery()
    )

    @property
    def resources(self) -> ResourceSnapshot | None:
        if self.last_heartbeat is None:
            return None
        return ResourceSnapshot(
            cpu_physical=self.cpu_physical,
            cpu_physical_cores=self.cpu_physical_cores,
            cpu_contributed=self.cpu_contributed,
            cpu_reserved=self.cpu_reserved,
            cpu_allocatable=max(Decimal(0), Decimal(str(self.cpu_contributed)) - self.cpu_reserved),
            memory_physical_mb=self.memory_physical_mb,
            memory_contributed_mb=self.memory_contributed_mb,
            memory_reserved_mb=self.memory_reserved_mb,
            memory_allocatable_mb=max(0, self.memory_contributed_mb - self.memory_reserved_mb),
        )
