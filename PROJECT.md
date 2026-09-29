# MeshCompute

## Purpose

MeshCompute is a distributed compute platform that lets people temporarily contribute unused compute capacity from devices they own and lets requesters run compatible workloads across that capacity.

The core idea is:

> Build reliable distributed compute from unreliable, heterogeneous, user-controlled machines.

The first version is intentionally small. It is not a marketplace yet. It is a control plane, worker agent, scheduler, and isolated workload executor that proves we can coordinate useful work across machines we do not own.

---

## Core principles

### 1. The provider owns the machine

MeshCompute borrows resources; it never owns them.

A provider can reclaim CPU, memory, disk, GPU, network capacity, or the entire machine at any time.

Provider actions must include:

- pause participation
- resume participation
- drain the worker
- change contributed CPU or memory
- terminate an individual workload
- terminate all workloads
- stop the worker agent
- shut down the machine

Local resource reclamation must never depend on the controller being reachable.

All contributed capacity is therefore **preemptible**.

### 2. Workers are unreliable by design

Workers can disappear because of:

- provider preemption
- shutdown
- network loss
- Wi-Fi changes
- power loss
- Docker failure
- agent crash
- OS updates
- laptop sleep

The platform should not try to make each worker reliable. It should make the overall system reliable despite unreliable workers.

### 3. Workers are untrusted

The worker host is not trusted by the requester.

For the MVP, requesters must assume the provider could inspect:

- workload code
- memory
- input data
- output data
- environment variables
- network traffic
- temporary files

MeshCompute must not claim requester confidentiality from providers.

The MVP is for non-sensitive workloads.

### 4. Workloads are untrusted

The provider is running code submitted by someone else.

Workloads must execute inside a restricted runtime that protects the host.

The MVP uses Docker containers. Docker is an implementation of the runtime interface, not the architecture itself.

### 5. Runtime capabilities are abstract

Workers advertise what execution environments they support.

The scheduler matches jobs to capabilities instead of assuming every worker is a Linux/Docker machine.

Initial runtime:

- `container`

Expected future runtimes:

- `wasm`
- stronger VM-based isolation
- GPU-aware executors
- platform-specific executors

---

## MVP scope

### In scope

- Python 3.12+
- Linux workers
- centralized control plane
- FastAPI
- PostgreSQL
- worker registration
- worker authentication
- heartbeats
- CPU and memory capacity advertisement
- configurable provider contribution limits
- worker health state
- queued jobs
- CPU/memory resource requests
- capability-aware scheduling
- atomic resource reservation
- worker pull model
- Docker-based execution
- leases
- retries
- provider preemption
- requester cancellation
- local provider controls
- basic logs and metrics

### Explicitly out of scope

Do not build these until the core system works:

- payments
- cryptocurrency
- blockchain
- GPU scheduling
- distributed model training
- Windows workers
- macOS workers
- PS5/Xbox clients
- smart-TV clients
- WASM execution
- Kubernetes
- peer-to-peer scheduler discovery
- NAT traversal
- confidential computing
- provider reputation
- multi-region controllers
- advanced pricing
- production-grade multi-tenancy
- GUI
- checkpointing implementation
- comprehensive automated tests

Tests will be added after the first end-to-end prototype works.

---

## Initial architecture

```text
                     +----------------------+
                     |    Control Plane     |
                     |                      |
                     | FastAPI              |
                     | Scheduler            |
                     | Worker State         |
                     | Job State            |
                     | Lease Management     |
                     +----------+-----------+
                                |
                   PostgreSQL   |
                                |
             +------------------+------------------+
             |                  |                  |
             v                  v                  v
         Worker A           Worker B           Worker C
         Python             Python             Python
             |                  |                  |
             v                  v                  v
       DockerExecutor      DockerExecutor      DockerExecutor
```

Workers initiate outbound communication with the controller.

The controller does not require inbound access to worker machines.

This is important because consumer devices commonly sit behind NAT, CGNAT, dynamic IP addresses, routers, and firewalls.

