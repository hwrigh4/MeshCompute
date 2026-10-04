"""Repeatable functional scenarios using the real API and PostgreSQL ledger."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import threading
from uuid import UUID

from sqlalchemy import update

from controller.models.worker import Worker
from local_test.lab import LabError


def check(condition, message):
    if not condition:
        raise LabError(f"Scenario failed: {message}. State preserved; run make dev-state.")


def parallel(functions):
    barrier = threading.Barrier(len(functions))

    def invoke(function):
        barrier.wait(timeout=10)
        return function()

    with ThreadPoolExecutor(max_workers=len(functions)) as pool:
        return list(pool.map(invoke, functions))


def job_state(lab, job):
    return lab.request("GET", f"/v1/jobs/{job['id']}").json()["state"]


def ledger(lab, worker):
    snapshot = lab.snapshot()
    row = next(w for w in snapshot["workers"] if w["id"] == worker["id"])
    allocations = [a for a in snapshot["allocations"] if str(a["worker_id"]) == worker["id"]]
    cpu = sum((a["cpu_reserved"] for a in allocations), Decimal(0))
    memory = sum(a["memory_reserved_mb"] for a in allocations)
    resources = row["resources"]
    check(cpu <= Decimal(str(resources["cpu_contributed"])), "CPU oversubscription")
    check(memory <= resources["memory_contributed_mb"], "memory oversubscription")
    check(float(cpu) == resources["cpu_reserved"] and memory == resources["memory_reserved_mb"], "worker output differs from persisted allocations")
    return cpu, memory


def basic(lab):
    a = lab.register("A", 2, 4096, "podman")
    b = lab.register("B", 4, 8192, "docker")
    jobs = [lab.submit(f"job-{i}", cpu) for i, cpu in enumerate((2, 3, 2), 1)]
    check(lab.claim(a)["job_id"] == jobs[0]["id"], "A must receive oldest fitting job 1")
    check(lab.claim(b)["job_id"] == jobs[1]["id"], "B must receive job 2")
    check(lab.claim(a) is None and lab.claim(b) is None, "job 3 cannot fit remaining capacity")
    check(job_state(lab, jobs[2]) == "QUEUED", "job 3 must remain queued")
    check(ledger(lab, a) == (2, 1024) and ledger(lab, b) == (3, 1024), "incorrect basic reservations")
    print("A -> job 1; B -> job 2; job 3 QUEUED (reservations remain until explicit reset).")


def oversized(lab):
    w = lab.register("A", 2, 4096)
    jobs = [lab.submit("too-much-cpu", 3), lab.submit("too-much-memory", 1, 8192)]
    check(lab.claim(w) is None, "oversized requests must not fit")
    check(all(job_state(lab, j) == "QUEUED" for j in jobs), "oversized job left QUEUED")
    check(ledger(lab, w) == (0, 0), "oversized jobs reserved resources")


def incompatible(lab):
    w = lab.register("no-runtime", 4, 8192, "none")
    j = lab.submit()
    check(lab.claim(w) is None and job_state(lab, j) == "QUEUED", "worker without container runtime received work")


def engine_only(lab, engine):
    w = lab.register(engine, engine=engine)
    j = lab.submit()
    assignment = lab.claim(w)
    check(assignment and assignment["job_id"] == j["id"] and assignment["runtime"] == "container", f"{engine}-only worker not assigned container job")
    check(job_state(lab, j) == "RUNNING" and ledger(lab, w) == (1, 1024), "assignment not persisted")
    attempts = lab.snapshot()["attempts"]
    check(len(attempts) == 1 and attempts[0]["state"] == "LEASED" and attempts[0]["attempt_number"] == 1, "incorrect initial attempt")


def stale(lab):
    w = lab.register("stale")
    j = lab.submit()
    # Fixture-only clock manipulation: no production endpoint or recovery behavior.
    with lab.engine.begin() as connection:
        connection.execute(update(Worker).where(Worker.id == UUID(w["id"])).values(
            last_heartbeat=datetime.now(timezone.utc) - timedelta(seconds=31)))
    check(lab.snapshot()["workers"][0]["state"] == "OFFLINE", "effective state must be OFFLINE")
    check(lab.claim(w) is None and job_state(lab, j) == "QUEUED", "stale worker received work")


def concurrent_claim(lab):
    workers = [lab.register(f"competitor-{i}") for i in range(4)]
    j = lab.submit()
    claims = parallel([lambda w=w: lab.claim(w) for w in workers])
    check(sum(a is not None for a in claims) == 1, "exactly one concurrent claim must win")
    state = lab.snapshot()
    check(len(state["attempts"]) == len(state["allocations"]) == 1, "duplicate attempt/allocation")
    check(job_state(lab, j) == "RUNNING", "claimed job not RUNNING")


def oversubscription(lab, memory=False):
    w = lab.register("busy", 4, 4096)
    for i in range(6):
        lab.submit(f"competing-job-{i}", 1 if memory else 3, 3072 if memory else 1024)
    claims = parallel([lambda: lab.claim(w) for _ in range(6)])
    check(sum(a is not None for a in claims) == 1, "only one large job should fit")
    check(ledger(lab, w) == ((1, 3072) if memory else (3, 1024)), "incorrect reservation total")


def cancellation_race(lab):
    w = lab.register("racer", 16, 32768)
    # Check both boundary outcomes before the nondeterministic concurrent races.
    cancelled = lab.submit("cancellation-wins")
    check(lab.cancel(cancelled) == 200 and lab.claim(w) is None, "cancellation winner was assigned")
    assigned = lab.submit("assignment-wins")
    check(lab.claim(w)["job_id"] == assigned["id"] and lab.cancel(assigned) == 409,
          "assignment winner did not reject later cancellation")
    outcomes = {"CANCELLED": 0, "RUNNING": 0}
    for i in range(8):
        lab.heartbeat(w)
        j = lab.submit(f"race-{i}")
        assignment, cancel_status = parallel([lambda: lab.claim(w), lambda: lab.cancel(j)])
        state = job_state(lab, j)
        check(state in outcomes, "race left job in an unexpected state")
        outcomes[state] += 1
        snapshot = lab.snapshot()
        attempts = [a for a in snapshot["attempts"] if str(a["job_id"]) == j["id"]]
        allocations = [a for a in snapshot["allocations"] if str(a["job_attempt_id"]) in {str(t["id"]) for t in attempts}]
        if state == "CANCELLED":
            check(assignment is None and cancel_status == 200 and not attempts and not allocations, "cancelled job also assigned")
        else:
            check(state == "RUNNING" and assignment and assignment["job_id"] == j["id"] and cancel_status == 409
                  and len(attempts) == len(allocations) == 1, "invalid claim/cancellation outcome")
    ledger(lab, w)
    print(f"Both ordered outcomes verified; concurrent race outcomes: {outcomes}")


SCENARIOS = {
    "basic": basic,
    "oversized": oversized,
    "incompatible": incompatible,
    "podman-only": lambda lab: engine_only(lab, "podman"),
    "docker-only": lambda lab: engine_only(lab, "docker"),
    "stale": stale,
    "concurrent-claim": concurrent_claim,
    "oversubscription": oversubscription,
    "memory-oversubscription": lambda lab: oversubscription(lab, memory=True),
    "cancellation-race": cancellation_race,
}


def run(lab, name, yes):
    if name == "all":
        selected = list(SCENARIOS)
    elif name == "concurrency":
        selected = ["concurrent-claim", "oversubscription", "memory-oversubscription", "cancellation-race"]
    elif name in SCENARIOS:
        selected = [name]
    else:
        raise LabError("Unknown scenario; choose all, concurrency, or " + ", ".join(SCENARIOS))
    lab.confirm_reset(yes)
    print("Real PostgreSQL + controller; SIMULATED worker hardware/engine capabilities. Reset before each scenario.")
    for scenario in selected:
        print(f"\n--- {scenario} ---", flush=True)
        lab.reset()
        SCENARIOS[scenario](lab)
        print(f"PASS {scenario}", flush=True)
    print(f"PASS {len(selected)} scenarios. Final scenario state retained for inspection.")
