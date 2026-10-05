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
from worker.agent.reconciliation import reconcile, ReconciliationFailed
from worker.agent.execution import ReportingFailed, work_once
from worker.config import WorkerSettings
from worker.preemption.control import ControlError, command
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
    parser = argparse.ArgumentParser(description="MeshCompute worker: heartbeat, execution, and local provider controls")
    parser.add_argument("action", nargs="?", choices=["reconcile", "claim", "work-once", "status", "pause", "resume", "drain", "resources", "stop-all"], help="default: heartbeat; work-once: one execution; controls operate locally without a token")
    parser.add_argument('--cpu', type=float, help='contributed logical CPUs (resources only)')
    parser.add_argument('--memory', type=int, help='contributed MiB (resources only)')
    args = parser.parse_args()
    if args.action == 'resources' and (args.cpu is None or args.memory is None):
        parser.error('resources requires --cpu and --memory')
    if args.action != 'resources' and (args.cpu is not None or args.memory is not None):
        parser.error('--cpu/--memory require resources')
    local = args.action in ('status', 'pause', 'resume', 'drain', 'resources', 'stop-all')
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if sys.platform != "linux":
        raise SystemExit("The MeshCompute worker currently requires Linux")
    try:
        settings = WorkerSettings(token='local-control-no-credential') if local else WorkerSettings()
    except ValidationError:
        # Never print configuration values, even in validation errors.
        raise SystemExit("Invalid worker configuration; check MESHCOMPUTE_WORKER_* settings") from None
    logging.info("Starting worker %s", settings.id)
    try:
        if local:
            asyncio.run(command(settings, args.action, args.cpu, args.memory))
        elif args.action == "reconcile":
            clear = asyncio.run(reconcile(settings))
            print("Reconciled; " + ("no active reservation" if clear else "active work remains; claims blocked"))
        elif args.action == "claim":
            assignment = asyncio.run(claim_once(settings))
            print(assignment.model_dump_json(indent=2) if assignment else "No work (local controls or controller 204)")
        else:
            asyncio.run(serve(settings, execute=args.action == "work-once"))
    except (HeartbeatRejected, ClaimFailed, ReportingFailed, LeaseLost, ControlError, ReconciliationFailed) as exc:
        raise SystemExit(str(exc)) from None
    except (OSError, TimeoutError, ValueError):
        message = 'Worker/control failed; inspect provider settings, permissions, and live agent.'
        if args.action == 'stop-all':
            message += ' Local cleanup is not confirmed.'
        raise SystemExit(message) from None
    logging.info("Worker stopped")


if __name__ == "__main__":
    main()
