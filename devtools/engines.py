"""Read-only diagnostics for real local Podman/Docker installations and sockets."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import shutil
import subprocess

import httpx

from worker.executors.container import engine_usable


async def check_engine(engine, socket_path):
    result = {"engine": engine, "installed": shutil.which(engine) is not None,
              "socket": socket_path, "socket_exists": Path(socket_path).exists(),
              "version": None, "rootless": None, "ping_ok": False, "info_ok": False}
    if result["installed"]:
        try:
            version = subprocess.run([engine, "--version"], capture_output=True, text=True, timeout=10)
            if version.returncode == 0:
                result["version"] = version.stdout.strip()
            # Probe CLI readiness as well, but inspect the explicitly chosen socket
            # separately: the CLI may use a different remote context.
            command = [engine, "info", "--format", "json" if engine == "podman" else "{{json .}}"]
            info = subprocess.run(command, capture_output=True, text=True, timeout=15)
            result["cli_info_ok"] = info.returncode == 0
            if info.returncode == 0:
                data = json.loads(info.stdout)
                result["cli_rootless"] = (data.get("host", {}).get("security", {}).get("rootless")
                    if engine == "podman" else any("rootless" in x for x in data.get("SecurityOptions", [])))
        except (OSError, subprocess.TimeoutExpired, ValueError):
            result["cli_info_ok"] = False
    try:
        transport = httpx.AsyncHTTPTransport(uds=socket_path)
        async with httpx.AsyncClient(transport=transport, base_url="http://engine", timeout=3, trust_env=False) as client:
            ping = await client.get("/_ping")
            result["ping_ok"] = ping.status_code == 200 and ping.text.strip() == "OK"
            info = await client.get("/info")
            info.raise_for_status()
            data = info.json()
            result["info_ok"] = isinstance(data, dict) and data.get("OSType") == "linux"
            result["rootless"] = any("rootless" in item for item in data.get("SecurityOptions", []))
    except (httpx.HTTPError, OSError, ValueError, AttributeError, TypeError):
        pass
    result["meshcompute_probe"] = await engine_usable(socket_path)
    result["validation"] = "PASS" if result["installed"] and result["version"] and result["meshcompute_probe"] else "PENDING"
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", choices=["podman", "docker", "all"], default="all")
    parser.add_argument("--socket", help="override API socket (requires a single --engine)")
    parser.add_argument("--require", action="store_true", help="fail if selected engine validation is pending")
    args = parser.parse_args()
    if args.socket and args.engine == "all":
        parser.error("--socket requires --engine podman or docker")
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    sockets = {"podman": os.environ.get("MESHCOMPUTE_WORKER_PODMAN_SOCKET", f"{runtime_dir}/podman/podman.sock"),
               "docker": os.environ.get("MESHCOMPUTE_WORKER_DOCKER_SOCKET", "/var/run/docker.sock")}
    names = list(sockets) if args.engine == "all" else [args.engine]
    reports = [asyncio.run(check_engine(name, args.socket or sockets[name])) for name in names]
    print(json.dumps(reports, indent=2))
    print("Read-only checks; nothing installed or configured. PENDING is not a successful real-engine validation.")
    if args.require and any(report["validation"] != "PASS" for report in reports):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
