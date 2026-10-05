# Local functional lab (Phases 4.5–6)

This Linux development lab uses real APIs, authentication, PostgreSQL transactions,
and the allocation ledger. Phase 4.5 scheduler fixtures simulate worker hardware;
manual runtime checks validate real engines independently. Phase 5 execution checks
run the production worker, execute real containers, record results, and release
allocations. Helpers are local functional tests, not a broad unit-test framework.

The repo-root [local_test/ package](../local_test/README.md) is the home for these
functional tests. Workload sources and Containerfiles stay in `examples/`;
credentials and locks stay in `.meshcompute-lab/`. Existing saved identities and
state files remain compatible; moving the helpers does not require a reset.
Run helpers from the repository root using the project environment (editable
installation recommended); this lab needs the checkout's examples and migrations.
`make test-local` retains the Phase 4.5 runtime/assignment aggregate.
`make test-execution` validates the Phase 5 end-to-end path; see its workflow below.

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
- Attempts: IDs, worker/job IDs, attempt number, engine, state, timestamps, exit
  code, failure reason, bounded stdout/stderr tails, and truncation flags.
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
.venv/bin/python -m local_test.lab register large --cpu 32 --memory 65536 --engine both
.venv/bin/python -m local_test.lab register paused --cpu 4 --memory 8192 --engine podman --state PAUSED
.venv/bin/python -m local_test.lab submit --name small --cpu 0.5 --memory 256
.venv/bin/python -m local_test.lab heartbeat A
.venv/bin/python -m local_test.lab claim A
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
selected scheduler scenario starts with a full explicit development reset, because
the fixtures require deterministic empty starting state (diagnostic leases now expire):

```bash
make test-scheduler YES=1
make test-scheduler-basic YES=1
make test-scheduler-concurrency YES=1
.venv/bin/python -m local_test.lab scenario stale --yes
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
is exposed. Job `RUNNING` means assigned; attempt `RUNNING` means execution was authorized.

## Real Podman and Docker checks

```bash
make check-engines
.venv/bin/python -m local_test.engines --engine podman --require
.venv/bin/python -m local_test.engines --engine docker --require
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
.venv/bin/python -m local_test.engines --engine docker --socket /var/run/docker.sock
```

Docker's `/info` reports rootless mode through `SecurityOptions`; see the
[Docker Engine API documentation](https://docs.docker.com/reference/api/engine/version-history/).
No installation, service start, permission change, or engine configuration change
is performed by diagnostics. Engine absence does not block simulated scenarios.

## Example OCI workloads

Each of the six runnable examples has a `Containerfile` and Python source. Their
images use `python:3.12-slim`, run as an unprivileged numeric user, and require no Python
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
make examples-build-podman   # six workloads plus the test-only volume fixture
make examples-build-docker   # same workloads and test fixture, using Docker
.venv/bin/python -m local_test.examples build --engine podman --name success
podman build -f examples/success/Containerfile -t localhost/meshcompute-success:dev examples/success
docker build -f examples/success/Containerfile -t localhost/meshcompute-success:dev examples/success
```

Build commands may fetch the base image using the developer's engine. Phase 5
workers use a local image first, and attempt a noninteractive pull only if missing. Images are tagged `localhost/meshcompute-<example>:dev`. Missing
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
MeshCompute execution, timeout, output, and completion path is tested separately
with the Phase 5 commands below.

## Later physical-device testing

The logical-worker lab does not establish real host isolation, CPU/memory
enforcement, or remote-engine correctness. Later, start the existing real agent
on each Linux device using its own registration/environment and a secured
controller URL, then validate actual engine probes on those hosts. Devices still
use outbound heartbeats/claims. Keep local simulated fixtures and credentials
separate; do not run destructive lab helpers against that environment.

## Repeatable real-environment validation

The following commands retain the Phase 4.5 diagnostic behavior. Use the local PostgreSQL/controller and a
working Podman installation with the six local example images. Run sequentially
on small development machines; the defaults suit a 4-CPU Chromebook with limited
available RAM and no swap. No helper installs software or changes engine/system
configuration. Existing loopback-only controller/database guards still apply.

Before testing, build missing example images explicitly with
`make examples-build-podman`. Preserve existing `.env` and database settings.
The primary three-terminal workflow is:

