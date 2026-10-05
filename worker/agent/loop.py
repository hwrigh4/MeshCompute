import asyncio
import logging
import platform
from importlib.metadata import version

import httpx
import psutil

from common.schemas.states import WorkerState
from common.schemas.workers import WorkerHeartbeat
from worker.config import WorkerSettings
from worker.executors.base import Executor
from worker.resources.detection import detect_resources
from worker.preemption.control import ControlStore

logger = logging.getLogger(__name__)
HEARTBEAT_INTERVAL = 5


class HeartbeatRejected(RuntimeError):
    """A permanent controller response that requires operator intervention."""


async def run_agent(settings: WorkerSettings, executor: Executor, ready: asyncio.Event | None = None) -> None:
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
                    agent_version=agent_version,
                    state=state.participation if state.participation != 'HEALTHY' else (WorkerState.HEALTHY if capabilities.healthy else WorkerState.DEGRADED),
                    cpu_architecture=platform.machine(),
                    resources=resources, telemetry=telemetry, executors=capabilities.executors,
                    container_engines=capabilities.container_engines,
                )
                response = await client.post(
                    f"v1/workers/{settings.id}/heartbeat", json=payload.model_dump(mode="json"),
                )
                response.raise_for_status()
                if ready is not None:
                    ready.set()
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                if status not in (408, 429) and not 500 <= status < 600:
                    raise HeartbeatRejected(
                        f"Heartbeat rejected (HTTP {status}); check controller URL, "
                        "worker ID/token, and agent/controller protocol compatibility"
                    ) from None
                logger.warning("Heartbeat failed (HTTP %s); retrying", status)
            except httpx.RequestError:
                logger.warning("Controller unavailable; retrying heartbeat")
            except (OSError, RuntimeError, ValueError):
                logger.warning("Resource or runtime detection failed; retrying heartbeat")
            # Keep the normal cadence without overlapping requests or retry storms.
            elapsed = asyncio.get_running_loop().time() - started
            end = asyncio.get_running_loop().time() + max(1, HEARTBEAT_INTERVAL - elapsed)
            while asyncio.get_running_loop().time() < end and store.read().revision == revision:
                await asyncio.sleep(.1)
