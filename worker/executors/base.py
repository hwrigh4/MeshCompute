from typing import Protocol

from common.schemas.workers import RuntimeCapabilities


class Executor(Protocol):
    """Runtime capability boundary; workload operations arrive in Phase 5."""

    async def capabilities(self) -> RuntimeCapabilities: ...
