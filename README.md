# MeshCompute

MeshCompute coordinates preemptible compute contributed by providers. `PROJECT.md`
is the product and architecture source of truth.

Phases 1–3 implement the FastAPI/PostgreSQL controller and a Linux worker agent
with authenticated heartbeats, resource reporting, and Docker availability
detection, plus a persisted requester job queue. Jobs are not scheduled or executed;
worker claims, reservations, and workload execution are not implemented.

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

For 4 CPUs and 8 GiB, set `CPU_LIMIT=4` and `MEMORY_LIMIT_MB=8192` using the full
environment names above. Limits above detected host capacity are capped at that
capacity; negative and nonfinite CPU limits are rejected. Zero contribution is
valid and does not itself make a worker unhealthy. Restart the agent to change
limits in Phase 2; local dynamic provider controls are a later phase.

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
heartbeat expire; no execution or preemption is involved.

The payload and worker listing keep `resources`, `telemetry`, and `executors`
separate. Resource fields include:

- `cpu_physical`: detected logical CPU capacity; `cpu_physical_cores` reports
  physical core count if available.
- `cpu_contributed`, `cpu_reserved`, `cpu_allocatable`.
- `memory_physical_mb`, `memory_contributed_mb`, `memory_reserved_mb`,
  `memory_allocatable_mb`.

Reserved values must be zero and allocatable values equal contributed values in
Phase 2. This is capacity accounting, independent of CPU usage or available memory.
Telemetry reports `cpu_usage_percent`, `load_1m` when available, and
`memory_available_mb`. CPU architecture is reported separately.

`executors.container` is true only when the configured Docker socket responds
successfully to `/_ping` and `/info` and reports a Linux runtime. Missing Docker,
permission errors, or daemon failures produce false and a `DEGRADED` heartbeat.
The probe runs each cycle so daemon recovery is detected. It does not create
containers or establish that a particular future workload will execute successfully.

## Worker states

| State | Meaning |
| --- | --- |
| `REGISTERING` | Registered identity with no heartbeat yet |
| `HEALTHY` | Fresh heartbeat and healthy runtime; capacity may be zero |
| `DEGRADED` | Docker/dependency failure or heartbeat age from 15 through 30 seconds |
| `DRAINING` | Advertised participation state: no new work, let existing work finish |
| `PAUSED` | Advertised participation state: no new work |
| `OFFLINE` | Last heartbeat is more than 30 seconds old |

The agent currently reports `HEALTHY` or `DEGRADED`. `PAUSED` and `DRAINING` are
represented in the protocol, but local control commands are not implemented yet.
Fresh heartbeats preserve those participation states. Staleness overrides them
in listing responses. A fresh heartbeat recovers an offline worker, but cannot
make a worker reporting runtime failure or no usable executors healthy. The
controller checks advertised executor availability generically; only the current
agent's runtime probe is Docker-specific.

Freshness is calculated when workers are listed (and when a heartbeat is returned),
using the controller's UTC receive time: under 15 seconds is fresh, 15–30 seconds
is degraded, over 30 is offline. The database stores the latest reported state
and heartbeat time; stale-state changes do not require a background task or database
writes. Workers that never heartbeat remain `REGISTERING`.
The API uses `effective_worker_state` in `controller/services/worker_health.py`;
future scheduling must use the same function rather than persisted state alone.

To observe this locally, run the worker, list `/v1/workers`, stop the worker, and
list again after 15 and 31 seconds. An inaccessible Docker socket should produce
`DEGRADED` immediately; a healthy Linux Docker daemon should produce `HEALTHY`.
Heartbeat requests with missing, incorrect, or another worker's token return 401.

For migration verification against a disposable database, run `alembic upgrade head`,
`alembic check`, `alembic downgrade 0001`, then `alembic upgrade head` and
`alembic check`. Downgrading removes Phase 2 telemetry/capabilities/resources and
resets states to `REGISTERING`, preserving identity and token hashes.

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
with no workers. `max_attempts` is stored for later phases; there are no attempts
or retry behavior yet.

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
| `RUNNING` | Execution in progress (reserved for later phases) |
| `SUCCEEDED` | Successful completion (reserved for later phases) |
| `FAILED` | Unsuccessful completion (reserved for later phases) |
| `CANCELLED` | Requester cancelled the queued job |

The `jobs` table uses a string runtime, JSONB command array, and a nullable
`owner_id` for future requester identity. This field is not accepted from API
clients. No worker assignment or attempt records are created.

Against a disposable database, verify the Phase 3 migration with `alembic upgrade
head`, `alembic check`, `alembic downgrade 0002`, `alembic upgrade head`, and
`alembic check`. Downgrading to Phase 2 drops jobs; worker data is preserved.
