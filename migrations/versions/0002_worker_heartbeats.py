"""Persist worker heartbeat resources and capabilities."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for column in (
        sa.Column("last_heartbeat", sa.DateTime(timezone=True)),
        sa.Column("cpu_physical", sa.Integer()),
        sa.Column("cpu_physical_cores", sa.Integer()),
        sa.Column("cpu_contributed", sa.Float()),
        sa.Column("memory_physical_mb", sa.Integer()),
        sa.Column("memory_contributed_mb", sa.Integer()),
        sa.Column("cpu_architecture", sa.String(64)),
        sa.Column("executors", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("telemetry", postgresql.JSONB()),
    ):
        op.add_column("workers", column)


def downgrade() -> None:
    # All states become REGISTERING again in the Phase 1 schema/API.
    op.execute("UPDATE workers SET state = 'REGISTERING'")
    for name in (
        "telemetry", "executors", "cpu_architecture", "memory_contributed_mb",
        "memory_physical_mb", "cpu_contributed", "cpu_physical_cores",
        "cpu_physical", "last_heartbeat",
    ):
        op.drop_column("workers", name)