---

## Technology choices

### Controller

Initial stack:

- Python 3.12+
- FastAPI
- Pydantic v2
- SQLAlchemy
- Alembic
- PostgreSQL
- HTTPX
- Prometheus client

Python is chosen for iteration speed.

Do not design internal APIs that require the worker and controller to use the same language. The network protocol is the contract.

If performance data later justifies it, performance-sensitive components can move to Go or Rust.

### Worker

Initial stack:

- Python
- asyncio
- psutil
- HTTPX
- Docker Engine API

Linux is the only required MVP worker platform.

---

## Repository structure

```text
meshcompute/
├── controller/
│   ├── api/
│   ├── models/
│   ├── scheduler/
│   ├── services/
│   └── main.py
│
├── worker/
│   ├── agent/
│   ├── executors/
│   │   ├── base.py
│   │   └── docker.py
│   ├── preemption/
│   ├── resources/
│   └── main.py
│
├── cli/
│   └── main.py
│
├── common/
│   ├── auth/
│   └── schemas/
│
├── examples/
│   └── monte-carlo/
│
├── AGENTS.md
├── PROJECT.md
├── docker-compose.yml
├── pyproject.toml
└── README.md
```

Avoid building a large abstraction hierarchy before behavior requires it.

---

## Executor interface

The worker must not be tightly coupled to Docker.

Conceptually:

```python
class Executor:
    async def capabilities(self):
        ...

    async def start(self, workload):
        ...

    async def status(self, execution_id):
        ...

    async def terminate(self, execution_id):
        ...

    async def cleanup(self, execution_id):
        ...
```

The first implementation is:

```text
DockerExecutor
```

Future implementations may include:

```text
WasmExecutor
FirecrackerExecutor
GpuExecutor
PlatformSpecificExecutor
```

A worker advertises executor capabilities during registration and heartbeats.

Example:

```json
{
  "executors": {
    "container": true,
    "wasm": false
  }
}
```

---

## Resource model

The scheduler must distinguish four concepts:

```text
PHYSICAL CAPACITY
       |
       v
PROVIDER CONTRIBUTION
       |
       v
ACTIVE RESERVATIONS
       |
       v
ALLOCATABLE CAPACITY
```

Example:

```text
Physical CPU:          12
Provider contribution:  8
Reserved by jobs:       5
Allocatable:             3
```

Do not use current CPU utilization as the primary scheduling capacity.

CPU utilization is telemetry.

Provider limits and active reservations determine schedulable capacity.

Example worker resource state:

```json
{
  "cpu": {
    "physical": 12,
    "contributed": 8,
    "reserved": 5,
    "allocatable": 3
  },
  "memory_mb": {
    "physical": 32768,
    "contributed": 16384,
    "reserved": 8192,
    "allocatable": 8192
  }
}
```

Telemetry remains separate:

```json
{
  "cpu_usage_percent": 31.2,
  "load_1m": 2.1,
  "memory_available_mb": 14021
}
```

---

## Dynamic capacity

Providers may change contribution limits at any time.

Example:

```text
8 CPU -> 4 CPU
```

If currently allocated capacity is below the new limit, no jobs need to stop.

If current allocations exceed the new contribution, the worker must preempt enough workloads to satisfy the new limit.

Example:

```text
Current reservations: 6 CPU
New provider limit:   4 CPU
Required reclaim:     2 CPU
```

The worker decides which attempts to terminate and immediately returns the resources to the provider.

---

## Worker state

Initial worker states:

- `REGISTERING`
- `HEALTHY`
- `DEGRADED`
- `DRAINING`
- `PAUSED`
- `OFFLINE`

Meanings:

**HEALTHY** — worker is functioning and accepting compatible jobs.

**DEGRADED** — worker can communicate but an important dependency such as the executor is unhealthy.

**DRAINING** — worker accepts no new jobs but allows existing jobs to complete.

**PAUSED** — worker is participating in control-plane communication but accepts no new work.