```bash
# Terminal 1: leave the controller running
make dev-up COMPOSE=podman-compose

# Terminal 2: after the controller is ready, leave the actual agent running
make dev-real-worker

# Terminal 3: wait for the first HEALTHY heartbeat (normally within 5 seconds)
make dev-state
make test-local
make dev-state                   # results remain persisted
```

If PostgreSQL is already running locally, Terminal 1 can use `make dev-controller`.
`test-local` runs `test-local-runtime` then `test-local-assignment`, in order:

| Aggregate | Ordered checks |
| --- | --- |
| `make test-local-runtime` | Required Podman API diagnostic, example source checks, six restricted Podman runs, cgroup limits probe |
| `make test-local-assignment` | Controller/database status, one real-worker assignment validation |

Each aggregate stops on the first failure, including under `make -j`. They never
install software, build or pull images, start persistent services, or reset data.
Destructive `test-scheduler*` scenarios are excluded and retain their explicit
reset workflow. Run only one aggregate at a time; independently requesting
multiple Make targets with `-j` can still run those separate targets concurrently.

For diagnosis, each existing command is still available:

```bash
make check-engines                # report both engines; PENDING is not PASS
.venv/bin/python -m local_test.engines --engine podman --require
make examples-check
make examples-run-podman
make check-container-limits
make dev-status
make dev-state
make test-real-worker             # one-shot: requires a clean lab
```

The runtime aggregate is repeatable without changing scheduler state. The
assignment aggregate (and thus `test-local`) needs a clean lab on rerun. Its
diagnostic claim does not renew; after about 30 seconds plus a recovery scan the
reservation is released and this max-attempts=1 job fails. The diagnostic helper
still requires empty attempt history; inspect results and use a separate clean
lab or explicitly reset disposable data before rerunning it.

These helpers currently target a local controller. Future remote functional
testing requires its own explicit configuration and safeguards; the existing
localhost-only controller/database guards remain in force. No remote test
framework is provided.

For Podman, enable its rootless API socket explicitly as described above if it
is inactive. Both the CLI and API should work: API probe success alone does not
establish that CLI image/container commands work from the current environment.

On one Debian 13 Crostini Chromebook, Compose failed with a Netavark nftables
error and succeeded with this **optional, per-command workaround**:

```bash
NETAVARK_FW=iptables make dev-db-up COMPOSE=podman-compose
```

This is an observation, not a universal fix or a global default. The helpers do
not set it, install firewall tools, or alter system configuration. Diagnose other
networking failures separately.

### Sequential OCI image and kernel-limit checks

`examples-run-podman` runs existing `localhost/meshcompute-<example>:dev` images
one at a time, with no pulls. Missing images fail with the build command. All runs
use `--network=none --cpus=1 --memory=128m --pids-limit=64 --read-only
--cap-drop=ALL --security-opt=no-new-privileges`. The helper uses local Podman
(`--remote=false`), without host mounts or privileged mode.

Expect six PASS lines and `6/6 checks passed; 0 failed`. The failure example must
exit **7** and print its intentional-failure marker; all others must exit **0**.
Container state inspection separates startup/engine/OOM errors from the expected
workload failure. The helper bounds each attached run to 30 seconds and engine
operations to 15 seconds. It removes only its own randomly named container after
each check, including failures, Ctrl-C and SIGTERM. A cleanup failure is nonzero
and names the container for manual inspection; remaining checks stop to avoid
overlapping workloads. No pruning or unrelated removal occurs. Abrupt
SIGKILL/host shutdown cannot guarantee cleanup.

`check-container-limits` uses the success image to read `cpu.max`, `memory.max`,
and `pids.max` from `/sys/fs/cgroup` inside a container with the same restrictions.
Expect a CPU quota/period ratio of **1**, memory **134217728**, and PID limit **64**.
The CPU period need not be 100000. Missing cgroup v2 files, unlimited values,
invalid output, mismatches, and unsupported environments fail nonzero. This
validates **configured kernel limits**, not a stress test or comprehensive
isolation audit. The helper applies the same timeouts and cleanup policy.

These manual-engine runs are separate from MeshCompute execution. They do not
submit jobs or exercise worker result reporting.

### Real heartbeat agent and one explicit assignment

