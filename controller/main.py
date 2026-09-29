from fastapi import FastAPI

from controller.api import health, workers

app = FastAPI(title="MeshCompute", version="0.1.0")
app.include_router(health.router)
app.include_router(workers.router)
