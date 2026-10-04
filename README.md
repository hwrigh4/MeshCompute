# MeshCompute

MeshCompute coordinates preemptible compute contributed by providers. `PROJECT.md`
is the product and architecture source of truth.

Phases 1–4 implement the FastAPI/PostgreSQL controller, Linux worker heartbeats,
Podman/Docker capability detection, a persisted job queue, and atomic worker-pull
assignment with resource reservations. Phase 5 adds restricted container execution,
bounded results, and atomic reservation release through `mesh-worker work-once`.

The Phase 4.5 [local functional lab](docs/development.md) adds Make commands,
simulated logical workers, repeatable scheduler/concurrency scenarios, state
inspection, real-engine diagnostics, and buildable OCI examples, organized under
[local_test/](local_test/README.md). The main validation command is `make test-local`;
follow the lab's three-terminal setup and build example images explicitly first.
`make test-scheduler` remains separate and requires a destructive local reset
confirmation. For real end-to-end execution, use `make test-execution`; it runs
its own one-shot worker and does not require the separate heartbeat terminal.
See the [Phase 5 workflow](docs/development.md#phase-5-container-execution).

## Run locally

Requires Python 3.12+ and Docker Compose (or an existing PostgreSQL database).
Run commands from the repository root:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
cp .env.example .env
docker compose up -d --wait postgres
alembic upgrade head
uvicorn controller.main:app --reload --host 127.0.0.1 --port 8000
```

`MESHCOMPUTE_DATABASE_URL` configures the SQLAlchemy PostgreSQL connection using
the `postgresql+psycopg://` driver. Environment variables override `.env` values.
The default matches the development database in `docker-compose.yml`.
Migrations run explicitly; the application does not create tables at startup.

The development database is bound to localhost with development-only credentials.
Registration and listing are currently unauthenticated: keep this foundation on
a trusted local network. Worker-specific heartbeats require the registration
bearer token. Use HTTPS when connecting outside localhost to protect that token
in transit.

## Smoke check

```bash
curl --fail http://127.0.0.1:8000/health
curl --fail -X POST http://127.0.0.1:8000/v1/workers/register \
  -H 'Content-Type: application/json' \
  -d '{"name":"worker-a","agent_version":"0.1.0"}'
curl --fail 'http://127.0.0.1:8000/v1/workers?limit=100&offset=0'
```

- `GET /health` returns `{"status":"ok"}` when PostgreSQL is reachable, or HTTP
  503 when unavailable. It checks connectivity, not migration status.
- `POST /v1/workers/register` returns HTTP 201 with a new UUID, name, agent version,
  `REGISTERING` state, timestamps, and a one-time bearer `token`. Supply the token
  to the worker through its process environment. Only a SHA-256 digest of the
  random token is persisted by the controller; neither component writes the
  plaintext token to disk or logs.
  Each registration creates a separate identity; names need not be unique.
- `GET /v1/workers` returns identities without tokens or token hashes, ordered by
  creation time and ID. `limit` defaults to 100 (maximum 1000); `offset` defaults
  to zero. Registration does not imply that a worker is healthy or schedulable.
- `/docs` provides the OpenAPI interface.

To check migration/model consistency, run `alembic check`. To stop the local
database while preserving its data, run `docker compose down`.

No automated test suite is included at this phase, per `AGENTS.md` and `PROJECT.md`.

## Register and start a Linux worker

The agent uses an existing identity; it does not register automatically or save
credentials locally. This example captures the registration response in memory
without printing the token or putting it in shell history (requires `curl` and
the activated Python environment). Do not enable shell tracing (`set -x`).

```bash
export MESHCOMPUTE_WORKER_CONTROLLER_URL=http://127.0.0.1:8000
registration=$(curl --fail --silent --show-error \
  "$MESHCOMPUTE_WORKER_CONTROLLER_URL/v1/workers/register" \
  -H 'Content-Type: application/json' \
  -d '{"name":"worker-a","agent_version":"0.1.0"}')
export MESHCOMPUTE_WORKER_ID=$(printf '%s' "$registration" | python -c \
  'import json,sys; print(json.load(sys.stdin)["id"])')
export MESHCOMPUTE_WORKER_TOKEN=$(printf '%s' "$registration" | python -c \
  'import json,sys; print(json.load(sys.stdin)["token"])')
unset registration
export MESHCOMPUTE_WORKER_CPU_LIMIT=2
export MESHCOMPUTE_WORKER_MEMORY_LIMIT_MB=4096
mesh-worker
# Alternatively: python -m worker.main
```

Configuration comes from environment variables, with prefix `MESHCOMPUTE_WORKER_`:

| Suffix | Default | Meaning |
| --- | --- | --- |
| `CONTROLLER_URL` | `http://127.0.0.1:8000` | Controller HTTP(S) base URL |
| `ID` | required | Registered worker UUID |
| `TOKEN` | required | Registration bearer token, held only in memory |
| `CPU_LIMIT` | `0` | Contributed logical CPUs; fractional values allowed |
| `MEMORY_LIMIT_MB` | `0` | Contributed memory in MiB (1,048,576 bytes) |
| `DOCKER_SOCKET` | `/var/run/docker.sock` | Local Docker Engine Unix socket |
| `PODMAN_SOCKET` | `$XDG_RUNTIME_DIR/podman/podman.sock` | Local Podman API socket; falls back to `/run/user/<uid>/podman/podman.sock` |
| `CONTAINER_ENGINE` | `auto` | `auto`, `podman`, or `docker`; required engine readiness |

For 4 CPUs and 8 GiB, set `CPU_LIMIT=4` and `MEMORY_LIMIT_MB=8192` using the full
environment names above. Limits above detected host capacity are capped at that
capacity; negative and nonfinite CPU limits are rejected. Zero contribution is
valid and does not itself make a worker unhealthy. Restart the agent to change
limits; local dynamic provider controls are a later phase.

The worker does not read `.env` or create a credential file. Keep the token out
of command-line arguments and logs; unset `MESHCOMPUTE_WORKER_TOKEN` when done.
If the token is lost, register a new identity. Token rotation is not implemented.

## Heartbeats and resource reporting

The worker sends `POST /v1/workers/{worker_id}/heartbeat` immediately and normally
every five seconds with `Authorization: Bearer <token>`. Network/request failures,
HTTP 408, 429, and 5xx responses retry with bounded request timeouts. Other HTTP
errors (including 400, 401, 403, 404, and 422) stop the agent with a nonzero exit
and a concise configuration/authentication/protocol error. Logs omit response
bodies and credentials. Redirects are not followed and stop the agent too. SIGINT/SIGTERM cancels
in-flight work and closes HTTP clients cleanly. Stopping the agent lets its
heartbeat expire. This default heartbeat-only mode never claims or executes work.

The payload and worker listing keep `resources`, `telemetry`, and `executors`
separate. Resource fields include:

- `cpu_physical`: detected logical CPU capacity; `cpu_physical_cores` reports
  physical core count if available.
- `cpu_contributed`, `cpu_reserved`, `cpu_allocatable`.
- `memory_physical_mb`, `memory_contributed_mb`, `memory_reserved_mb`,
  `memory_allocatable_mb`.

Controller responses derive reserved values from PostgreSQL `worker_allocations`;
allocatable values are contribution minus reservations, floored at zero.
Heartbeat resource fields describe the worker's local view and never overwrite
the controller's ledger. Heartbeats do not maintain a second local allocation
ledger; the controller remains authoritative, including during `work-once`.
Capacity is independent of CPU usage or available memory.
Telemetry reports `cpu_usage_percent`, `load_1m` when available, and
`memory_available_mb`. CPU architecture is reported separately.

`executors.container` is true when either configured Podman or Docker socket
responds successfully to `/_ping` and `/info` and reports a Linux runtime.
`container_engines` reports `podman` and `docker` usability separately. Both may
be true. Missing sockets, permission errors, or daemon failures make that engine
unavailable. The probe runs each heartbeat cycle without pulling images or
creating containers. A successful probe does not guarantee a future workload's success.

Podman's Docker-compatible API allows the same read-only probe for both engines.
The default Podman socket targets the current user's rootless service; rootless
Podman is preferred for execution. The agent does not install
Podman, start its service, or change system configuration. See the
[Podman API service documentation](https://docs.podman.io/en/latest/markdown/podman-system-service.1.html)
for socket setup. A different socket can be configured explicitly.

With `CONTAINER_ENGINE=auto`, either usable engine makes the agent healthy.
Explicit `podman` or `docker` requires that engine to be usable; otherwise the
agent reports `DEGRADED`, even if the other engine is available. Both engines'
actual availability is still advertised. No execution selection policy is implemented.
Engine metadata is optional for older heartbeat clients; omitted metadata is
stored as unknown (`null`). When supplied, it must agree with `executors.container`.

## Worker states

| State | Meaning |
| --- | --- |
| `REGISTERING` | Registered identity with no heartbeat yet |
| `HEALTHY` | Fresh heartbeat and healthy runtime; capacity may be zero |
| `DEGRADED` | Required runtime/dependency failure or heartbeat age from 15 through 30 seconds |
| `DRAINING` | Advertised participation state: no new work, let existing work finish |
| `PAUSED` | Advertised participation state: no new work |
| `OFFLINE` | Last heartbeat is more than 30 seconds old |

The agent currently reports `HEALTHY` or `DEGRADED`. `PAUSED` and `DRAINING` are
represented in the protocol, but local control commands are not implemented yet.
Fresh heartbeats preserve those participation states. Staleness overrides them
in listing responses. A fresh heartbeat recovers an offline worker, but cannot
make a worker reporting runtime failure or no usable executors healthy. The
controller checks advertised executor availability generically; only the current
agent knows which local engines back its runtime.

Freshness is calculated when workers are listed (and when a heartbeat is returned),
using the controller's UTC receive time: under 15 seconds is fresh, 15–30 seconds
is degraded, over 30 is offline. The database stores the latest reported state
and heartbeat time; stale-state changes do not require a background task or database
writes. Workers that never heartbeat remain `REGISTERING`.
The API uses `effective_worker_state` in `controller/services/worker_health.py`;
scheduling uses the same function rather than persisted state alone.

To observe this locally, run the worker, list `/v1/workers`, stop the worker, and
list again after 15 and 31 seconds. With automatic engine selection, two unavailable
engine sockets produce `DEGRADED`; either usable Linux engine produces `HEALTHY`.
Heartbeat requests with missing, incorrect, or another worker's token return 401.

## Job queue (Phase 3)

Requester job endpoints are unauthenticated in this local MVP. Anyone with access
to the controller can submit, inspect, or cancel queued jobs. Keep the controller
on a trusted local network. No accounts or requester authorization are implemented.

Submit a container workload:

```bash
curl --fail -X POST http://127.0.0.1:8000/v1/jobs \
  -H 'Content-Type: application/json' \
  -d '{
    "name": "monte-carlo",
    "runtime": "container",
    "image": "python:3.12-slim",
    "command": ["python", "-c", "print(sum(i*i for i in range(1000)))"],
    "resources": {"cpu": 0.5, "memory_mb": 2048},
    "timeout_seconds": 600,
    "max_attempts": 3
  }'
```

The response is HTTP 201 with a UUID, `QUEUED` state, and timestamps. Resource
requests appear in responses as `cpu_requested` and `memory_requested_mb`.
`started_at` and `completed_at` initially remain null. Commands are stored as an
argument array without trimming or shell interpretation. The image is only a
reference: the controller does not pull it or verify its contents.

Names and image references must be nonempty (maximum 128 and 2048 characters).
Only `container` is accepted as runtime. Commands require at least one string
element. CPU must be positive and finite; fractional CPUs are accepted. Memory
(MiB), timeout (seconds), and maximum attempts must be positive integers, at most
2,147,483,647. Invalid requests return 422, including NaN/infinity. Resource
requests are not compared with current worker capacity; a job can be queued even
with no workers. `max_attempts` is stored for future recovery policy; Phase 4
creates attempt 1 on assignment and has no retries.

List, retrieve, and cancel jobs (replace the UUID with one returned by submission):

```bash
curl --fail 'http://127.0.0.1:8000/v1/jobs?limit=100&offset=0'
job_id=REPLACE_WITH_JOB_UUID
curl --fail "http://127.0.0.1:8000/v1/jobs/$job_id"
curl --fail -X POST "http://127.0.0.1:8000/v1/jobs/$job_id/cancel"
```

Listing uses oldest creation time first, then ID; `limit` defaults to 100
(maximum 1000), and `offset` defaults to zero. Retrieval and cancellation return
404 for an unknown UUID. Cancellation returns HTTP 200, transitions `QUEUED` to
`CANCELLED`, and sets `completed_at`. Repeating cancellation returns the same job
without changing timestamps. Other states return 409; their history is preserved.

| Job state | Meaning |
| --- | --- |
| `QUEUED` | Persisted and waiting; all new jobs begin here |
| `RUNNING` | Assigned to a worker; does not mean a container is executing in Phase 4 |
| `SUCCEEDED` | Successful completion (reserved for later phases) |
| `FAILED` | Unsuccessful completion (reserved for later phases) |
| `CANCELLED` | Requester cancelled the queued job |

The `jobs` table uses a string runtime, JSONB command array, and a nullable
`owner_id` for future requester identity. This field is not accepted from API
clients. Submitting a job creates no assignment until a worker claims it.

Against a disposable database, verify the Phase 3 migration with `alembic upgrade
head`, `alembic check`, `alembic downgrade 0002`, `alembic upgrade head`, and
`alembic check`. Downgrading to Phase 2 drops jobs; worker data is preserved.

## Worker claims and reservations (Phase 4)

Keep `mesh-worker` running to send heartbeats. In another terminal with the same
worker environment, explicitly ask for one assignment:

```bash
mesh-worker claim
```

This prints an assignment or `No work (204)` and exits. It performs one request
and never executes the command. The normal heartbeat loop does not automatically
claim work in this phase. A failed claim response may conceal a committed assignment;
the command does not automatically retry. Recovery and reconciliation are later phases.

The equivalent authenticated API request is:

```bash
curl --include --fail -X POST \
  "$MESHCOMPUTE_WORKER_CONTROLLER_URL/v1/workers/$MESHCOMPUTE_WORKER_ID/claim" \
  -H "Authorization: Bearer $MESHCOMPUTE_WORKER_TOKEN"
```

No job ID is supplied. The controller returns HTTP 204 with an empty body if the
worker is ineligible or there is no fitting job. An assignment returns HTTP 200:

```json
{
  "attempt_id": "<uuid>",
  "job_id": "<uuid>",
  "runtime": "container",
  "image": "python:3.12-slim",
  "command": ["python", "-c", "print(1)"],
  "resources": {"cpu": 2, "memory_mb": 2048},
  "timeout_seconds": 600
}
```

Requester runtime stays `container`; Podman and Docker are worker implementation
details, never scheduler placement keys. Eligibility requires effective current
state `HEALTHY`, a matching advertised runtime, and enough unreserved CPU and
memory. Stale, degraded, paused, draining, and never-heartbeating workers receive
no work. Utilization telemetry does not affect eligibility.

For worker-pull placement, the policy is **oldest compatible fitting job first**,
then job ID to break creation-time ties. Oversized or incompatible earlier jobs
are skipped. This is the simple pull-oriented alternative to global best-fit
worker selection: the controller does not hold work for another worker or push
assignments. A locked job may be skipped by a concurrent claim.

Each claim transaction:

1. Locks the requesting worker row, then reads controller allocations.
2. Checks effective health, generic runtime compatibility, and available resources.
3. Selects a queued job with `FOR UPDATE SKIP LOCKED`.
4. Creates a `LEASED` attempt and its allocation, and changes the job to `RUNNING`.
5. Commits all changes together before returning the assignment.

The worker lock serializes simultaneous claims for that worker; heartbeats share
that lock when changing contributions. The job lock prevents duplicate assignment
and is the same lock used by requester cancellation. If cancellation wins, no
attempt/reservation is created. If assignment wins, cancellation sees `RUNNING`
and returns 409. Errors before commit roll back all assignment changes.

`job_attempts` references job and worker identities and uniquely numbers attempts
per job starting at 1. `worker_allocations` uses the attempt ID as its primary key,
allowing at most one allocation per attempt. Its CPU and memory values are the
controller's reservation source of truth. The queue index supports state/order
selection. Reserved CPU uses PostgreSQL numeric/decimal arithmetic to avoid
fractional CPU summation drift; memory is stored in integer MiB. Allocation and
attempt worker indexes support worker lookups. The
attempt-number unique index also covers lookups by job ID.

In Phase 4, `RUNNING` means assigned and attempt `LEASED` means reserved. Job and
attempt start timestamps, attempt completion, and lease expiration remain null.
A diagnostic `claim` still launches no container. Phase 5 `work-once` adds
execution and result-driven resource release. Neither path renews/expires leases,
retries jobs, or recovers disappeared workers. Reservations persist across
controller and agent restarts until a terminal result is recorded.
Reducing contribution below existing reservations leaves zero allocatable capacity
and blocks further claims; it does not preempt or release existing assignments.

For a local smoke check, register two logical workers, advertise 2 CPU/4096 MiB
and 4 CPU/8192 MiB, submit fitting and oversized jobs, and invoke claims using
each worker's token. Inspect `/v1/workers` for controller-side reserved/allocatable
resources and `/v1/jobs/{id}` for state. Multiple logical workers may run on one
development machine with independent IDs and environment settings. These simulated
checks use engine APIs only for availability detection; a local
simulation may also send authenticated heartbeats directly. Phase 5 execution
checks instead launch real restricted containers.

Historical Phase 4 migration check, on a disposable database without execution
history (Phase 5 migration instructions are in the development guide):

```bash
alembic upgrade 0004
alembic downgrade 0003
alembic upgrade head
alembic check
```

Downgrading drops attempt/allocation data and engine metadata, preserving workers
and jobs. Jobs assigned by Phase 4 are reset to `QUEUED` during this explicit
downgrade because no workload has executed; this is not automatic retry behavior.