**OFFLINE** — the controller has stopped receiving heartbeats.

Health and capacity are separate concepts.

A perfectly healthy worker may contribute zero CPU.

---

## Heartbeats

Default heartbeat interval:

```text
5 seconds
```

Initial controller thresholds:

```text
heartbeat age < 15 seconds  -> HEALTHY
15-30 seconds              -> DEGRADED
> 30 seconds               -> OFFLINE
```

Heartbeat payload should eventually contain:

- worker state
- agent version
- executor capabilities
- physical resources
- contributed resources
- reserved resources
- allocatable resources
- CPU/memory telemetry
- active attempt IDs

Example endpoint:

```text
POST /v1/workers/{worker_id}/heartbeat
```

---

## Worker pull model

Workers request assignments from the controller.

```text
Worker
  |
  | heartbeat
  +-------------------->
  |
  | claim work
  +-------------------->
  |
  |<-------------------- lease
  |
  | execute
  |
  | renew lease
  +-------------------->
  |
  | report result
  +-------------------->
```

Benefits:

- no inbound firewall configuration
- NAT-friendly
- provider remains in control
- workers can disappear naturally
- capacity can change dynamically

---

## Job model

Requester-visible job states:

- `QUEUED`
- `RUNNING`
- `SUCCEEDED`
- `FAILED`
- `CANCELLED`

Execution attempt states:

- `LEASED`
- `RUNNING`
- `SUCCEEDED`
- `FAILED`
- `TIMED_OUT`
- `PREEMPTED`
- `LOST`
- `CANCELLED`

A job may have multiple attempts.

Example:

```text
Job 123
  Attempt 1 -> Worker A -> PREEMPTED
  Attempt 2 -> Worker C -> LOST
  Attempt 3 -> Worker B -> SUCCEEDED

Final job state -> SUCCEEDED
```

---

## Requester cancellation vs provider preemption

These must remain distinct.

Requester cancellation means:

> I no longer want this job.

Result:

```text
CANCELLED
```

Provider preemption means:

> I want my machine resources back.

Attempt result:

```text
PREEMPTED
```

If retry policy allows it, the overall job returns to:

```text
QUEUED
```

Provider preemption is normal platform behavior and should not automatically count as provider failure.

---

## Provider controls

Local CLI should eventually support:

```bash
mesh-worker status
mesh-worker pause
mesh-worker resume
mesh-worker drain
mesh-worker resources --cpu 4 --memory 8G
mesh-worker cancel <attempt-id>
mesh-worker stop-all
```

### `pause`

Stop accepting new jobs.

### `resume`

Resume accepting compatible jobs.

### `drain`

Stop accepting new jobs and allow current jobs to finish.

### `resources`

Change contribution limits dynamically.

### `cancel`

Terminate a specific attempt.

### `stop-all`

Terminate every MeshCompute workload and reclaim all resources immediately.

These commands must work even when the control plane is unavailable.

---

## Preemption procedure

When a provider terminates a workload:

```text
1. Mark local attempt PREEMPTING.
2. Send SIGTERM.
3. Wait a short grace period.
4. Send SIGKILL if still running.
5. Release local CPU/memory reservations.
6. Clean temporary workload resources.
7. Mark attempt PREEMPTED locally.
8. Notify the controller when possible.
```

Resource release must not wait for controller acknowledgment.

Suggested structured preemption reasons:

- `PROVIDER_REQUESTED`
- `PROVIDER_REDUCED_CAPACITY`
- `PROVIDER_STOPPED_AGENT`
- `LOCAL_RESOURCE_PRESSURE`
- `SYSTEM_SHUTDOWN`

---

## Leases

Assignments are temporary leases rather than permanent ownership.

Initial defaults:

```text
lease duration: 30 seconds
renewal interval: 10 seconds
```

If renewal stops:

```text
lease expires
      |
      v
attempt -> LOST
      |
      v
job -> QUEUED
```

when retry policy permits.

