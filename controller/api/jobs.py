from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from sqlalchemy import select
from sqlalchemy.orm import Session

from common.schemas.jobs import JobSubmission, JobView
from controller.database import get_session
from controller.models.job import Job
from controller.services import jobs


class JobRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def validated_request(request: Request) -> Response:
            try:
                return await handler(request)
            except RequestValidationError as exc:
                # Echoing rejected NaN/Infinity inputs would make the error JSON
                # itself invalid. Return locations/messages without raw inputs.
                errors = [
                    {key: error[key] for key in ("loc", "msg", "type")}
                    for error in exc.errors()
                ]
                return JSONResponse(status_code=422, content={"detail": errors})

        return validated_request


router = APIRouter(prefix="/v1/jobs", tags=["jobs"], route_class=JobRoute)
DatabaseSession = Annotated[Session, Depends(get_session)]


@router.post("", response_model=JobView, status_code=status.HTTP_201_CREATED)
def create_job(submission: JobSubmission, session: DatabaseSession) -> Job:
    return jobs.create_job(session, submission)


@router.get("", response_model=list[JobView])
def list_jobs(
    session: DatabaseSession,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[Job]:
    return list(session.scalars(
        select(Job).order_by(Job.created_at, Job.id).limit(limit).offset(offset)
    ))


@router.get("/{job_id}", response_model=JobView)
def get_job(job_id: UUID, session: DatabaseSession) -> Job:
    job = session.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@router.post("/{job_id}/cancel", response_model=JobView)
def cancel_job(job_id: UUID, session: DatabaseSession) -> Job:
    try:
        return jobs.cancel_job(session, job_id)
    except jobs.JobNotFound:
        raise HTTPException(status_code=404, detail="Job not found") from None
    except jobs.JobCancellationConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
