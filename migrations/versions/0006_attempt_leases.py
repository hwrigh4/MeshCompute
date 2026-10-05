"""Ownership leases and recovery index.

Revision ID: 0006
Revises: 0005
"""
from alembic import op
import sqlalchemy as sa

revision = '0006'
down_revision = '0005'
branch_labels = None
depends_on = None


def upgrade():
    # Block concurrent old-controller claims while checking and installing the guard.
    op.execute('LOCK TABLE job_attempts IN ACCESS EXCLUSIVE MODE')
    if op.get_bind().execute(sa.text("SELECT EXISTS (SELECT 1 FROM job_attempts WHERE state IN ('LEASED','RUNNING') AND lease_expires_at IS NULL)")).scalar():
        raise RuntimeError('Active pre-Phase6 attempts have no lease. Stop old controllers/workers and inspect their reservations; migrate a separate database or explicitly reset a disposable local lab. No leases were invented.')
    op.create_check_constraint('ck_active_attempt_lease', 'job_attempts', "state NOT IN ('LEASED', 'RUNNING') OR lease_expires_at IS NOT NULL")
    op.create_index('ix_job_attempts_state_lease', 'job_attempts', ['state', 'lease_expires_at'])


def downgrade():
    op.execute('LOCK TABLE job_attempts IN ACCESS EXCLUSIVE MODE')
    if op.get_bind().execute(sa.text("SELECT EXISTS (SELECT 1 FROM job_attempts WHERE state = 'LOST' OR state IN ('LEASED','RUNNING'))")).scalar():
        raise RuntimeError('Cannot downgrade LOST history or active leases to Phase 5; inspect state and use a separate disposable database')
    op.drop_index('ix_job_attempts_state_lease', table_name='job_attempts')
    op.drop_constraint('ck_active_attempt_lease', 'job_attempts')
