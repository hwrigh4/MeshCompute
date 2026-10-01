"""Build OCI examples explicitly, or smoke-check their bounded Python sources."""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
NAMES = ("success", "failure", "sleep", "cpu-burn", "memory-hold", "monte-carlo")


def source_check():
    outputs = {}
    for name in NAMES:
        result = subprocess.run([sys.executable, str(ROOT / "examples" / name / "main.py")],
                                capture_output=True, text=True, timeout=10)
        expected = 7 if name == "failure" else 0
        if result.returncode != expected:
            raise SystemExit(f"FAIL {name}: expected exit {expected}, got {result.returncode}")
        outputs[name] = result
        print(f"PASS {name} source (exit {expected})")
    if outputs["success"].stdout != "meshcompute example: success\n":
        raise SystemExit("FAIL deterministic success output")
    if "intentional failure" not in outputs["failure"].stderr:
        raise SystemExit("FAIL failure stderr")
    repeat = subprocess.run([sys.executable, str(ROOT / "examples/monte-carlo/main.py")],
                            capture_output=True, text=True, timeout=10, check=True)
    if repeat.stdout != outputs["monte-carlo"].stdout:
        raise SystemExit("FAIL deterministic monte-carlo output")
    print("Monte Carlo result:", json.loads(repeat.stdout))
    for name, flag, value in (("memory-hold", "--mib", "0"), ("memory-hold", "--mib", "257"),
                             ("sleep", "--seconds", "61"), ("cpu-burn", "--seconds", "0"),
                             ("monte-carlo", "--samples", "1000001")):
        result = subprocess.run([sys.executable, str(ROOT / "examples" / name / "main.py"), flag, value],
                                capture_output=True, timeout=5)
        if result.returncode != 2:
            raise SystemExit(f"FAIL {name} bounds validation")
    print("PASS development workload bounds. Source checks do not validate OCI image builds or container isolation.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["check", "build"])
    parser.add_argument("--engine", choices=["podman", "docker"], default="podman")
    parser.add_argument("--name", choices=NAMES, help="build one example instead of all")
    args = parser.parse_args()
    if args.action == "check":
        source_check()
        return
    if not shutil.which(args.engine):
        raise SystemExit(f"PENDING: {args.engine} is not installed; OCI builds were not performed")
    for name in ([args.name] if args.name else NAMES):
        context = ROOT / "examples" / name
        subprocess.run([args.engine, "build", "-f", str(context / "Containerfile"),
                        "-t", f"localhost/meshcompute-{name}:dev", str(context)], check=True)
    print("Image builds complete. No images were run through MeshCompute.")


if __name__ == "__main__":
    main()
