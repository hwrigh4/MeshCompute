from fastapi import FastAPI

from controller.api import attempts, health, jobs, workers
from controller.api.body_limit import AttemptBodyLimit

app = FastAPI(title="MeshCompute", version="0.1.0")
app.include_router(health.router)
app.include_router(workers.router)
app.include_router(jobs.router)

app.add_middleware(AttemptBodyLimit)
app.include_router(attempts.router)
