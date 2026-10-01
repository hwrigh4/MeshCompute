from common.schemas.workers import ExecutorCapabilities
from worker.executors.container import engine_usable


class DockerExecutor:
    def __init__(self, socket_path: str):
        self.socket_path = socket_path

    async def capabilities(self) -> ExecutorCapabilities:
        return ExecutorCapabilities(container=await engine_usable(self.socket_path))
