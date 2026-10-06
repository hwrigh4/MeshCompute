"""Conservative monotonic ownership tracking, independent of heartbeats."""
import asyncio
import logging

import httpx

from common.logging import event
from common.schemas.leases import LeaseRenewal

logger = logging.getLogger(__name__)


class LeaseLost(RuntimeError):
    pass


class LeaseKeeper:
    def __init__(self, settings, assignment):
        self.settings, self.assignment = settings, assignment
        self.deadline = assignment._lease_deadline
        self.ready = asyncio.Event()
        self.lost = False
        self.reason = 'Attempt lease lost; local execution stopped, controller recovery owns reservation release'
        if self.deadline is not None:
            self.ready.set()

    def lose(self):
        if not self.lost:
            event(logger, "lease.lost", level=logging.WARNING, worker_id=self.settings.id, job_id=self.assignment.job_id, attempt_id=self.assignment.attempt_id)
        self.lost = True
        self.ready.set()

    def check(self):
        if self.lost or self.deadline is None or asyncio.get_running_loop().time() >= self.deadline:
            self.lose()
            raise LeaseLost(self.reason)

    async def run(self):
        loop = asyncio.get_running_loop()
        async with httpx.AsyncClient(
            base_url=str(self.settings.controller_url).rstrip('/') + '/',
            headers={'Authorization': f'Bearer {self.settings.token.get_secret_value()}'},
            timeout=5, trust_env=False, follow_redirects=False,
        ) as client:
            while True:
                sent = loop.time()
                if self.deadline is not None and sent >= self.deadline:
                    self.lose()
                    return
                remaining = self.deadline - sent if self.deadline is not None else 5
                delay = 2  # Bounded transient retry cadence, not a lease policy constant.
                try:
                    async with asyncio.timeout(min(5, remaining)):
                        response = await client.post(
                            f'v1/workers/{self.settings.id}/attempts/{self.assignment.attempt_id}/renew',
                        )
                    if response.status_code == 200:
                        renewal = LeaseRenewal.model_validate(response.json())
                        if renewal.attempt_id != self.assignment.attempt_id:
                            raise ValueError('Unexpected attempt')
                        # Use request SEND time, not response receipt: response latency
                        # must never grant the worker extra ownership time.
                        if self.deadline is not None and loop.time() >= self.deadline:
                            self.lose()
                            return
                        self.deadline = sent + renewal.lease_duration_seconds
                        self.check()
                        self.ready.set()
                        event(logger, "lease.renewed", level=logging.DEBUG, worker_id=self.settings.id, attempt_id=self.assignment.attempt_id, job_id=self.assignment.job_id)
                        delay = max(0, sent + renewal.renew_after_seconds - loop.time())
                    elif response.status_code not in (408, 429) and response.status_code < 500:
                        self.lose()  # Includes auth failures and expired/terminal conflicts.
                        return
                except (httpx.HTTPError, OSError, ValueError, TimeoutError):
                    pass
                if self.deadline is None or loop.time() >= self.deadline:
                    self.lose()
                    return
                await asyncio.sleep(min(delay, self.deadline - loop.time()))
