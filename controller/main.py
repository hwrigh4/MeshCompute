import asyncio
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI
from common.logging import configure_logging
from controller import metrics
from controller.services.recovery import recovery_loop

from controller.api import attempts, health, jobs, workers
from controller.api.body_limit import AttemptBodyLimit


@asynccontextmanager
async def lifespan(app):
    configure_logging()
    task = asyncio.create_task(recovery_loop())
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


app = FastAPI(title="MeshCompute", version="0.1.0", lifespan=lifespan)
app.include_router(health.router)
app.include_router(workers.router)
app.include_router(jobs.router)

app.add_middleware(AttemptBodyLimit)
app.include_router(attempts.router)

app.include_router(metrics.router)
app.add_middleware(metrics.RequestMetrics)
