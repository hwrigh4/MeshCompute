# Local functional lab (Phase 4.5)

This is a disposable Linux development lab for the Phase 4 controller. Requests
use the real APIs, authentication, PostgreSQL transactions, and allocation ledger.
Logical workers simulate hardware and engine capabilities. No MeshCompute worker
pulls images, launches workloads, completes jobs, releases allocations, or processes
leases. The helpers are not production commands or a formal unit-test suite.

## Requirements and startup

- Linux, Python 3.12+, Make, and the project dependencies.
- PostgreSQL 17, either already running locally or via the existing Compose file.
- Optional rootless Podman (preferred) or Docker for PostgreSQL containers, engine
  validation, and example image builds. Podman Compose also needs an installed
  Compose provider. No engine is needed for simulated scheduler scenarios when
  PostgreSQL is already available.

From the repository root:

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
cp .env.example .env  # first setup only; preserve existing settings
make dev-up
```

`dev-up` starts the existing PostgreSQL Compose service, waits for database
connectivity, applies migrations, and runs the controller in the foreground.
Ctrl-C stops the controller; the database and its volume remain. In another
terminal, use `make dev-status` to check controller health, migration revision,
and row counts.

The Makefile prefers `podman compose` when Podman is installed, otherwise
`docker compose`. Override it explicitly if necessary:

```bash
make dev-up COMPOSE='podman compose'
make dev-up COMPOSE='docker compose'
make dev-up COMPOSE=podman-compose
```

No compatibility daemon, system configuration, or container engine is installed
automatically. All commands use `.venv/bin/python` by default; override `PYTHON`
if needed. Database startup also works separately:

```bash
make dev-db-up
make dev-migrate
make dev-controller
# Stop the foreground controller with Ctrl-C, then:
make dev-down
```

`dev-down` stops the Compose database without deleting its named volume. It does
not manage a controller launched in another terminal. No containerized controller
or Kubernetes deployment is required.

For an existing local PostgreSQL instance, skip `dev-db-up`/`dev-up` and set the
same connection URL for the controller and lab:

```bash
export MESHCOMPUTE_DATABASE_URL=postgresql+psycopg://meshcompute:meshcompute@127.0.0.1:5432/meshcompute_lab
export MESHCOMPUTE_LAB_URL=http://127.0.0.1:8000
make dev-controller
```

Create that database using your normal PostgreSQL administration tools first.
Database settings use the project's `.env` support. `MESHCOMPUTE_LAB_URL` is read
from the process environment. For a different controller port, set both
`CONTROLLER_PORT` for Make and `MESHCOMPUTE_LAB_URL` for the helpers. The lab accepts
only loopback controller/DB addresses and database names beginning `meshcompute`.
This guard is not proof a database is disposable: check the target before reset.

## Reset and inspect

```bash
make dev-state
make dev-reset       # warns and requires typing RESET
make dev-reset YES=1 # explicitly acknowledge deletion, useful in automation
```

**Reset deletes all workers, jobs, attempts, allocations, and saved logical-worker
credentials in this lab.** It preserves tables, migrations, and the PostgreSQL
volume. Stop real worker agents and simulated heartbeat loops first. It does not
release individual production allocations or pretend jobs have completed.

Inspection prints JSON grouped into workers, jobs, attempts, and allocations:

- Workers: effective state, capacity/contribution/reservations/allocatable CPU and
  memory, generic executor capabilities, engine metadata, and last heartbeat.
- Jobs: state, CPU/memory requests, workload metadata, and timestamps.
- Attempts: IDs, worker/job IDs, attempt number, and state.
- Allocations: worker/attempt IDs and reserved CPU/memory.

It reads explicit response/diagnostic fields, never credentials or token hashes.
Direct database access is deliberate for the local lab; no diagnostic production
API or web dashboard is added.

## Logical workers and jobs

After reset, seed three simulated workers and three jobs:

```bash
make dev-seed-workers
make dev-seed-jobs
make dev-heartbeats  # foreground heartbeat-only loop; Ctrl-C stops it
```

Default workers are A (2 CPU / 2048 MiB, Podman), B (4 CPU / 8192 MiB, Docker),
and C (8 CPU / 16384 MiB, no container capability). Default jobs request 2, 3,
and 2 CPUs with 1024 MiB each. No engine or image is required to queue these jobs.
Seed commands are deliberately not idempotent: duplicate worker aliases are
rejected, and repeated job submission creates additional jobs.

Custom fixtures can differ from the physical host:

```bash
.venv/bin/python -m devtools.lab register large --cpu 32 --memory 65536 --engine both
.venv/bin/python -m devtools.lab register paused --cpu 4 --memory 8192 --engine podman --state PAUSED
.venv/bin/python -m devtools.lab submit --name small --cpu 0.5 --memory 256
.venv/bin/python -m devtools.lab heartbeat A
.venv/bin/python -m devtools.lab claim A
make dev-state
```

Engine fixture choices are `podman`, `docker`, `both`, and `none`. Simulated
physical capacity is set to match or exceed its contribution so payloads pass
the unchanged production validation. These are **development-only claims about
hardware**, not real detection. The actual Linux worker still detects its host.

Registration saves IDs and bearer tokens to `.meshcompute-lab/workers.json`.
The directory is mode 0700, files are created atomically with mode 0600, and the
directory is gitignored. Tokens are never printed. This credential persistence
is an explicit local-development convenience; production worker behavior remains
environment-only. Do not share that directory or enable shell tracing around
credentials. Reset removes the credential file and invalidates its database
identities. Run one registration/seeding process at a time per checkout.

Claims are always explicit, single requests; heartbeat loops never claim work.
If a claim response fails after a commit, an assignment may still exist. Helpers
inspect persisted state on failure and stop; they do not retry the claim. Inspect
with `make dev-state` before any subsequent decision. This is diagnostics, not
reconciliation or recovery.

## Functional scenarios

Stop background agents and heartbeat loops before running these commands. Each
selected scenario starts with a full explicit development reset, because Phase 4
allocations never expire or complete:

```bash
make test-scheduler YES=1
make test-scheduler-basic YES=1
make test-scheduler-concurrency YES=1
.venv/bin/python -m devtools.lab scenario stale --yes
```

Omit `YES=1`/`--yes` for an interactive reset confirmation. Failures exit nonzero
and leave data in place for inspection. The final successful scenario's data is
also retained. Scenarios use real PostgreSQL and a separately running controller;
no SQLite substitute, mocked scheduler, or production validation bypass is used.

| Scenario | Assertion |
| --- | --- |
| `basic` | A=2 CPU/4096 MiB claims job 1 (2 CPU), B=4 CPU/8192 MiB claims job 2 (3 CPU), job 3 (2 CPU) stays queued |
| `oversized` | CPU-oversized and memory-oversized jobs remain queued with no reservations |
| `incompatible` | A worker without a container runtime receives no work |
| `podman-only` | Simulated healthy Podman-only worker gets a container job and a LEASED attempt |
| `docker-only` | Simulated healthy Docker-only worker does likewise |
| `stale` | Backdate a fixture heartbeat in the local DB; effective OFFLINE worker cannot claim |
| `concurrent-claim` | Four workers race for one job; exactly one attempt/allocation exists |
| `oversubscription` | Six claims for one worker cannot over-reserve CPU |
| `memory-oversubscription` | Six claims for one worker cannot over-reserve memory |
| `cancellation-race` | Eight concurrent claim/cancel races each end either CANCELLED without allocations or RUNNING with exactly one allocation |

No scenario fabricates completion, allocation release, or lease expiry. The stale
scenario's SQL timestamp edit is fixture setup only; no new production endpoint
is exposed. `RUNNING` still means assigned, not executing.

## Real Podman and Docker checks

```bash
make check-engines
.venv/bin/python -m devtools.engines --engine podman --require
.venv/bin/python -m devtools.engines --engine docker --require
```

The report distinguishes installed binaries/version, CLI runtime communication,
CLI rootless mode, selected API socket existence, `/_ping`, `/info`, API rootless
mode, and MeshCompute's actual `engine_usable()` result. CLI and API checks may
refer to different engines if your CLI uses a remote context. Socket checks are
local Unix-socket checks, matching the worker's implementation. Reports omit raw
engine info dumps. `PENDING` means validation did not pass; `--require` makes that
a nonzero exit. `PASS` confirms engine capability, not a successful workload run;
check the separate `rootless` field to confirm the preferred mode.

If Podman is installed but its rootless socket is inactive, these are explicit
operator actions (the helpers never run them):

```bash
podman --version
podman info --format json
systemctl --user enable --now podman.socket
systemctl --user status podman.socket
```

The usual rootless path is `$XDG_RUNTIME_DIR/podman/podman.sock`, falling back to
`/run/user/<uid>/podman/podman.sock`. For a non-systemd development session, the
operator can instead run a foreground service:

```bash
podman system service --time=0 "unix://$XDG_RUNTIME_DIR/podman/podman.sock"
```

See [Podman's API service documentation](https://docs.podman.io/en/latest/markdown/podman-system-service.1.html)
for rootless socket activation and [Podman info](https://docs.podman.io/en/latest/markdown/podman-info.1.html)
for runtime information. The service uses a Docker-compatible API. Do not expose
engine sockets to workloads or enable a network listener for this lab.

Docker uses `/var/run/docker.sock` by default. Rootless Docker often needs an
explicit socket override. Use the same overrides for diagnostics and the worker:

```bash
export MESHCOMPUTE_WORKER_PODMAN_SOCKET=/run/user/1000/podman/podman.sock
export MESHCOMPUTE_WORKER_DOCKER_SOCKET=/run/user/1000/docker.sock
make check-engines
# Or a one-off diagnostic:
.venv/bin/python -m devtools.engines --engine docker --socket /var/run/docker.sock
```

Docker's `/info` reports rootless mode through `SecurityOptions`; see the
[Docker Engine API documentation](https://docs.docker.com/reference/api/engine/version-history/).
No installation, service start, permission change, or engine configuration change
is performed by diagnostics. Engine absence does not block simulated scenarios.

## Example OCI workloads

Each example has an independent `Containerfile` and Python source. Images use
`python:3.12-slim`, run as an unprivileged numeric user, and require no Python
packages beyond the standard library. Build contexts contain only the example.

| Example | Default behavior / configuration |
| --- | --- |
| `success` | Prints deterministic output, exits 0 |
| `failure` | Prints an intentional stderr message, exits 7 |
| `sleep` | Sleeps 2 seconds; `--seconds` 1–60 |
| `cpu-burn` | One CPU loop for 1 second; `--seconds` 1–60 |
| `memory-hold` | Holds 16 MiB for 2 seconds; `--mib` 1–256 and `--seconds` 1–60 |
| `monte-carlo` | Seeded pi estimation, 100,000 samples, seed 42; `--samples` 1–1,000,000 and `--seed` |

Build and validate sources:

```bash
make examples-check          # bounded host Python runs; does not validate OCI builds
make examples-build-podman   # all six images
make examples-build-docker   # all six images
.venv/bin/python -m devtools.examples build --engine podman --name success
podman build -f examples/success/Containerfile -t localhost/meshcompute-success:dev examples/success
docker build -f examples/success/Containerfile -t localhost/meshcompute-success:dev examples/success
```

Build commands may fetch the base image using the developer's engine; workers do
not pull anything. Images are tagged `localhost/meshcompute-<example>:dev`. Missing
engines produce a clear pending message and nonzero build exit, not a simulated
build success. Sources can still be checked without an engine.

Optional **manual** image validation, outside MeshCompute:

```bash
podman run --rm --network=none --cpus=1 --memory=128m --pids-limit=64 \
  --read-only --cap-drop=ALL --security-opt=no-new-privileges localhost/meshcompute-success:dev
podman run --rm --network=none --cpus=1 --memory=128m --pids-limit=64 \
  --read-only --cap-drop=ALL --security-opt=no-new-privileges localhost/meshcompute-memory-hold:dev \
  python /app/main.py --mib 32 --seconds 5
```

The same commands work with `docker` in place of `podman` when supported. The
failure image should exit 7, not 0. Keep memory requests below the manual engine
limit. These are bounded development examples, not host-exhaustion tests. The
formal MeshCompute sandbox, launch, timeout, logs, and completion path remain Phase 5.

## Later physical-device testing

The logical-worker lab does not establish real host isolation, CPU/memory
enforcement, or remote-engine correctness. Later, start the existing real agent
on each Linux device using its own registration/environment and a secured
controller URL, then validate actual engine probes on those hosts. Devices still
use outbound heartbeats/claims. Keep local simulated fixtures and credentials
separate; do not run destructive lab helpers against that environment.
