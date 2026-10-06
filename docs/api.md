# MeshCompute API Reference

This document summarizes the current controller HTTP API. The controller also exposes FastAPI-generated interactive documentation at `/docs` and the machine-readable OpenAPI schema at `/openapi.json`.

> Security note: requester-facing job/attempt read APIs are still local-MVP interfaces and are not authenticated. Worker mutation APIs require a worker bearer token. Keep the controller on a trusted network or localhost unless additional authentication/TLS is provided.

## Conventions

- Base URL in local development: `http://127.0.0.1:8000`
- JSON request/response bodies unless noted otherwise.
- Worker-authenticated endpoints use:
  `Authorization: Bearer <worker-token>`
- UUIDs are represented as strings.
- Timestamps are ISO 8601 datetimes.
- Validation failures return HTTP 422.
- Unknown resources generally return HTTP 404.
- State conflicts generally return HTTP 409.

## Health and metrics

### GET /health

Checks controller database connectivity.

Response:

```json
{"status":"ok"}
```

Returns HTTP 503 if PostgreSQL is unavailable.

### GET /metrics

Prometheus-compatible controller metrics.

The endpoint includes current worker/resource and job gauges plus scheduler, attempt, execution, lease, recovery, preemption, and reconciliation counters/histograms.

The response is Prometheus text format, not JSON.

## Workers

### POST /v1/workers/register

Registers a new worker identity.

Request:

```json
{
  "name": "worker-a",
  "agent_version": "0.1.0"
}
```

Response: HTTP 201.

The response contains the worker UUID and a one-time bearer token. The plaintext token is not persisted by the controller and is not returned by later worker-list endpoints.

### GET /v1/workers

Lists workers.

Query parameters:

- `limit`: 1–1000, default 100
- `offset`: non-negative integer, default 0

Each worker includes identity/state plus the latest resource, telemetry, executor, and container-engine information when a heartbeat has been received.

Controller responses derive reserved/allocatable resources from the allocation ledger.

### POST /v1/workers/{worker_id}/heartbeat

Worker-authenticated.

Updates heartbeat, provider participation state, resource contribution, telemetry, executor capability, engine availability, and bounded active-attempt observations.

Representative request:

```json
{
  "agent_version": "0.1.0",
  "state": "HEALTHY",
  "cpu_architecture": "x86_64",
  "resources": {
    "cpu_physical": 8,
    "cpu_physical_cores": 4,
    "cpu_contributed": 4,
    "cpu_reserved": 0,
    "cpu_allocatable": 4,
    "memory_physical_mb": 16384,
    "memory_contributed_mb": 8192,
    "memory_reserved_mb": 0,
    "memory_allocatable_mb": 8192
  },
  "telemetry": {
    "cpu_usage_percent": 10,
    "load_1m": 0.5,
    "memory_available_mb": 12000
  },
  "executors": {
    "container": true
  },
  "container_engines": {
    "podman": true,
    "docker": false
  },
  "active_attempt_ids": []
}
```

Worker-reported reserved fields are not the controller allocation authority.

### POST /v1/workers/{worker_id}/claim

Worker-authenticated.

Requests one compatible queued job for the worker.

No job ID is supplied by the worker. Scheduling remains controller-owned.

Returns:

- HTTP 200 with a `WorkAssignment`
- HTTP 204 when no work is available or the worker is ineligible

Representative response:

```json
{
  "attempt_id": "00000000-0000-0000-0000-000000000000",
  "job_id": "00000000-0000-0000-0000-000000000000",
  "runtime": "container",
  "image": "localhost/meshcompute-success:dev",
  "command": ["python", "/app/main.py"],
  "resources": {
    "cpu": 0.5,
    "memory_mb": 128
  },
  "timeout_seconds": 60,
  "lease_expires_at": "2026-10-06T17:00:30Z",
  "lease_duration_seconds": 30,
  "renew_after_seconds": 10
}
```

A successful claim atomically creates a LEASED attempt and resource allocation and moves the job to RUNNING.

## Jobs

### POST /v1/jobs

Submits a container job.

Request:

```json
{
  "name": "monte-carlo",
  "runtime": "container",
  "image": "localhost/meshcompute-monte-carlo:dev",
  "command": ["python", "/app/main.py"],
  "resources": {
    "cpu": 0.5,
    "memory_mb": 128
  },
  "timeout_seconds": 60,
  "max_attempts": 3
}
```

Response: HTTP 201 with a `JobView`.

Current requester runtime is only `container`.

### GET /v1/jobs

Lists jobs ordered by creation time and ID.

Query parameters:

- `limit`: 1–1000, default 100
- `offset`: non-negative integer, default 0

### GET /v1/jobs/{job_id}

Returns one job.

### POST /v1/jobs/{job_id}/cancel

Cancels a QUEUED job.

Behavior:

