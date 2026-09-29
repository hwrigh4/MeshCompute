from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from common.auth.tokens import issue_worker_token
from common.schemas.workers import RegisteredWorker, WorkerRegistration, WorkerView
from controller.database import get_session
from controller.models.worker import Worker

router = APIRouter(prefix="/v1/workers", tags=["workers"])
DatabaseSession = Annotated[Session, Depends(get_session)]


@router.post("/register", response_model=RegisteredWorker, status_code=status.HTTP_201_CREATED)
def register_worker(
    registration: WorkerRegistration, response: Response, session: DatabaseSession
) -> RegisteredWorker:
    token, token_hash = issue_worker_token()
    worker = Worker(**registration.model_dump(), token_hash=token_hash)
    session.add(worker)
    session.commit()
    session.refresh(worker)
    response.headers["Cache-Control"] = "no-store"
    return RegisteredWorker(**WorkerView.model_validate(worker).model_dump(), token=token)


@router.get("", response_model=list[WorkerView])
def list_workers(
    session: DatabaseSession,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[Worker]:
    return list(
        session.scalars(
            select(Worker).order_by(Worker.created_at, Worker.id).limit(limit).offset(offset)
        )
    )
