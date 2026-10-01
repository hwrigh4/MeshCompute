"""Development-only API driver and SQL inspector. Run from the repository root."""
import argparse
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import tempfile
import time
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from common.schemas.jobs import JobView
from common.schemas.assignments import WorkAssignment
from common.schemas.workers import WorkerHeartbeat
from controller.database import get_engine
from controller.models.job import Job
from controller.models.job_attempt import JobAttempt
from controller.models.worker import Worker
from controller.models.worker_allocation import WorkerAllocation
from controller.services.worker_health import worker_status

STATE_DIR = Path(__file__).resolve().parents[1] / ".meshcompute-lab"
STATE_FILE = STATE_DIR / "workers.json"
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


class LabError(RuntimeError):
    pass


class Lab:
    def __init__(self):
        self.engine = get_engine()
        self.url = os.environ.get("MESHCOMPUTE_LAB_URL", "http://127.0.0.1:8000").rstrip("/")
        parsed = urlsplit(self.url)
        db = self.engine.url
        if (parsed.hostname not in LOCAL_HOSTS or parsed.scheme not in {"http", "https"}
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or db.host not in LOCAL_HOSTS or not (db.database or "").startswith("meshcompute")):
            raise LabError("Lab requires a loopback controller and PostgreSQL database named meshcompute*. Never target production.")
        self.client = httpx.Client(base_url=self.url, timeout=10, trust_env=False, follow_redirects=False)

    def close(self):
        self.client.close()
        self.engine.dispose()

    def request(self, method, path, **kwargs):
        try:
            response = self.client.request(method, path, **kwargs)
        except httpx.RequestError:
            raise LabError(f"{method} {path}: network failure; no automatic retry") from None
        if response.is_error or response.is_redirect:
            raise LabError(f"{method} {path}: HTTP {response.status_code}; response body omitted")
        return response

    def credentials(self):
        if STATE_DIR.is_symlink() or STATE_FILE.is_symlink():
            raise LabError("Refusing symlinked credential storage")
        if not STATE_FILE.exists():
            return {}
        if STATE_FILE.stat().st_mode & 0o077:
            raise LabError("Credential file permissions must be 0600")
        saved = json.loads(STATE_FILE.read_text())
        if saved["controller_url"] != self.url:
            raise LabError("Saved credentials belong to another controller; use the matching URL or reset the lab")
        return saved["workers"]

    def save(self, workers):
        if STATE_DIR.is_symlink() or STATE_FILE.is_symlink():
            raise LabError("Refusing symlinked credential storage")
        STATE_DIR.mkdir(mode=0o700, exist_ok=True)
        STATE_DIR.chmod(0o700)
        # Atomic replacement avoids a partial credential file; mkstemp uses 0600.
        fd, temporary = tempfile.mkstemp(dir=STATE_DIR)
        try:
            with os.fdopen(fd, "w") as output:
                json.dump({"controller_url": self.url, "workers": workers}, output)
            os.replace(temporary, STATE_FILE)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def register(self, name, cpu=2, memory=4096, engine="podman", state="HEALTHY"):
        if not math.isfinite(cpu) or cpu <= 0 or memory <= 0:
            raise LabError("Simulated physical CPU and memory must be positive and finite")
        workers = self.credentials()
        if name in workers:
            raise LabError(f"Worker alias {name} already exists; use heartbeat or reset")
        registration = self.request("POST", "/v1/workers/register", json={"name": f"lab-{name}", "agent_version": "0.1.0"}).json()
        w = {"id": registration["id"], "token": registration["token"],
             "cpu": cpu, "memory": memory, "engine": engine, "state": state}
        workers[name] = w
        self.save(workers)
        # Catch a misconfigured lab/controller DB pairing before fixture mutation.
        with Session(self.engine) as session:
            if session.get(Worker, UUID(w["id"])) is None:
                raise LabError("Controller and lab do not use the same database; check both configurations")
        self.heartbeat(w)
        print(f"Registered simulated {name}: {w['id']} ({cpu} CPU / {memory} MiB, {engine})")
        return w

    def heartbeat(self, w):
        engines = {"podman": w["engine"] in ("podman", "both"), "docker": w["engine"] in ("docker", "both")}
        payload = WorkerHeartbeat.model_validate({
            "agent_version": "0.1.0", "state": w["state"], "cpu_architecture": "x86_64",
            "resources": {"cpu_physical": math.ceil(w["cpu"]), "cpu_contributed": w["cpu"],
                          "cpu_allocatable": w["cpu"], "memory_physical_mb": w["memory"],
                          "memory_contributed_mb": w["memory"], "memory_allocatable_mb": w["memory"]},
            "telemetry": {"cpu_usage_percent": 0, "load_1m": 0, "memory_available_mb": w["memory"]},
            "executors": {"container": any(engines.values())}, "container_engines": engines,
        })
        return self.request("POST", f"/v1/workers/{w['id']}/heartbeat", headers=self.auth(w), json=payload.model_dump(mode="json")).json()

    @staticmethod
    def auth(w):
        return {"Authorization": f"Bearer {w['token']}"}

    def claim(self, w):
        try:
            response = self.request("POST", f"/v1/workers/{w['id']}/claim", headers=self.auth(w))
            return None if response.status_code == 204 else WorkAssignment.model_validate(response.json()).model_dump(mode="json")
        except (LabError, ValueError):
            print("Claim outcome may be ambiguous. Inspecting persisted state; the claim will NOT be retried.")
            try:
                self.print_state()
            except SQLAlchemyError:
                print("Database inspection unavailable; run make dev-state before another claim.")
            raise LabError("Claim stopped; inspect state before deciding whether to claim again") from None

    def submit(self, name="example", cpu=1, memory=1024, image="localhost/meshcompute-success:dev"):
        return self.request("POST", "/v1/jobs", json={
            "name": name, "runtime": "container", "image": image,
            "command": ["python", "/app/main.py"], "resources": {"cpu": cpu, "memory_mb": memory},
            "timeout_seconds": 60, "max_attempts": 1,
        }).json()

    def cancel(self, job):
        # 409 is a valid outcome when racing assignment; never retry it.
        try:
            response = self.client.post(f"/v1/jobs/{job['id']}/cancel")
        except httpx.RequestError:
            raise LabError("Cancellation response unavailable; inspect state") from None
        if response.status_code not in (200, 409):
            raise LabError(f"Cancellation returned HTTP {response.status_code}")
        return response.status_code

    def snapshot(self):
        with Session(self.engine) as session:
            workers = [worker_status(w, datetime.now(timezone.utc)).model_dump(mode="json")
                       for w in session.scalars(select(Worker).order_by(Worker.name, Worker.id))]
            jobs = [JobView.model_validate(j).model_dump(mode="json")
                    for j in session.scalars(select(Job).order_by(Job.created_at, Job.id))]
            attempts = [dict(row._mapping) for row in session.execute(select(
                JobAttempt.id, JobAttempt.job_id, JobAttempt.worker_id, JobAttempt.attempt_number, JobAttempt.state))]
            allocations = [dict(row._mapping) for row in session.execute(select(
                WorkerAllocation.worker_id, WorkerAllocation.job_attempt_id,
                WorkerAllocation.cpu_reserved, WorkerAllocation.memory_reserved_mb))]
        return {"workers": workers, "jobs": jobs, "attempts": attempts, "allocations": allocations}

    def print_state(self):
        print(json.dumps(self.snapshot(), indent=2, default=str))

    def confirm_reset(self, yes):
        print(f"WARNING: deletes ALL workers, jobs, attempts, allocations in local database {self.engine.url.database}, and saved lab credentials.")
        print("Stop real agents and background simulated heartbeats before reset/scenarios.")
        if not yes and input("Type RESET to continue: ") != "RESET":
            raise LabError("Reset cancelled")

    def reset(self):
        with self.engine.begin() as connection:
            # No CASCADE: unexpected tables/FKs should block, not be silently deleted.
            connection.execute(text("TRUNCATE worker_allocations, job_attempts, jobs, workers"))
        STATE_FILE.unlink(missing_ok=True)
        print("Local lab state reset (schema and migration history preserved).")


