import hashlib
import secrets
from typing import Annotated
from uuid import UUID

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from controller.database import get_session
from controller.models.worker import Worker

bearer = HTTPBearer(auto_error=False)


def authenticated_worker(
    worker_id: UUID,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    session: Annotated[Session, Depends(get_session)],
) -> Worker:
    worker = session.get(Worker, worker_id)
    digest = hashlib.sha256(credentials.credentials.encode()).hexdigest() if credentials else ""
    if worker is None or not secrets.compare_digest(digest, worker.token_hash):
        raise HTTPException(
            status_code=401, detail="Invalid worker credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return worker
