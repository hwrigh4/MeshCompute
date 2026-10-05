import asyncio
import logging

import httpx

from common.schemas.assignments import WorkAssignment
from worker.config import WorkerSettings
from worker.preemption.control import ControlStore


class ClaimFailed(RuntimeError):
    pass


async def claim_once(settings: WorkerSettings) -> WorkAssignment | None:
    """Make one explicit claim; this function never launches or retries an assignment."""
    state = ControlStore(settings).read()
    if state.participation != 'HEALTHY' or state.cpu == 0 or state.memory_mb == 0:
        logging.getLogger(__name__).info('Local participation/contribution blocks claim; no controller request sent')
        return None
    try:
        async with httpx.AsyncClient(
            base_url=str(settings.controller_url).rstrip("/") + "/",
            headers={"Authorization": f"Bearer {settings.token.get_secret_value()}"},
            timeout=5, trust_env=False, follow_redirects=False,
        ) as client:
            sent = asyncio.get_running_loop().time()
            response = await client.post(f"v1/workers/{settings.id}/claim")
            response.raise_for_status()
            if response.status_code == 204:
                return None
            assignment = WorkAssignment.model_validate(response.json())
            assignment._provider_stop_sequence = state.stop_sequence
            assignment._lease_deadline = sent + assignment.lease_duration_seconds
            return assignment
    except httpx.HTTPStatusError as exc:
        raise ClaimFailed(f"Claim rejected (HTTP {exc.response.status_code}); check worker credentials and controller") from None
    except httpx.RequestError:
        raise ClaimFailed("Claim response unavailable; assignment may have committed. Inspect controller state before another claim") from None
    except ValueError:
        raise ClaimFailed("Invalid claim response; inspect controller state before another claim") from None