Provider-triggered interruption should be explicitly reported as `PREEMPTED` instead of waiting for the controller to infer `LOST`.

---

## Execution guarantees

Assume **at-least-once execution**, not exactly-once execution.

A worker might successfully finish a task but fail before the controller receives completion. The lease can expire and the job can execute again.

Requester workloads should therefore preferably be:

- idempotent
- retryable
- stateless
- tolerant of duplicate execution

Checkpointing is expected later for long-running jobs.

---

## Scheduler

Initial eligibility:

```text
worker state == HEALTHY
AND worker accepts work
AND compatible executor exists
AND allocatable CPU >= requested CPU
AND allocatable memory >= requested memory
```

Initial placement can use simple best-fit logic.

Prefer a compatible machine that satisfies the job while leaving larger machines available for larger requests.

Resource reservation must be atomic.

Example:

```text
Worker has 4 CPU available.

Job A requests 3 CPU.
Job B requests 3 CPU.

Only one reservation may succeed.
```

PostgreSQL is the initial source of truth for controller-side reservation state.

Do not build advanced optimization during the MVP.

---

## Docker executor

Initial execution flow:

```text
receive assignment
      |
validate job
      |
pull image
      |
create temporary workspace
      |
start restricted container
      |
enforce CPU/memory limits
      |
capture stdout/stderr
      |
monitor timeout/preemption
      |
collect result metadata
      |
destroy container
      |
clean workspace
      |
report result
```

Initial container restrictions should include:

- no privileged mode
- no Docker socket
- no host PID namespace
- no host networking
- no arbitrary host mounts
- drop unnecessary Linux capabilities
- `no-new-privileges`
- CPU limits
- memory limits
- PID limits
- read-only root filesystem where practical
- temporary writable job workspace only

Outbound workload networking should default to disabled.

---

## API target

The eventual MVP API should contain roughly:

```text
POST /v1/workers/register
POST /v1/workers/{id}/heartbeat
POST /v1/workers/{id}/claim
GET  /v1/workers

POST /v1/jobs
GET  /v1/jobs
GET  /v1/jobs/{id}
POST /v1/jobs/{id}/cancel

POST /v1/attempts/{id}/start
POST /v1/attempts/{id}/renew
POST /v1/attempts/{id}/complete
POST /v1/attempts/{id}/fail
POST /v1/attempts/{id}/preempt

GET /health
GET /metrics
```

Not all endpoints should be implemented at once.

---

## Initial persistence model

Core entities:

### workers

- id
- name
- credential/token hash
- state
- agent version
- physical CPU
- contributed CPU
- physical memory
- contributed memory
- last heartbeat
- created/updated timestamps

### worker capabilities

- worker ID
- executor type
- CPU architecture
- metadata

### jobs

- id
- owner ID
- name
- runtime
- workload image/module reference
- command
- requested CPU
- requested memory
- timeout
- state
- max attempts
- timestamps

### job attempts

- id
- job ID
- worker ID
- attempt number
- state
- lease expiration
- timestamps
- exit code
- failure reason
- preemption reason

### worker allocations

- worker ID
- attempt ID
- reserved CPU
- reserved memory

---

## Development phases

### Phase 1 — Foundation

Build:

- repo structure
- FastAPI controller
- PostgreSQL
- SQLAlchemy
- Alembic
- configuration
- worker registration
- basic worker identity
- `/health`
- worker listing

### Phase 2 — Worker agent

Build:

- Python worker
- resource detection
- configurable contribution limits
- heartbeat loop
- worker states
- capability advertisement
- Docker executor detection

Goal:

```text
Controller
  Worker A -> HEALTHY -> 4 CPU
  Worker B -> HEALTHY -> 8 CPU
```

### Phase 3 — Job queue

Build:

- job persistence
- job submission
- job listing/status
- requester cancellation
- resource request validation
- `QUEUED` state

### Phase 4 — Scheduler

Build:

