from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from common.schemas.leases import LeaseRenewal
from common.schemas.attempts import AttemptResult, AttemptStart, AttemptView
from controller.api.auth import authenticated_worker
from controller.api.jobs import JobRoute
from controller.database import get_session
from controller.models.job_attempt import JobAttempt
from controller.models.worker import Worker
from controller.services import attempts

router = APIRouter(tags=['attempts'], route_class=JobRoute)
DatabaseSession = Annotated[Session, Depends(get_session)]
AssignedWorker = Annotated[Worker, Depends(authenticated_worker)]


@router.get('/v1/attempts/{attempt_id}', response_model=AttemptView)
def get_attempt(attempt_id: UUID, session: DatabaseSession):
    attempt = session.get(JobAttempt, attempt_id)
    if attempt is None:
        raise HTTPException(404, 'Attempt not found')
    return attempt


@router.post('/v1/workers/{worker_id}/attempts/{attempt_id}/start', response_model=AttemptView)
def start_attempt(attempt_id: UUID, payload: AttemptStart, worker: AssignedWorker, session: DatabaseSession):
    return attempts.start(session, worker.id, attempt_id, payload)


@router.post('/v1/workers/{worker_id}/attempts/{attempt_id}/result', response_model=AttemptView)
def finish_attempt(attempt_id: UUID, payload: AttemptResult, worker: AssignedWorker, session: DatabaseSession):
    return attempts.complete(session, worker.id, attempt_id, payload)


@router.post('/v1/workers/{worker_id}/attempts/{attempt_id}/renew', response_model=LeaseRenewal)
def renew_attempt(attempt_id: UUID, worker: AssignedWorker, session: DatabaseSession):
    return attempts.renew(session, worker.id, attempt_id)