def main():
    parser = argparse.ArgumentParser(description="DEVELOPMENT ONLY: simulated workers and local database inspection")
    commands = parser.add_subparsers(dest="action", required=True)
    for name in ("wait-db", "status", "state", "seed-workers", "seed-jobs"):
        commands.add_parser(name)
    reset = commands.add_parser("reset")
    reset.add_argument("--yes", action="store_true", help="acknowledge deletion of all local lab data")
    register = commands.add_parser("register")
    register.add_argument("name")
    register.add_argument("--cpu", type=float, default=2)
    register.add_argument("--memory", type=int, default=4096, help="MiB")
    register.add_argument("--engine", choices=["podman", "docker", "both", "none"], default="podman")
    register.add_argument("--state", choices=["HEALTHY", "DEGRADED", "PAUSED", "DRAINING"], default="HEALTHY")
    heartbeat = commands.add_parser("heartbeat")
    heartbeat.add_argument("name", nargs="?", default="all")
    heartbeat.add_argument("--loop", action="store_true")
    claim = commands.add_parser("claim")
    claim.add_argument("name")
    submit = commands.add_parser("submit")
    submit.add_argument("--name", default="example")
    submit.add_argument("--cpu", type=float, default=1)
    submit.add_argument("--memory", type=int, default=1024)
    submit.add_argument("--image", default="localhost/meshcompute-success:dev")
    scenario = commands.add_parser("scenario")
    scenario.add_argument("name", nargs="?", default="all")
    scenario.add_argument("--yes", action="store_true")
    args = parser.parse_args()
    lab = None
    try:
        lab = Lab()
        if args.action == "wait-db":
            for _ in range(30):
                try:
                    with lab.engine.connect() as connection:
                        connection.execute(text("SELECT 1"))
                    print("PostgreSQL ready")
                    break
                except SQLAlchemyError:
                    time.sleep(1)
            else:
                raise LabError("PostgreSQL unavailable; check connection settings")
        elif args.action == "status":
            lab.request("GET", "/health")
            with lab.engine.connect() as connection:
                revision = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
            snapshot = lab.snapshot()
            print(f"Controller healthy; migration {revision}; " + ", ".join(f"{k}={len(v)}" for k, v in snapshot.items()))
        elif args.action == "state":
            lab.print_state()
        elif args.action == "reset":
            lab.confirm_reset(args.yes)
            lab.reset()
        elif args.action == "register":
            lab.register(args.name, args.cpu, args.memory, args.engine, args.state)
        elif args.action == "seed-workers":
            for name, cpu, memory, engine in [("A", 2, 2048, "podman"), ("B", 4, 8192, "docker"), ("C", 8, 16384, "none")]:
                lab.register(name, cpu, memory, engine)
        elif args.action == "seed-jobs":
            for number, cpu in enumerate((2, 3, 2), 1):
                j = lab.submit(f"job-{number}", cpu)
                print(f"Submitted {j['name']}: {j['id']}")
        elif args.action == "heartbeat":
            while True:
                workers = lab.credentials()
                selected = workers if args.name == "all" else {args.name: workers[args.name]}
                for name, w in selected.items():
                    result = lab.heartbeat(w)
                    print(f"{name}: {result['state']}", flush=True)
                if not args.loop:
                    break
                time.sleep(5)
        elif args.action == "claim":
            result = lab.claim(lab.credentials()[args.name])
            print(json.dumps(result, indent=2) if result else "No work (204)")
        elif args.action == "submit":
            print(json.dumps(lab.submit(args.name, args.cpu, args.memory, args.image), indent=2))
        elif args.action == "scenario":
            from devtools.scenarios import run
            run(lab, args.name, args.yes)
    except LabError as exc:
        raise SystemExit(str(exc)) from None
    except (SQLAlchemyError, httpx.RequestError, OSError, ValueError, KeyError) as exc:
        raise SystemExit(f"Lab failed ({type(exc).__name__}); check local configuration/state. Credentials and raw responses omitted.") from None
    except KeyboardInterrupt:
        print("Lab stopped")
    finally:
        if lab:
            lab.close()


if __name__ == "__main__":
    main()