`dev-real-worker` registers/reuses `lab-real-local`, stored separately from
simulated aliases in `.meshcompute-lab/real-worker.json`. It reuses the lab's
atomic credential writes (directory 0700, file 0600, gitignored). Credentials go
only into the production worker's environment, never its arguments or output.
A Linux advisory lock, retained by the actual agent, prevents duplicate
helper-managed agents for this identity in the same checkout. Do not copy this
credential file to another checkout or start a second agent manually with it.
Lock files remain on disk; the kernel releases locks when their processes exit.

The helper defaults to **1 CPU / 256 MiB**, and requires the actual Podman API
probe to pass before registration. It then runs the unchanged production
heartbeat implementation, including real host detection, contribution clamping,
engine probes, the 5-second heartbeat cadence, and signal handling. Override
contributions explicitly when needed:

```bash
MESHCOMPUTE_WORKER_CPU_LIMIT=2 MESHCOMPUTE_WORKER_MEMORY_LIMIT_MB=512 make dev-real-worker
```

Existing worker socket environment settings apply. An explicit
`MESHCOMPUTE_WORKER_CONTAINER_ENGINE=docker` or `auto` uses production detection
semantics, but `test-real-worker` deliberately requires advertised Podman
capability. The contribution is **scheduler configuration**, not OS enforcement;
manual Podman checks above validate actual container kernel limits.

`test-real-worker` requires the helper-managed agent to be running, freshly
HEALTHY, container/Podman-capable, and contributing at least 0.5 CPU / 128 MiB.
It checks that the saved identity matches the configured database. Stale
credentials fail with guidance instead of silently registering replacements.
Keep other job submitters and claimers stopped during this check. Preflight
refuses any queued/running jobs, attempts, or allocations anywhere in the lab;
it does not erase or modify existing work. Concurrent copies of this check in
one checkout are also refused.

It submits one success-image job (0.5 CPU / 128 MiB), rechecks for competing work,
makes exactly one explicit authenticated claim through the production worker's
`claim_once` implementation, and verifies the returned
assignment plus the database's RUNNING job, LEASED attempt number 1, and matching
allocation. Expected final message:

```text
Assignment validated; this diagnostic does not execute containers. Use make test-execution for Phase 5.
```

Submission/claim failures are never automatically retried. An ambiguous claim
triggers persisted-state inspection; if inspection is unavailable, run
`make dev-state` before deciding what to do next. Interruptions may also leave a
queued or assigned job. This is diagnostics, not reconciliation.

Results remain for inspection. A second diagnostic run refuses existing attempt
history as well as active jobs/reservations.
This diagnostic claim does not renew: its lease expires and controller recovery
releases the reservation. **Stopping a heartbeat worker does not itself release
reservations.** Inspect `make dev-state` after recovery. Use a separate clean
lab for another diagnostic, or explicitly `make dev-reset` only if all local
work can be deleted; stop agents first. Reset removes simulated, real, and recovery credential
files. After a reset, restart
`make dev-real-worker` (new identity) and repeat the test. For a database reset
performed outside the helper, stop the agent and remove only the stale
`.meshcompute-lab/real-worker.json` after confirming the reset and DB/URL pairing.
Never reset automatically to resolve a configuration mismatch.

For shutdown, Ctrl-C the worker and controller in their respective terminals;
`make dev-down` optionally stops Compose PostgreSQL while retaining its volume.


## Phase 5 container execution

Keep PostgreSQL and the controller running in Terminal 1. Build the six example
images explicitly if they are missing. Stop `dev-real-worker` in Terminal 2:
`work-once` sends and maintains its own real heartbeats, waits for its first
confirmed heartbeat, then claims at most one job. It exits cleanly on no work.
Do not run competing submitters/claimers while validating a local lab.

```bash
# Terminal 1
make dev-up COMPOSE=podman-compose

# Terminal 2: no separate heartbeat agent needed
make examples-build-podman        # explicit setup, only if images are missing
make dev-status
make dev-state                   # check for existing queued/running jobs/reservations
make test-execution
make dev-state                   # terminal results remain available
```

