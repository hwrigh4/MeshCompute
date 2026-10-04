# Local functional tests (Phases 4.5 and 5)

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
format, directory mode 0700, and credential mode 0600. No state migration is needed.
Tokens are never printed or passed as command arguments.

Assignment results stay in PostgreSQL for `make dev-state`. A rerun requires a
clean lab because Phase 4 reservations never expire or complete. Stopping a
worker **does not release reservations**. After inspecting results, stop agents
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
history is compatible with reruns. Old assignment-only reservations require
inspection and a separate lab or an explicit disposable-data reset.

`make dev-work-once` executes one manually submitted job using the protected real
identity. Production `mesh-worker work-once` takes credentials only through the
environment, sends heartbeats, claims once, executes, reports, and exits. No
separate heartbeat process is required. `max_attempts` does not cause retries.
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
