"""Controller lease policy. Workers consume these timings through the protocol."""
from datetime import timedelta

from sqlalchemy import func, select

from common.schemas.states import AttemptState

LEASE_DURATION_SECONDS = 30
RENEW_AFTER_SECONDS = 10
RECOVERY_INTERVAL_SECONDS = 5
RECOVERY_BATCH_SIZE = 100
ACTIVE_ATTEMPTS = (AttemptState.LEASED, AttemptState.RUNNING)


def controller_now(session):
    # PostgreSQL now() is transaction-start time, potentially before a lock wait.
    return session.scalar(select(func.clock_timestamp()))


def deadline(now):
    return now + timedelta(seconds=LEASE_DURATION_SECONDS)


def timing(expires_at):
    return dict(lease_expires_at=expires_at, lease_duration_seconds=LEASE_DURATION_SECONDS,
                renew_after_seconds=RENEW_AFTER_SECONDS)
