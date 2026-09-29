from typing import Protocol

from common.schemas.workers import ExecutorCapabilities


class Executor(Protocol):
    """Phase 2 runtime boundary; workload operations arrive in Phase 5."""

    async def capabilities(self) -> ExecutorCapabilities: ...