`make test-execution` runs sequentially with 0.5 CPU / 128 MiB requests (64 MiB
for the memory-over-limit case) on a real worker contributing 1 CPU / 256 MiB.
It uses the existing protected real-worker identity and helper locks. It refuses
active jobs/reservations and a running heartbeat helper, never resets data, and
checks images before submitting. Terminal history is allowed: successful runs
can be repeated without reset. An assignment-only reservation now expires; wait
for controller recovery and inspect it before proceeding. Pre-Phase6 unleased
reservations require inspection before migration, not invented lease deadlines.
**Stopping a heartbeat agent does not itself release reservations.**

The scenarios cover success, intentional exit 7 with stderr, sleep timeout, CPU
burn, memory hold, bounded memory exhaustion, deterministic Monte Carlo output,
actual cgroup v2/security settings, exact argv, and separate 64 KiB output tails.
Each result must have one attempt even with `max_attempts=3`, the expected engine,
terminal job/attempt, no allocation, and no container. CPU/memory scenarios inspect
live engine settings; the kernel probe also checks effective capabilities,
no-new-privileges, seccomp, UID/GID, network interfaces, CPU/memory/PID/swap limits,
and writable temporary paths. It is not a comprehensive isolation audit.
Duplicate start/result reports and conflicting results are also checked.

```bash
# Diagnose one scenario, or explicitly select another available engine
.venv/bin/python -m local_test.execution --scenario success --engine podman
.venv/bin/python -m local_test.execution --scenario timeout --engine auto
.venv/bin/python -m local_test.execution --engine docker
make examples-build-docker        # separate explicit setup if needed
```

Expected output includes `PASS success: SUCCEEDED, engine=podman, ...` and
`PASS timeout: TIMED_OUT, ...`; failure and memory-over scenarios correctly pass
with a FAILED attempt. The CLI returns zero when a workload's terminal result
was successfully recorded, including workload failure; operational/reporting
failures return nonzero. The functional helper checks the expected outcome.

To execute one manually submitted job using the local protected identity:

```bash
.venv/bin/python -m local_test.lab submit --name phase5-success --cpu 0.5 --memory 128
make dev-work-once
make dev-state
# Use the attempt UUID printed by the worker/state inspection:
curl http://127.0.0.1:8000/v1/attempts/ATTEMPT_UUID
```

`dev-work-once` defaults to Podman and 1 CPU / 256 MiB; the same explicit
`MESHCOMPUTE_WORKER_*` contribution/engine overrides as `dev-real-worker` apply.
The production command is `mesh-worker work-once` (or
`.venv/bin/python -m worker.main work-once`) using existing worker ID/token and
controller settings from its environment. Production stores no credentials.
The default `mesh-worker` remains heartbeat-only; `mesh-worker claim` remains a
single diagnostic claim without execution. Work is sequential; a Linux abstract
socket prevents simultaneous work-once executors for one identity on one host.
Do not share an identity across hosts or bypass helper locks.

### Phase 5.1 failure-path checks

`make test-execution` includes four additional scenarios without replacing the
existing ten. Build the test-only metadata image explicitly before the full run:

```bash
.venv/bin/python -m local_test.examples build --engine podman --name test-volume
# Or build all six workloads plus this fixture with make examples-build-podman.
.venv/bin/python -m local_test.execution --scenario image-failure
.venv/bin/python -m local_test.execution --scenario policy-rejection
.venv/bin/python -m local_test.execution --scenario oversized-result
.venv/bin/python -m local_test.execution --scenario name-collision
```

All four use a real local controller/PostgreSQL and real worker heartbeat/claim.
The helper checks the initial LEASED attempt and allocation before invoking the
unchanged production execution coordinator in a bounded child process. This
allows fixture setup between claim and execution; no engine or result is mocked.
Use `--engine podman` or `--engine docker` to select an available local engine.

