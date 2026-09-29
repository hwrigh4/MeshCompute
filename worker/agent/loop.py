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

logger = logging.getLogger(__name__)
HEARTBEAT_INTERVAL = 5


async def run_agent(settings: WorkerSettings, executor: Executor) -> None:
    psutil.cpu_percent(interval=None)  # Prime the utilization sample.
    agent_version = version("meshcompute")
    async with httpx.AsyncClient(
        base_url=str(settings.controller_url).rstrip("/") + "/",
        headers={"Authorization": f"Bearer {settings.token.get_secret_value()}"},
        timeout=5, trust_env=False, follow_redirects=False,
    ) as client:
        while True:
            started = asyncio.get_running_loop().time()
            try:
                capabilities = await executor.capabilities()
                resources, telemetry = detect_resources(settings.cpu_limit, settings.memory_limit_mb)
                payload = WorkerHeartbeat(
                    agent_version=agent_version,
                    state=WorkerState.HEALTHY if capabilities.container else WorkerState.DEGRADED,
                    cpu_architecture=platform.machine(),
                    resources=resources, telemetry=telemetry, executors=capabilities,
                )
                response = await client.post(
                    f"v1/workers/{settings.id}/heartbeat", json=payload.model_dump(mode="json"),
                )
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                logger.warning("Heartbeat rejected (HTTP %s); retrying", exc.response.status_code)
            except httpx.RequestError:
                logger.warning("Controller unavailable; retrying heartbeat")
            except (OSError, RuntimeError, ValueError):
                logger.warning("Resource or runtime detection failed; retrying heartbeat")
            # Keep the normal cadence without overlapping requests or retry storms.
            elapsed = asyncio.get_running_loop().time() - started
            await asyncio.sleep(max(1, HEARTBEAT_INTERVAL - elapsed))
