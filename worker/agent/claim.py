import httpx

from common.schemas.assignments import WorkAssignment
from worker.config import WorkerSettings


class ClaimFailed(RuntimeError):
    pass


async def claim_once(settings: WorkerSettings) -> WorkAssignment | None:
    """Make one explicit claim; Phase 4 never launches or retries an assignment."""
    try:
        async with httpx.AsyncClient(
            base_url=str(settings.controller_url).rstrip("/") + "/",
            headers={"Authorization": f"Bearer {settings.token.get_secret_value()}"},
            timeout=5, trust_env=False, follow_redirects=False,
        ) as client:
            response = await client.post(f"v1/workers/{settings.id}/claim")
            response.raise_for_status()
            if response.status_code == 204:
                return None
            return WorkAssignment.model_validate(response.json())
    except httpx.HTTPStatusError as exc:
        raise ClaimFailed(f"Claim rejected (HTTP {exc.response.status_code}); check worker credentials and controller") from None
    except httpx.RequestError:
        raise ClaimFailed("Claim response unavailable; assignment may have committed. Inspect controller state before another claim") from None
    except ValueError:
        raise ClaimFailed("Invalid claim response; inspect controller state before another claim") from None