| Scenario | Fixture and assertion | End state / cleanup |
| --- | --- | --- |
| `image-failure` | A missing image references a randomly reserved, non-listening loopback port. The real engine's acquisition fails quickly without external registry/DNS dependencies. | FAILED / IMAGE_PULL_FAILED; both start timestamps null, allocation removed, no container. |
| `policy-rejection` | `localhost/meshcompute-test-volume:dev` is a tiny `FROM scratch` image declaring `/data` as a volume. Its metadata exists in the real engine; the worker must reject it before launch. | FAILED / SECURITY_POLICY_UNSUPPORTED; no start timestamps, allocation or container. Security policy stays unchanged. |
| `oversized-result` | The assigned worker's authenticated result request contains more than the 800 KiB body limit. HTTP 413, rather than field-validation 422, must leave the entire job, attempt and allocation unchanged. The body is never printed. | At the assertion: still LEASED, with the original allocation and timestamps. Then the real success workload executes normally to finish SUCCEEDED and release the reservation; no artificial release. |
| `name-collision` | Direct fixture setup creates an unstarted container with the expected attempt name but only a unique test-ownership label. After worker execution, its ID, configuration and state must still match. | FAILED / CONTAINER_CREATE_FAILED; valid terminal result recorded and allocation removed. Only after proving preservation does the harness remove its fixture by ID and verified test label, independently of production cleanup. |

Image failure, policy rejection and collision validate real engine interactions;
the 413 assertion validates controller/API behavior (its subsequent success run
uses the real engine). Every scenario checks exactly one attempt despite
`max_attempts=3`, a clean child exit, terminal results, and final container cleanup.
They reuse existing credentials, locks, local guards, state inspection and failure
handling. The existing cancellation-race check normalizes snapshot UUIDs before
matching allocations; this corrects a test-only false failure when a claim wins.
Tests never build images or reset data automatically. Results remain available for inspection; successful runs can be repeated without a reset.

### Execution policy and limits

The requester runtime remains `container`. `ContainerExecutor` selects the
configured engine immediately before execution. `auto` prefers usable Podman,
then Docker; explicit `podman`/`docker` never falls back. Adapters share a local
Docker-compatible Unix API implementation; Podman's adapter also verifies its
native capability sets. CLI contexts and host registry configuration are not
passed to workloads. Existing local images are pinned by image ID. Missing images
are pulled noninteractively with a 120-second overall acquisition deadline and
bounded progress parsing. No registry credential protocol is added. Image-declared
volumes are rejected to avoid unbounded anonymous storage.

Every execution uses numeric UID/GID 65532:65532, no network, a read-only root,
all capabilities dropped, no-new-privileges, 64 PIDs, the reserved CPU quota and
memory in MiB, and memory+swap equal to memory (no extra swap). CPU uses a 100000
microsecond period; nonrepresentable requests or quotas below the kernel's 1000
microsecond minimum fail safely. Configuration is inspected before launch.
Engine defaults for seccomp and SELinux/AppArmor remain enabled where available.
No privileged mode, host namespace, host bind, device, socket, or host credential
injection is offered. Image commands are replaced with the submitted argv exactly,
without an implicit shell. Incompatible images fail instead of relaxing policy.

`/tmp` and `/mesh/work` each have an isolated 16 MiB tmpfs with
nosuid/nodev/noexec; shared memory is capped at 16 MiB. Memory cgroups also bound
temporary RAM usage. Image healthchecks and container restart policies are disabled.
There is no artifact persistence. Contribution settings configure the scheduler;
Phase 5 applies each reservation to a container. Manual Podman limit checks remain
an independent way to validate kernel enforcement. Containers are not a perfect
security boundary, and provider hosts remain able to inspect workload data.

Output is attached before launch with separate stdout/stderr, drained in small
chunks, and retained as at most 64 KiB UTF-8 per stream. Invalid UTF-8 and NUL are
replaced; truncation flags identify dropped bytes. Engine log persistence is
disabled to prevent unbounded host log files. PostgreSQL/API enforce the same
byte limit, and attempt mutation bodies are capped at 800 KiB before JSON parsing
(to allow escaped JSON). No full log service is implemented.

The execution deadline includes container start and running time. On expiry the
worker requests a two-second stop grace, kills if still running, confirms exit,
and removes the container. Engine operations have bounded timeouts. Cleanup checks
the current attempt's ownership label and deletes by inspected container ID,
including partial creation. A colliding existing name is never adopted or removed.
Images are cached; no unrelated volumes, networks, or containers are pruned.

### Result protocol and remaining limits

Worker-authenticated endpoints use the existing worker ID and bearer token:

- `POST /v1/workers/{worker_id}/attempts/{attempt_id}/start` with
  `{"container_engine":"podman"}` authorizes LEASED → RUNNING and records start
  timestamps. Repeating it with the same engine returns the existing timestamps
  only while the lease remains valid.
