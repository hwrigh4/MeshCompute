import asyncio
import argparse
import logging
import signal
import sys
from contextlib import suppress

from pydantic import ValidationError

from worker.agent.loop import HeartbeatRejected, run_agent
from worker.agent.claim import ClaimFailed, claim_once
from worker.agent.leases import LeaseLost
from worker.agent.execution import ReportingFailed, work_once
from worker.config import WorkerSettings
from worker.executors.container import ContainerExecutor


async def serve(settings: WorkerSettings, execute: bool = False) -> None:
    loop = asyncio.get_running_loop()
    executor = ContainerExecutor(settings.podman_socket, settings.docker_socket, settings.container_engine)
    agent = asyncio.create_task(work_once(settings, executor) if execute else run_agent(settings, executor))
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, agent.cancel)
    try:
        with suppress(asyncio.CancelledError):
            await agent
    finally:
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(sig)


def main() -> None:
    parser = argparse.ArgumentParser(description="MeshCompute worker: heartbeat, diagnostic claim, or one restricted execution")
    parser.add_argument("action", nargs="?", choices=["claim", "work-once"], help="claim prints an assignment; work-once executes one job with heartbeats")
    args = parser.parse_args()
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
        if args.action == "claim":
            assignment = asyncio.run(claim_once(settings))
            print(assignment.model_dump_json(indent=2) if assignment else "No work (204)")
        else:
            asyncio.run(serve(settings, execute=args.action == "work-once"))
    except (HeartbeatRejected, ClaimFailed, ReportingFailed, LeaseLost) as exc:
        raise SystemExit(str(exc)) from None
    logging.info("Worker stopped")


if __name__ == "__main__":
    main()
