from typing import Annotated
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from common.auth.tokens import issue_worker_token
from common.schemas.workers import (
    RegisteredWorker, WorkerRegistration, WorkerView, WorkerHeartbeat, WorkerStatus,
)
from controller.api.auth import authenticated_worker
from controller.services.worker_health import worker_status
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


@router.get("", response_model=list[WorkerStatus])
def list_workers(
    session: DatabaseSession,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[WorkerStatus]:
    workers = session.scalars(
        select(Worker).order_by(Worker.created_at, Worker.id).limit(limit).offset(offset)
    )
    now = datetime.now(timezone.utc)
    return [worker_status(worker, now) for worker in workers]


@router.post("/{worker_id}/heartbeat", response_model=WorkerStatus)
def heartbeat(
    payload: WorkerHeartbeat,
    worker: Annotated[Worker, Depends(authenticated_worker)],
    session: DatabaseSession,
) -> WorkerStatus:
    worker.agent_version = payload.agent_version
    worker.last_heartbeat = datetime.now(timezone.utc)
    worker.state = payload.state
    worker.cpu_architecture = payload.cpu_architecture
    for field in (
        "cpu_physical", "cpu_physical_cores", "cpu_contributed",
        "memory_physical_mb", "memory_contributed_mb",
    ):
        setattr(worker, field, getattr(payload.resources, field))
    worker.executors = payload.executors.model_dump()
    worker.telemetry = payload.telemetry.model_dump()
    session.commit()
    session.refresh(worker)
    return worker_status(worker, datetime.now(timezone.utc))