- `POST /v1/workers/{worker_id}/attempts/{attempt_id}/result` records state,
  engine, exit code, stable failure reason, bounded output, and truncation flags.
  The exact same terminal report is idempotent; conflicting outcomes or fields
  return 409 without rewriting history.
- `GET /v1/attempts/{attempt_id}` exposes result metadata and bounded output,
  never worker credentials. Requester reads/submissions remain local-MVP APIs
  without requester authentication; do not expose them to the internet.

The container starts only after a confirmed start response. Start and result
requests may retry the same payload up to three times on transient/ambiguous
responses; claims and engine starts are never blindly retried. If ambiguity
remains, the worker stops and requires inspection. No further job is claimed.

Success maps to attempt/job SUCCEEDED. Nonzero exit maps to FAILED; timeout maps
to attempt TIMED_OUT and job FAILED. Engine/image/policy failures can end a LEASED
attempt as FAILED without start timestamps. Completion timestamps, both terminal
states, and allocation deletion commit in **one PostgreSQL transaction**. Local
container exit alone never releases a server reservation. Cleanup happens before
result reporting; unresolved cleanup keeps the reservation and emits inspection
guidance rather than assuming the workload is gone.

Ctrl-C/SIGTERM during execution cleans up the current container and attempts a
FAILED/WORKER_INTERRUPTED result while ownership remains valid. Phase 6 now
recovers expired reservations after SIGKILL, host loss, or unresolved controller
communication. Only LOST attempts are retryable; ordinary Phase 5 failures remain
terminal even with `max_attempts=3`. Lease loss cleans up without reporting
WORKER_INTERRUPTED. No provider preemption or reconciliation is implemented.
For shutdown, stop the worker and controller with Ctrl-C, then optionally
`make dev-down COMPOSE=podman-compose`; the database volume remains.

### Phase 5 migration check

Run this only on a separate disposable database, before creating execution history:

```bash
.venv/bin/python -m alembic upgrade head
.venv/bin/python -m alembic check
.venv/bin/python -m alembic downgrade 0004
.venv/bin/python -m alembic upgrade head
.venv/bin/python -m alembic check
```

Migration 0005 adds bounded attempt results without changing earlier migrations.
Downgrade refuses non-LEASED attempt history because Phase 4 cannot represent it;
never discard existing results automatically to make a migration check pass.


## Phase 6 leases and failure recovery

Claims atomically reserve resources and create a 30-second ownership lease.
Assignments and authenticated `POST /v1/workers/{worker_id}/attempts/{attempt_id}/renew`
responses include `lease_expires_at`, `lease_duration_seconds`, and
`renew_after_seconds` (10). Timing policy lives in
`controller/services/lease_policy.py`. The attempt read API includes the deadline.

The controller uses PostgreSQL time after acquiring worker → job → attempt locks.
Start, renew, and active result reports return HTTP 409 at or after expiry, even
before the reaper runs. Identical already-recorded terminal reports remain
idempotent after their historical deadline. Workers cannot report LOST or
LEASE_EXPIRED. Heartbeat freshness measures worker health; it never renews an
attempt or substitutes for ownership.

A FastAPI lifespan task scans persisted active deadlines every five seconds,
re-locks/re-checks each candidate, then atomically marks LOST/LEASE_EXPIRED,
sets completion time, deletes the allocation, and either requeues the job or
fails it. Competing controllers serialize through database locks; no in-memory
ownership state is required. Shutdown cancels and awaits the task and its bounded
DB scan. Restart immediately scans persisted deadlines without waiting for a
worker, claim, or manual recovery request.

Only LOST attempts consume the retry policy: if the highest attempt number is
below `max_attempts`, the job becomes QUEUED with a null completion time. The
next normal claim creates the next attempt; claim also defensively enforces the
limit. At exhaustion the job becomes FAILED. Job `started_at` retains its first
actual start; a never-started LEASED attempt can leave it null. Normal failure,
timeout, image, policy, and interruption results remain terminal without retry.