- worker eligibility
- executor matching
- CPU/memory filtering
- best-fit placement
- atomic resource reservation
- job attempts
- worker claim endpoint

### Phase 5 — Execution

Build:

- DockerExecutor
- image pull
- container launch
- CPU/memory limits
- output capture
- timeout handling
- cleanup
- result reporting

At this point MeshCompute should run a job end-to-end.

### Phase 6 — Failure recovery

Build:

- leases
- lease renewal
- lease expiry
- `LOST`
- requeue
- retry limits

Then deliberately kill workers and verify jobs recover.

### Phase 7 — Provider control

Build:

- pause
- resume
- drain
- contribution changes
- cancel attempt
- stop-all
- `PREEMPTED`

Local controls must work without the controller.

### Phase 8 — Reconciliation

Build:

- active-attempt heartbeat reporting
- expected-vs-reported attempt reconciliation
- stale reservation cleanup
- state reconciliation

### Phase 9 — Observability

Build:

- structured logs
- Prometheus metrics
- heartbeat metrics
- scheduler metrics
- preemption metrics
- job latency/runtime metrics

### Phase 10 — Tests and hardening

Only after the end-to-end prototype works, add:

- unit tests
- integration tests
- scheduler concurrency tests
- failure injection
- preemption tests
- resource-accounting tests
- executor security tests

---

## First acceptance test

Start:

```text
Worker A:
  contributed CPU: 2
  contributed RAM: 4 GB

Worker B:
  contributed CPU: 4
  contributed RAM: 8 GB
```

Submit:

```text
Job 1: 2 CPU
Job 2: 3 CPU
Job 3: 2 CPU
```

Expected:

```text
Worker A <- Job 1
Worker B <- Job 2
Job 3    -> QUEUED
```

When Job 1 completes:

```text
Worker A <- Job 3
```

Kill Worker B during Job 2.

Expected:

```text
Worker B stops heartbeating
        |
lease expires
        |
attempt -> LOST
        |
Job 2 -> QUEUED
        |
later capacity becomes available
        |
new attempt executes elsewhere
```

---

## Provider-preemption acceptance test

Start:

```text
Worker A
physical CPU:    12
contributed CPU:  8
```

Running:

```text
Job A: 3 CPU
Job B: 3 CPU
```

Provider executes:

```bash
mesh-worker resources --cpu 4
```

Current reservation is 6 CPU and new contribution is 4 CPU.

The worker must reclaim at least 2 CPU.

One appropriate result:

```text
Job A attempt -> PREEMPTED
resources returned immediately
job A -> QUEUED
job A may later run elsewhere
```

Provider control must not wait for the controller.

---

## Future directions

After the MVP is proven, investigate:

### WASM

Use a `WasmExecutor` for highly portable, lightweight workloads across:

- Linux
- Windows
- macOS
- ARM
- NAS devices
- potentially other installable appliances

### GPU compute

Begin with Linux/NVIDIA workers rather than trying to create a universal GPU layer immediately.

### Checkpointing

Allow long-running preemptible workloads to resume on another worker rather than restart from zero.

### Host activity awareness

Eventually allow consumer workers to automatically yield resources when:

- keyboard/mouse activity resumes
- a game launches
- controller activity appears
- CPU/GPU pressure increases
- memory pressure becomes dangerous

Local user activity always wins.

### Marketplace

Only after useful compute supply and demand are demonstrated should MeshCompute add:

- pricing
- metering
- provider credits
- requester billing
- reliability-aware pricing

---

## Current build target

Do not build the marketplace yet.

The immediate target is:

```text
FastAPI controller
       +
PostgreSQL
       +
Python Linux worker
       +
resource advertisement
       +
heartbeats
       +
job queue
       +
scheduler
       +
DockerExecutor
       +
leases/retries
       +
provider preemption
```

The MVP is successful when a requester can submit a workload, the scheduler assigns it to a worker, the worker executes it in isolation, the provider can interrupt it, and the control plane can recover by running the job elsewhere.
