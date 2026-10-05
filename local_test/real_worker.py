"""Development-only real heartbeat agent and one-shot assignment validation."""
import argparse
import asyncio
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
from importlib.metadata import version
import os
import sys
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from controller.models.worker import Worker
from controller.services.worker_health import worker_status
from local_test.lab import Lab, LabError, REAL_STATE_FILE, STATE_DIR
from worker.config import WorkerSettings
from worker.agent.claim import ClaimFailed, claim_once
from worker.executors.container import ContainerExecutor

ALIAS = "real-local"


@contextmanager
def lock_file(name):
    if STATE_DIR.is_symlink():
        raise LabError("Refusing symlinked lab directory")
    STATE_DIR.mkdir(mode=0o700, exist_ok=True)
    STATE_DIR.chmod(0o700)
    fd = os.open(STATE_DIR / name, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(fd, 0o600)
        yield fd
    finally:
        os.close(fd)


def acquire(fd):
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False


def identity(lab, register=False):
    saved = lab.credentials()
    w = saved.get(ALIAS)
    if w is None:
        if not register:
            raise LabError("No real-worker identity; start make dev-real-worker in another terminal")
        result = lab.request("POST", "/v1/workers/register", json={
            "name": "lab-real-local", "agent_version": version("meshcompute"),
        }).json()
        w = {"id": result["id"], "token": result["token"]}
        lab.save({ALIAS: w})
    with Session(lab.engine) as session:
        worker = session.get(Worker, UUID(w["id"]))
        if worker is None or worker.token_hash != hashlib.sha256(w["token"].encode()).hexdigest():
            raise LabError("Stale real-worker credentials or controller/DB mismatch. Check configuration; stop the agent. "
                           "After a confirmed lab reset, remove .meshcompute-lab/real-worker.json and restart dev-real-worker.")
    return w


def start(lab, work_once=False):
    with lock_file("real-worker.lock") as fd:
        if not acquire(fd):
            raise LabError("A helper-managed real worker is already running in this checkout; stop it first")
        # Validate settings and the actual production API probe before registration.
        env = os.environ.copy()
        env.update(MESHCOMPUTE_WORKER_CONTROLLER_URL=lab.url,
                   MESHCOMPUTE_WORKER_ID="00000000-0000-0000-0000-000000000000",
                   MESHCOMPUTE_WORKER_TOKEN="configuration-check")
        env.setdefault("MESHCOMPUTE_WORKER_CPU_LIMIT", "1")
        env.setdefault("MESHCOMPUTE_WORKER_MEMORY_LIMIT_MB", "256")
        env.setdefault("MESHCOMPUTE_WORKER_CONTAINER_ENGINE", "podman")
        settings = WorkerSettings(controller_url=lab.url, id=env["MESHCOMPUTE_WORKER_ID"],
                                  token=env["MESHCOMPUTE_WORKER_TOKEN"],
                                  cpu_limit=env["MESHCOMPUTE_WORKER_CPU_LIMIT"],
                                  memory_limit_mb=env["MESHCOMPUTE_WORKER_MEMORY_LIMIT_MB"],
                                  container_engine=env["MESHCOMPUTE_WORKER_CONTAINER_ENGINE"])
        executor = ContainerExecutor(settings.podman_socket, settings.docker_socket, settings.container_engine)
        if not asyncio.run(executor.capabilities()).healthy:
            raise LabError("Required real engine API probe failed; run make check-engines and check the worker socket setting")
        w = identity(lab, register=True)
        env.update(MESHCOMPUTE_WORKER_ID=w["id"], MESHCOMPUTE_WORKER_TOKEN=w["token"])
        print(f"Real worker {w['id']}: requested contribution {settings.cpu_limit} CPU / "
              f"{settings.memory_limit_mb} MiB; engine={settings.container_engine}. Ctrl-C stops this agent.", flush=True)
        lab.close()
        # Replace the helper with the actual production entry point. Preserve only
        # this advisory lock across exec; credentials travel in environment only.
        os.set_inheritable(fd, True)
        os.execve(sys.executable, [sys.executable, "-m", "worker.main", *(["work-once"] if work_once else [])], env)


def verify(lab):
    with lock_file("real-worker-test.lock") as test_fd, lock_file("real-worker.lock") as agent_fd:
        if not acquire(test_fd):
            raise LabError("Another real-worker assignment check is running")
        if acquire(agent_fd):
            raise LabError("Start make dev-real-worker in another terminal and leave it running")
        w = identity(lab)
        with Session(lab.engine) as session:
            status = worker_status(session.get(Worker, UUID(w["id"])), datetime.now(timezone.utc))
        if (status.state != "HEALTHY" or not status.container_engines or not status.container_engines.podman
                or not status.executors.container or not status.resources
                or status.resources.cpu_contributed < 0.5 or status.resources.memory_contributed_mb < 128):
            raise LabError("Real worker must be freshly HEALTHY with real Podman capability and >=0.5 CPU / 128 MiB; wait for its heartbeat")
        before = lab.snapshot()
        if (any(j["state"] in ("QUEUED", "RUNNING") for j in before["jobs"])
                or before["attempts"] or before["allocations"]):
            raise LabError("Dirty lab: queued/running jobs, attempts, or reservations exist. Run make dev-state. "
                           "Use a separate lab, or stop agents and explicitly make dev-reset when existing work can be deleted. No job submitted.")
        print("Preflight passed. Keep other job submitters/claimers stopped during this one-shot check.", flush=True)
        try:
            job = lab.submit("real-worker-success", cpu=0.5, memory=128)
        except (LabError, ValueError):
            inspect_failure(lab)
            raise LabError("Submission may have committed; no retry. Inspect make dev-state before rerunning") from None
        print(f"Submitted job {job['id']}; making exactly one claim", flush=True)
        # Recheck after submission: never knowingly claim another queued job.
        pending = lab.snapshot()
        if (any(j["id"] != job["id"] and j["state"] in ("QUEUED", "RUNNING") for j in pending["jobs"])
                or pending["attempts"] or pending["allocations"]):
            raise LabError("Lab changed during submission; no claim made. Results preserved; inspect make dev-state")
        settings = WorkerSettings(controller_url=lab.url, id=w["id"], token=w["token"])
        try:
            result = asyncio.run(claim_once(settings))
            assignment = result.model_dump(mode="json") if result else None
        except ClaimFailed:
            inspect_failure(lab)
            raise LabError("Claim outcome may be ambiguous; no retry. Inspect make dev-state before another claim") from None
        after = lab.snapshot()
        jobs = [j for j in after["jobs"] if j["id"] == job["id"]]
        attempts = [a for a in after["attempts"] if str(a["job_id"]) == job["id"]]
        allocations = [a for a in after["allocations"] if assignment and str(a["job_attempt_id"]) == assignment["attempt_id"]]
        valid = (
            assignment is not None and assignment["job_id"] == job["id"]
            and assignment["runtime"] == "container" and assignment["image"] == job["image"]
            and assignment["command"] == job["command"]
            and assignment["resources"] == {"cpu": 0.5, "memory_mb": 128}
            and assignment["timeout_seconds"] == job["timeout_seconds"]
            and len(jobs) == 1 and jobs[0]["state"] == "RUNNING"
            and len(attempts) == 1 and str(attempts[0]["id"]) == assignment["attempt_id"]
            and str(attempts[0]["worker_id"]) == w["id"] and attempts[0]["state"] == "LEASED"
            and attempts[0]["attempt_number"] == 1 and len(allocations) == 1
            and str(allocations[0]["worker_id"]) == w["id"]
            and allocations[0]["cpu_reserved"] == 0.5 and allocations[0]["memory_reserved_mb"] == 128
        )
        if not valid:
            lab.print_state()
            raise LabError("Assignment validation failed; results preserved. Do not retry blindly; inspect make dev-state")
        print(f"PASS job {job['id']}: RUNNING; attempt {assignment['attempt_id']}: LEASED; reserved 0.5 CPU / 128 MiB")
        print("Assignment validated; this diagnostic does not execute containers. Use make test-execution for Phase 5.")
        print("Results preserved. This diagnostic claim does not renew: its lease expires and recovery releases the reservation. Inspect make dev-state; this diagnostic still requires empty attempt history for a rerun.")


def inspect_failure(lab):
    try:
        lab.print_state()
    except SQLAlchemyError:
        print("Database inspection unavailable; run make dev-state before another submission/claim.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["start", "test", "work-once"])
    args = parser.parse_args()
    lab = None
    try:
        lab = Lab(state_file=REAL_STATE_FILE)
        lab.request("GET", "/health")
        if args.action in ("start", "work-once"):
            start(lab, work_once=args.action == "work-once")
        else:
            verify(lab)
    except LabError as exc:
        raise SystemExit(str(exc)) from None
    except (SQLAlchemyError, OSError, ValueError, KeyError, ValidationError) as exc:
        raise SystemExit(f"Real-worker helper failed ({type(exc).__name__}); check configuration/state. Credentials and raw responses omitted.") from None
    except KeyboardInterrupt:
        raise SystemExit("Stopped; inspect make dev-state before retrying an interrupted assignment check") from None
    finally:
        if lab:
            lab.close()


if __name__ == "__main__":
    main()
