"""Bounded container execution results.

Revision ID: 0005
Revises: 0004
"""
from alembic import op
import sqlalchemy as sa

revision = '0005'
down_revision = '0004'
branch_labels = None
depends_on = None


def upgrade():
    for column in (
        sa.Column('container_engine', sa.String(16)),
        sa.Column('exit_code', sa.Integer()),
        sa.Column('failure_reason', sa.String(64)),
        sa.Column('stdout_tail', sa.String(65536)),
        sa.Column('stderr_tail', sa.String(65536)),
        sa.Column('stdout_truncated', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('stderr_truncated', sa.Boolean(), nullable=False, server_default='false'),
    ):
        op.add_column('job_attempts', column)
    op.create_check_constraint('ck_attempt_engine', 'job_attempts', "container_engine IS NULL OR container_engine IN ('podman', 'docker')")
    op.create_check_constraint('ck_attempt_log_bytes', 'job_attempts', 'octet_length(stdout_tail) <= 65536 AND octet_length(stderr_tail) <= 65536')


def downgrade():
    if op.get_bind().execute(sa.text("SELECT EXISTS (SELECT 1 FROM job_attempts WHERE state <> 'LEASED')")).scalar():
        raise RuntimeError('Cannot downgrade execution history to Phase 4; use a separate empty validation database')
    op.drop_constraint('ck_attempt_log_bytes', 'job_attempts')
    op.drop_constraint('ck_attempt_engine', 'job_attempts')
    for name in ('stderr_truncated', 'stdout_truncated', 'stderr_tail', 'stdout_tail', 'failure_reason', 'exit_code', 'container_engine'):
        op.drop_column('job_attempts', name)
