"""Persist attempts, resource reservations, and container engine metadata."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("workers", sa.Column("container_engines", postgresql.JSONB(), nullable=True))
    op.create_index("ix_jobs_state_created_at_id", "jobs", ["state", "created_at", "id"])
    op.create_table(
        "job_attempts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), sa.ForeignKey("jobs.id"), nullable=False),
        sa.Column("worker_id", sa.Uuid(), sa.ForeignKey("workers.id"), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id", "attempt_number", name="uq_job_attempt_number"),
        sa.CheckConstraint("attempt_number >= 1", name="ck_attempt_number_positive"),
    )
    op.create_index("ix_job_attempts_worker_id", "job_attempts", ["worker_id"])
    op.create_table(
        "worker_allocations",
        sa.Column("job_attempt_id", sa.Uuid(), sa.ForeignKey("job_attempts.id"), nullable=False),
        sa.Column("worker_id", sa.Uuid(), sa.ForeignKey("workers.id"), nullable=False),
        sa.Column("cpu_reserved", sa.Numeric(), nullable=False),
        sa.Column("memory_reserved_mb", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("job_attempt_id"),
        sa.CheckConstraint("cpu_reserved > 0 AND cpu_reserved < 'Infinity'::numeric", name="ck_allocation_cpu_positive_finite"),
        sa.CheckConstraint("memory_reserved_mb > 0", name="ck_allocation_memory_positive"),
    )
    op.create_index("ix_worker_allocations_worker_id", "worker_allocations", ["worker_id"])


def downgrade() -> None:
    # These jobs were assigned but never executed in Phase 4.
    op.execute("""
        UPDATE jobs SET state = 'QUEUED', updated_at = now()
        WHERE state = 'RUNNING' AND id IN (SELECT job_id FROM job_attempts)
    """)
    op.drop_table("worker_allocations")
    op.drop_table("job_attempts")
    op.drop_index("ix_jobs_state_created_at_id", table_name="jobs")
    op.drop_column("workers", "container_engines")
