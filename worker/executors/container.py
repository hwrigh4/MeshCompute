import asyncio

import httpx

from common.schemas.workers import ContainerEngines, ExecutorCapabilities, RuntimeCapabilities


async def engine_usable(socket_path: str) -> bool:
    """Probe a Linux engine's compatible API without creating a workload."""
    transport = httpx.AsyncHTTPTransport(uds=socket_path)
    try:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://engine", timeout=2, trust_env=False,
        ) as client:
            ping = await client.get("/_ping")
            ping.raise_for_status()
            info = await client.get("/info")
            info.raise_for_status()
            return ping.text.strip() == "OK" and info.json().get("OSType") == "linux"
    except (httpx.HTTPError, OSError, ValueError, AttributeError):
        return False


class ContainerExecutor:
    def __init__(self, podman_socket: str, docker_socket: str, engine: str = "auto"):
        self.podman_socket = podman_socket
        self.docker_socket = docker_socket
        self.engine = engine

    async def capabilities(self) -> RuntimeCapabilities:
        podman, docker = await asyncio.gather(
            engine_usable(self.podman_socket), engine_usable(self.docker_socket),
        )
        return RuntimeCapabilities(
            executors=ExecutorCapabilities(container=podman or docker),
            container_engines=ContainerEngines(podman=podman, docker=docker),
            healthy=(podman or docker) if self.engine == "auto" else {"podman": podman, "docker": docker}[self.engine],
        )
