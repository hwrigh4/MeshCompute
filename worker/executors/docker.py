import httpx

from common.schemas.workers import ExecutorCapabilities


class DockerExecutor:
    def __init__(self, socket_path: str):
        self.socket_path = socket_path

    async def capabilities(self) -> ExecutorCapabilities:
        # A CLI binary or socket file alone does not establish daemon usability.
        transport = httpx.AsyncHTTPTransport(uds=self.socket_path)
        try:
            async with httpx.AsyncClient(
                transport=transport, base_url="http://docker", timeout=2, trust_env=False,
            ) as client:
                ping = await client.get("/_ping")
                ping.raise_for_status()
                info = await client.get("/info")
                info.raise_for_status()
                usable = ping.text.strip() == "OK" and info.json().get("OSType") == "linux"
                return ExecutorCapabilities(container=usable)
        except (httpx.HTTPError, OSError, ValueError, AttributeError):
            return ExecutorCapabilities(container=False)
