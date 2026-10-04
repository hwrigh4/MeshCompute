"""Explicit, sequential local-engine validation; never called by a worker."""
import json
from fractions import Fraction
import shutil
import signal
import subprocess
from uuid import uuid4

from local_test.examples import NAMES

RESTRICTIONS = ["--network=none", "--cpus=1", "--memory=128m", "--pids-limit=64",
                "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges"]
PROBE = """
import json
from pathlib import Path
root = Path('/sys/fs/cgroup')
print(json.dumps({name: (root / name).read_text().strip()
                  for name in ('cpu.max', 'memory.max', 'pids.max')}))
"""


class CheckFailed(RuntimeError):
    pass


class CleanupFailed(CheckFailed):
    pass


def command(args, timeout=15):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


def restricted_run(image_name, argv=()):
    engine = ["podman", "--remote=false"]
    image = f"localhost/meshcompute-{image_name}:dev"
    exists = command([*engine, "image", "exists", image])
    if exists.returncode == 1 and not exists.stderr.strip():
        raise CheckFailed(f"Missing {image}; run make examples-build-podman")
    if exists.returncode:
        raise CheckFailed(f"Podman image lookup failed: {exists.stderr.strip()}")
    name = f"meshcompute-check-{uuid4().hex}"
    try:
        created = command([*engine, "create", "--name", name, "--pull=never",
                           *RESTRICTIONS, image, *argv])
        if created.returncode:
            raise CheckFailed(f"Container creation failed: {created.stderr.strip()}")
        result = command([*engine, "start", "--attach", name], timeout=30)
        inspected = command([*engine, "inspect", name])
        if inspected.returncode:
            raise CheckFailed("Cannot inspect container outcome")
        state = json.loads(inspected.stdout)[0]["State"]
        if (state.get("Running") or state.get("Error") or state.get("OOMKilled")
                or not state.get("StartedAt") or state["StartedAt"].startswith("0001-")
                or result.returncode in (125, 126, 127)
                or state.get("ExitCode") != result.returncode):
            raise CheckFailed(f"Engine/startup/OOM failure (CLI exit {result.returncode}); {result.stderr.strip()}")
        return result
    finally:
        # Only this invocation's random name is removed, even after a failed create.
        try:
            removed = command([*engine, "rm", "--force", "--ignore", name])
        except (OSError, subprocess.TimeoutExpired):
            raise CleanupFailed(f"Cleanup unavailable/timed out; inspect/remove only container {name}") from None
        if removed.returncode:
            raise CleanupFailed(f"Cleanup failed; inspect/remove only container {name}")


def validate_limits(output):
    values = json.loads(output)
    quota, period = values["cpu.max"].split()
    if int(quota) <= 0 or int(period) <= 0 or Fraction(int(quota), int(period)) != 1:
        raise CheckFailed(f"CPU limit mismatch: {values['cpu.max']}")
    if values["memory.max"] != "134217728" or values["pids.max"] != "64":
        raise CheckFailed(f"Memory/PID limit mismatch: {values}")
    return values


def interrupted(signum, frame):
    raise KeyboardInterrupt


def main_check(action, engine):
    if engine != "podman":
        raise SystemExit("These real-environment checks currently require Podman")
    if not shutil.which("podman"):
        raise SystemExit("BLOCKED: Podman not installed; no containers run")
    previous = signal.signal(signal.SIGTERM, interrupted)
    failures = 0
    checked = 0
    names = NAMES if action == "run" else ("success",)
    try:
        for name in names:
            checked += 1
            label = name if action == "run" else "cgroup v2 limits"
            try:
                result = restricted_run(name, () if action == "run" else ("python", "-c", PROBE))
                expected = 7 if action == "run" and name == "failure" else 0
                if result.returncode != expected:
                    raise CheckFailed(f"Expected workload exit {expected}, got {result.returncode}")
                if name == "failure" and "meshcompute example: intentional failure" not in result.stderr:
                    raise CheckFailed("Exit 7 without the expected intentional-failure output")
                if action == "limits":
                    print(json.dumps(validate_limits(result.stdout), sort_keys=True))
                print(f"PASS {label} (exit {expected}" + (", intentional workload failure)" if expected else ")"))
            except CleanupFailed as exc:
                failures += 1
                print(f"FAIL {label}: {exc}")
                print("Stopping checks because the container may still be running.")
                break
            except (CheckFailed, OSError, ValueError, KeyError, IndexError, TypeError, subprocess.TimeoutExpired) as exc:
                failures += 1
                print(f"FAIL {label}: {exc}")
        print(f"{checked - failures}/{len(names)} checks passed; {failures} failed")
        if checked < len(names):
            print(f"{len(names) - checked} checks skipped after cleanup failure")
        if action == "limits":
            print("Checks configured kernel limits only; not a stress test or comprehensive isolation audit.")
            if failures:
                print("Limits unavailable, unsupported, or mismatched; environment validation did not pass.")
    except KeyboardInterrupt:
        raise SystemExit("Interrupted; cleanup attempted for this check's container only") from None
    finally:
        signal.signal(signal.SIGTERM, previous)
    if failures:
        raise SystemExit(1)
