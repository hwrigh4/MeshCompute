# MeshCompute

MeshCompute coordinates preemptible compute contributed by providers. `PROJECT.md`
is the product and architecture source of truth.

Phase 1 implements a FastAPI controller, PostgreSQL persistence, SQLAlchemy models,
Alembic migrations, environment configuration, and basic worker identities.
The worker, CLI, scheduler, executor, and example directories are scaffolding only.
Worker agents, heartbeats, resource advertisement, jobs, and execution belong to
later phases.

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
a trusted local network. A registration issues a credential for future worker
authentication; Phase 1 has no authenticated worker operations yet.

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
  `REGISTERING` state, timestamps, and a one-time bearer `token`. Save the ID and
  token securely. Only a SHA-256 digest of the random token is persisted.
  Each registration creates a separate identity; names need not be unique.
- `GET /v1/workers` returns identities without tokens or token hashes, ordered by
  creation time and ID. `limit` defaults to 100 (maximum 1000); `offset` defaults
  to zero. Registration does not imply that a worker is healthy or schedulable.
- `/docs` provides the OpenAPI interface.

To check migration/model consistency, run `alembic check`. To stop the local
database while preserving its data, run `docker compose down`.

No automated test suite is included at this phase, per `AGENTS.md` and `PROJECT.md`.