- QUEUED -> CANCELLED
- repeated cancellation is idempotent
- non-QUEUED jobs return HTTP 409
- unknown job returns HTTP 404

Running-job cancellation is not currently a requester feature.

## Attempts

### GET /v1/attempts/{attempt_id}

Returns attempt state and execution result metadata.

Representative fields:

- `id`
- `job_id`
- `worker_id`
- `attempt_number`
- `state`
- `container_engine`
- `created_at`
- `started_at`
- `completed_at`
- `lease_expires_at`
- `exit_code`
- `failure_reason`
- bounded stdout/stderr tails
- truncation flags

Attempt states currently include:

- LEASED
- RUNNING
- SUCCEEDED
- FAILED
- TIMED_OUT
- LOST
- PREEMPTED

## Worker attempt lifecycle

All endpoints below are worker-authenticated and validate that the attempt belongs to the authenticated worker.

### POST /v1/workers/{worker_id}/attempts/{attempt_id}/start

Marks a live LEASED attempt RUNNING.

Request:

```json
{
  "container_engine": "podman"
}
```

The same worker may repeat an identical start request idempotently.

Expired leases cannot be started.

### POST /v1/workers/{worker_id}/attempts/{attempt_id}/renew

Renews a live LEASED or RUNNING attempt lease.

Response:

```json
{
  "attempt_id": "00000000-0000-0000-0000-000000000000",
  "lease_expires_at": "2026-10-06T17:00:40Z",
  "lease_duration_seconds": 30,
  "renew_after_seconds": 10
}
```

Expired, LOST, or terminal attempts cannot renew.

### POST /v1/workers/{worker_id}/attempts/{attempt_id}/result

Records a terminal worker result.

Accepted worker-reported states:

- SUCCEEDED
- FAILED
- TIMED_OUT
- PREEMPTED

Representative success:

```json
{
  "state": "SUCCEEDED",
  "container_engine": "podman",
  "exit_code": 0,
  "failure_reason": null,
  "stdout_tail": "done\n",
  "stderr_tail": "",
  "stdout_truncated": false,
  "stderr_truncated": false
}
```

Representative provider preemption:

```json
{
  "state": "PREEMPTED",
  "container_engine": "podman",
  "exit_code": null,
  "failure_reason": "PROVIDER_PREEMPTED",
  "stdout_tail": "",
  "stderr_tail": "",
  "stdout_truncated": false,
  "stderr_truncated": false
}
```

Terminal reporting atomically updates attempt/job state and releases the allocation.

Identical terminal reports are idempotent. Conflicting terminal reports return HTTP 409.

LOST is controller-generated and cannot be submitted by workers.

## Reconciliation

### POST /v1/workers/{worker_id}/reconcile

Worker-authenticated.

Compares a bounded set of locally discovered attempt IDs with controller-known attempts for that worker.

Request:

```json
{
  "attempt_ids": [
    "00000000-0000-0000-0000-000000000000"
  ]
}
```

Response includes worker-owned controller attempts, lease/context information, an `active` flag, and unknown IDs.

Unknown and foreign IDs intentionally receive equivalent treatment so the endpoint does not disclose another worker's attempt data.

### POST /v1/workers/{worker_id}/attempts/{attempt_id}/missing

Worker-authenticated.

Reports that a previously RUNNING workload is absent from a successfully inspected local engine.

Request:

```json
{
  "job_id": "00000000-0000-0000-0000-000000000000",
  "container_engine": "podman",
  "lease_expires_at": "2026-10-06T17:00:40Z"
}
```

The controller re-checks assignment ownership, RUNNING state, allocation, live lease, engine, job ID, and the observed lease deadline under row locks.

If the snapshot is still current, the attempt becomes:

```text
LOST / WORKLOAD_MISSING
```

The allocation is released and normal retry/max-attempt policy is applied atomically.

A stale reconciliation snapshot returns HTTP 409 rather than incorrectly declaring work missing.

## Important state semantics

### Job states

- `QUEUED`: waiting for assignment
- `RUNNING`: assigned to an active attempt
- `SUCCEEDED`: completed successfully
- `FAILED`: terminal unsuccessful job
- `CANCELLED`: requester cancelled before assignment

### Retryable attempt outcomes

Currently retryable:

- `LOST`
- `PREEMPTED`

Normal FAILED and TIMED_OUT outcomes are terminal for the job.

`max_attempts` limits the total number of attempts.

## Generated schema

FastAPI remains the authoritative executable schema.

With the controller running:

```bash
# Interactive Swagger UI
open http://127.0.0.1:8000/docs

# Raw OpenAPI JSON
curl --fail http://127.0.0.1:8000/openapi.json

# Prometheus metrics
curl --fail http://127.0.0.1:8000/metrics
```

When this document and generated OpenAPI disagree, treat the running application's OpenAPI schema and implementation as authoritative and update this document.