The worker starts its lease keeper immediately after claim, before image work,
and renews through execution, capture, cleanup, and terminal reporting. It uses
a conservative monotonic deadline anchored to request-send time, with timings
from the protocol, so network latency cannot extend its local ownership. Transient
renew failures retry at a bounded cadence. Rejection or deadline expiry cancels
execution and removes only the current attempt's owned container. It sends no
stale terminal result and makes no assumption that the controller has released
the allocation. Claim requests themselves are still never blindly retried.

Hard death can leave a provider-side orphan running after controller recovery:
the controller cannot reach into an unavailable provider engine. The orphan may
exit naturally or stop with its engine/host. Retries can overlap it: execution is
**at least once**, not exactly once. Worker reconciliation belongs to Phase 8;
provider preemption belongs to Phase 7. Neither is implemented here.

### Recovery validation

This suite needs real PostgreSQL, a migrated idle local lab, and the existing
success/sleep images on a real Podman or Docker API. Stop other controllers and
agents for this database first. Unlike `test-execution`, `test-recovery` owns a
short-lived controller child so it can safely test shutdown/restart; an occupied
controller port is rejected. It never stops unrelated processes or resets data.

```bash
# Stop the normal foreground controller/agents with Ctrl-C first.
make dev-db-up COMPOSE=podman-compose
make dev-migrate
make examples-build-podman             # explicit, only if needed
make dev-state                        # database inspection works without controller
make test-recovery
make dev-state                        # retained terminal attempts, including LOST
# Return to the ordinary lab afterward:
make dev-up COMPOSE=podman-compose
```

Use `.venv/bin/python -m local_test.recovery --engine podman` (or `docker`) to
require a particular engine; default auto prefers Podman. Tests run sequentially
and use 0.5 CPU / 128 MiB workloads, with real workers contributing 1 CPU / 256 MiB.
Allow several minutes. Recovery identities use the same protected atomic storage
conventions in `.meshcompute-lab/recovery-workers.json`; no production credential
persistence changed. A successful run can be repeated with terminal history.
Failures preserve state; inspect it and wait for recovery or explicitly reset
only disposable data. Ctrl-C stops owned children; no automatic state reset.

| Check | Evidence |
| --- | --- |
| Renewal | Real 40-second sleep crosses its initial lease while RUNNING, deadline advances, heartbeat stays healthy, allocation remains until success |
| LEASED expiry | Diagnostic claim plus test-only SQL backdating; LOST, no allocation, QUEUED despite fresh real heartbeats |
| Hard death/retry | SIGKILL an owned real worker after renewal; LOST then a different worker executes attempt 2 successfully; test-owned orphan inspected and removed by matching label |
| Exhaustion | Two expired claims with max_attempts=2 end FAILED; no third attempt |
| Expiry authority/auth | Wrong/missing credentials rejected; stale renew/start/result cannot revive an expired attempt; stale result cannot alter attempt 2; workers cannot forge LOST |
| Restart | Owned controller stopped, persisted lease backdated; real ASGI endpoints without lifespan prove rejection before reaping; restarting the controller recovers automatically |
| Result/recovery ordering | API-only result fixture wins before recovery and survives historical expiry; competing DB recovery transactions preserve LOST against later result |
| Lease loss on worker | Controller outage reaches the monotonic deadline; a separate real workload gets explicit renewal rejection. Both remove their containers without stale terminal reports |

Backdating and direct API result fixtures are explicitly test setup, not simulated
engine validation. The real execution paths use production CLI/engine adapters.
The harness removes only its strictly labeled orphan fixtures; successful results
and LOST history remain in PostgreSQL. Existing `make test-scheduler` (explicit
reset) and `make test-execution` remain separate commands and retain their checks.
The optional Crostini `NETAVARK_FW=iptables` workaround above remains per-command,
not a global default. All controller/database guards stay localhost-only.

### Phase 6 migration

Migration 0006 adds the `(state, lease_expires_at)` recovery index and requires a
non-null deadline for active LEASED/RUNNING attempts. It deliberately fails if
pre-Phase6 active unleased attempts exist. Stop old controllers/workers and
inspect their reservations; use a separate database or explicitly reset a
confirmed disposable lab. No deadlines are invented for ambiguous old ownership.
Existing terminal history and saved identities remain compatible. Downgrade
refuses active leases or LOST history; test upgrade/check/downgrade 0004/upgrade/
check only on a separate empty disposable database, using the commands above.
