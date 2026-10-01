import asyncio
import logging
import signal
import sys
from contextlib import suppress

from pydantic import ValidationError

from worker.agent.loop import HeartbeatRejected, run_agent
from worker.config import WorkerSettings
from worker.executors.docker import DockerExecutor


async def serve(settings: WorkerSettings) -> None:
    loop = asyncio.get_running_loop()
    agent = asyncio.create_task(run_agent(settings, DockerExecutor(settings.docker_socket)))
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, agent.cancel)
    try:
        with suppress(asyncio.CancelledError):
            await agent
    finally:
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(sig)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if sys.platform != "linux":
        raise SystemExit("The MeshCompute worker currently requires Linux")
    try:
        settings = WorkerSettings()
    except ValidationError:
        # Never print configuration values, even in validation errors.
        raise SystemExit("Invalid worker configuration; check MESHCOMPUTE_WORKER_* settings") from None
    logging.info("Starting worker %s", settings.id)
    try:
        asyncio.run(serve(settings))
    except HeartbeatRejected as exc:
        raise SystemExit(str(exc)) from None
    logging.info("Worker stopped")


if __name__ == "__main__":
    main()
