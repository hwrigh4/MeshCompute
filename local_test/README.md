# Local functional tests (Phases 4.5–7)

This repo-root Python package holds MeshCompute's local functional testing tools.
It keeps these kinds of validation distinct:

- **Simulated scheduler scenarios:** real PostgreSQL/API transactions with
  simulated worker hardware and capabilities. `make test-scheduler` explicitly
  resets the local lab; it is not part of `test-local`.
- **Real runtime checks:** Podman API detection, bounded example source checks,
  six sequential restricted containers, and configured cgroup v2 limits.
- **Real-worker assignment:** the production heartbeat agent and one explicit
  claim, verified against the database. This diagnostic stops at LEASED.
- **Real execution:** `make test-execution` runs the production one-shot worker,
  all six example workloads, and focused resource/security/output checks. Results
  become terminal and allocations are released; no recovery/requeue is simulated.

## Setup and main command

Use Linux, Python 3.12+, Make, the installed project dependencies, PostgreSQL 17,
and working Podman CLI/API access (rootless preferred). The workflow below also
needs `podman-compose`. Run from the repository root with `.venv` available;
see [development setup](../docs/development.md) for installation and API sockets.
This package is included in project packaging, but the lab requires a checkout
for `examples/` and migrations; use an editable install for development.

Build the six example images explicitly if missing:

```bash
make examples-build-podman
```

Use three terminals with matching local configuration:

```bash
# Terminal 1
make dev-up COMPOSE=podman-compose

# Terminal 2: once the controller is ready, leave the worker running
make dev-real-worker

# Terminal 3: wait for the worker's first HEALTHY heartbeat
make dev-state
make test-local
```

`test-local` runs these aggregates sequentially and stops on failure, even with
`make -j`:

| Command | Checks, in order |
| --- | --- |
| `make test-local-runtime` | Required Podman API probe; example sources; six restricted Podman runs; actual CPU/memory/PID cgroup limits |
| `make test-local-assignment` | Controller/database status; real-worker assignment |

Run one aggregate at a time. Aggregates never install software, build/pull images,
start persistent services, or reset state. The worker's default 1 CPU / 256 MiB
contribution configures scheduler capacity; the separate Podman runs validate
kernel limits (1 CPU / 128 MiB / 64 PIDs), not a stress test or isolation audit.

## Diagnosis, reruns, and shutdown

Individual commands remain available: `make check-engines`, `make examples-check`,
`make examples-run-podman`, `make check-container-limits`, `make dev-status`,
`make dev-state`, and `make test-real-worker`. For a required Podman-only probe:

```bash
.venv/bin/python -m local_test.engines --engine podman --require
```

Sources and Containerfiles remain in `examples/`. Saved credentials and locks
remain in the gitignored `.meshcompute-lab/`, with the same identities and file
format, directory mode 0700, and credential mode 0600. Saved identity formats remain compatible; see the Phase 6 database migration guard.
Tokens are never printed or passed as command arguments.

Assignment results stay in PostgreSQL for `make dev-state`. A rerun requires a
clean lab. Diagnostic claims do not renew: Phase 6 expires their leases and
releases reservations automatically. Stopping a worker **does not itself release
reservations**. Inspect state after recovery. The diagnostic helper still
requires empty attempt history, unlike execution/recovery suites. After inspecting results, stop agents
with Ctrl-C and run `make dev-reset` only if all existing local lab data can be
deleted; then restart `make dev-real-worker`, wait for HEALTHY, and retest.
Never blindly retry an ambiguous claim: inspect persisted state first.

For shutdown, Ctrl-C the worker and controller in their terminals, then optionally
run `make dev-down COMPOSE=podman-compose` to stop PostgreSQL and retain its volume.

On one Crostini Chromebook, a Netavark nftables failure was resolved with:

```bash
NETAVARK_FW=iptables make dev-up COMPOSE=podman-compose
```

This is an optional per-command workaround, not a universal fix or global default.

These tests target a localhost controller and database. Future remote functional
tests need explicit configuration and separate safeguards; do not weaken the
local guards. See [development.md](../docs/development.md) for full lifecycle,
restrictions, engine overrides, and explicit scheduler reset scenarios.


## Phase 5 end-to-end execution

Keep Terminal 1's controller/database running. Stop Terminal 2's heartbeat agent;
Phase 5 work-once maintains its own heartbeat. With the example images built:

```bash
make dev-state
make test-execution
make dev-state
```

