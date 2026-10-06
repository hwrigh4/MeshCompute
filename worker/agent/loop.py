import asyncio
import logging
import platform
from importlib.metadata import version

import httpx
import psutil

from common.logging import event
from common.schemas.states import WorkerState
from common.schemas.workers import WorkerHeartbeat
from worker.config import WorkerSettings
from worker.executors.base import Executor
from worker.resources.detection import detect_resources
from worker.preemption.control import ControlStore
from worker.agent.reconciliation import active_ids, reconcile, ReconciliationFailed, ReconciliationRejected, INTERVAL

logger = logging.getLogger(__name__)
HEARTBEAT_INTERVAL = 5


class HeartbeatRejected(RuntimeError):
    """A permanent controller response that requires operator intervention."""


async def run_agent(settings: WorkerSettings, executor: Executor, ready: asyncio.Event | None = None) -> None:
    # Scans are independent of heartbeats and lease renewal. The first complete
    # comparison gates startup; later failures never turn absence into evidence.
    initialized, reconnect = asyncio.Event(), asyncio.Event()

    async def comparisons():
        while True:
            reconnect.clear()
            try:
                await reconcile(settings)
                initialized.set()
            except ReconciliationRejected:
                raise
            except ReconciliationFailed:
                event(logger, 'reconciliation.incomplete', level=logging.WARNING, worker_id=settings.id)
            try:
                await asyncio.wait_for(reconnect.wait(), INTERVAL if initialized.is_set() else 5)
            except TimeoutError:
                pass

    async def heartbeats():
        await initialized.wait()
        await _heartbeats(settings, executor, ready, reconnect)

    comparisons_task = asyncio.create_task(comparisons())
    heartbeat_task = asyncio.create_task(heartbeats())
    try:
        done, _ = await asyncio.wait((comparisons_task, heartbeat_task), return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            await task
    finally:
        comparisons_task.cancel()
        heartbeat_task.cancel()
        await asyncio.gather(comparisons_task, heartbeat_task, return_exceptions=True)


async def _heartbeats(settings, executor, ready, reconnect):
    disconnected = False
    store = ControlStore(settings)
    psutil.cpu_percent(interval=None)  # Prime the utilization sample.
    agent_version = version("meshcompute")
    async with httpx.AsyncClient(
        base_url=str(settings.controller_url).rstrip("/") + "/",
        headers={"Authorization": f"Bearer {settings.token.get_secret_value()}"},
        timeout=5, trust_env=False, follow_redirects=False,
    ) as client:
        while True:
            started = asyncio.get_running_loop().time()
            revision = store.read().revision
            try:
                capabilities = await executor.capabilities()
                state = store.read()
                resources, telemetry = detect_resources(state.cpu, state.memory_mb)
                payload = WorkerHeartbeat(
                    agent_version=agent_version, active_attempt_ids=active_ids(settings),
                    state=state.participation if state.participation != 'HEALTHY' else (WorkerState.HEALTHY if capabilities.healthy else WorkerState.DEGRADED),
                    cpu_architecture=platform.machine(),
                    resources=resources, telemetry=telemetry, executors=capabilities.executors,
                    container_engines=capabilities.container_engines,
                )
                response = await client.post(
                    f"v1/workers/{settings.id}/heartbeat", json=payload.model_dump(mode="json"),
                )
                response.raise_for_status()
                event(logger, "heartbeat.confirmed", level=logging.DEBUG, worker_id=settings.id, state=payload.state)
                if disconnected:
                    event(logger, "controller.reconnected", worker_id=settings.id)
                    reconnect.set()
                    disconnected = False
                if ready is not None:
                    ready.set()
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                if status not in (408, 429) and not 500 <= status < 600:
                    raise HeartbeatRejected(
                        f"Heartbeat rejected (HTTP {status}); check controller URL, "
                        "worker ID/token, and agent/controller protocol compatibility"
                    ) from None
                event(logger, "heartbeat.failed", level=logging.DEBUG if disconnected else logging.WARNING, worker_id=settings.id, outcome="controller_error")
                disconnected = True
                reconnect.set()
            except httpx.RequestError:
                event(logger, "heartbeat.failed", level=logging.DEBUG if disconnected else logging.WARNING, worker_id=settings.id, outcome="connection_error")
                disconnected = True
                reconnect.set()
            except (OSError, RuntimeError, ValueError):
                event(logger, "heartbeat.detection_failed", level=logging.WARNING, worker_id=settings.id)
            # Keep the normal cadence without overlapping requests or retry storms.
            elapsed = asyncio.get_running_loop().time() - started
            end = asyncio.get_running_loop().time() + max(1, HEARTBEAT_INTERVAL - elapsed)
            while asyncio.get_running_loop().time() < end and store.read().revision == revision:
                await asyncio.sleep(.1)
