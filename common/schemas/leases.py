from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field, model_validator


class LeaseTiming(BaseModel):
    lease_expires_at: datetime
    lease_duration_seconds: int = Field(gt=0)
    renew_after_seconds: int = Field(gt=0)

    @model_validator(mode='after')
    def valid_timing(self):
        if self.renew_after_seconds >= self.lease_duration_seconds or self.lease_expires_at.tzinfo is None:
            raise ValueError('Lease requires a timezone and renewal before expiration')
        return self


class LeaseRenewal(LeaseTiming):
    attempt_id: UUID