The helper defaults to `auto` (Podman preferred, Docker fallback). Use
`.venv/bin/python -m local_test.execution --engine podman --scenario success`
for one check, or `--engine docker` for Docker. It refuses queued/running work and
reservations, builds/pulls nothing, and never resets data. Existing terminal
history is compatible with reruns. Diagnostic reservations now expire; inspect
state before rerunning. Old unleased reservations require inspection before migration.

`make dev-work-once` executes one manually submitted job using the protected real
identity. Production `mesh-worker work-once` takes credentials only through the
environment, sends heartbeats, claims once, executes, reports, and exits. No
separate heartbeat process is required. Only LOST/PREEMPTED attempts retry up to `max_attempts`;
ordinary execution failures remain terminal.
Inspect the printed attempt through `GET /v1/attempts/{id}` or `make dev-state`.
See the [execution workflow and limits](../docs/development.md#phase-5-container-execution)
for security policy, output limits, failure handling, migrations, and shutdown.


Phase 5.1 adds exactly four scenarios to the same `make test-execution` suite:
`image-failure`, `policy-rejection`, `oversized-result`, and `name-collision`.
Run one with `python -m local_test.execution --scenario NAME`. Build the tiny
rejected-volume fixture first with
`python -m local_test.examples build --engine podman --name test-volume`
(or use the normal full image-build command). The original six source/runtime
checks remain unchanged.

These prove real-engine acquisition failure and fail-closed policy rejection,
HTTP 413 with unchanged assigned state, and preservation of an unrelated-name
fixture. No controller result is fabricated: reservations are released by normal
worker terminal reporting. The oversized-request check subsequently executes its
success job; the collision fixture is removed only after preservation is proven.
See [scenario details](../docs/development.md#phase-51-failure-path-checks) for
expected states, real-engine versus fixture behavior, and cleanup.


## Phase 6 recovery

`make test-recovery` tests leases against real PostgreSQL and a real engine. Stop
other controllers/agents for the lab first: this helper owns a temporary
controller so it can test restart safely, and refuses an occupied port. It never
resets data, installs software, or builds images.

```bash
make dev-db-up COMPOSE=podman-compose
make dev-migrate
make dev-state
make test-recovery
make dev-state
```

Build success/sleep images explicitly if missing. `--engine podman` or `--engine
docker` on `python -m local_test.recovery` requires that engine. Checks cover a
40-second renewed workload, expired diagnostic claim with fresh heartbeat,
SIGKILL and retry on a second worker, exhausted attempts, stale/authenticated
requests, competing recovery transactions, terminal replay, controller restart,
and local termination after connectivity loss. SQL backdating and direct API
fixtures are labeled separately from real-engine behavior. Results stay in the
DB; protected recovery identities stay in `.meshcompute-lab/recovery-workers.json`.

Heartbeats do not renew leases. Leases last 30 seconds, renew every 10, and are
recovered by the controller about every 5. Only LOST/PREEMPTED attempts requeue while
attempts remain. Stale workers cannot mutate expired assignments. Hard death can
leave an orphan provider container and overlapping retry execution: this is
at-least-once behavior. The harness cleans only its labeled orphan fixtures;
production reconciliation remains deferred; Phase 7 controls live sessions. See
[recovery details](../docs/development.md#phase-6-leases-and-failure-recovery).


## Phase 7 provider controls

`make test-provider` reuses the owned-controller recovery harness. Stop normal
controllers/agents first, start PostgreSQL, migrate, and build success/sleep images
explicitly if missing. The suite refuses active work and never resets data.

It validates real pause/resume, settings across restart, draining, capacity
reduction without killing work, online/offline stop-all, PREEMPTED retries and
exhaustion, and preservation of an unrelated restricted container. Only the
ownership check briefly overlaps two small workloads. Test fixtures and retained
results follow the existing local lab conventions. Use `--engine podman` or
`--engine docker` through `python -m local_test.provider` for diagnosis.

For manual controls use `mesh-worker status|pause|resume|drain|stop-all` and
`mesh-worker resources --cpu 1 --memory 256`. Set the worker ID and the same
state directory as the agent. Lab helpers use `.meshcompute-lab/provider-state`;
production defaults to `~/.local/state/meshcompute`. Controls need no token.
Stop-all leaves participation PAUSED and acknowledges local cleanup independently
of controller reporting. It never discovers orphan containers after restart.
See [provider controls](../docs/development.md#phase-7-local-provider-controls)
for complete configuration, security, offline semantics, and shutdown details.
