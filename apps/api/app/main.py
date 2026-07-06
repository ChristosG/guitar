from fastapi import FastAPI
from app.routers import health

app = FastAPI(title="Guitar Tutor Copilot API")
app.include_router(health.router)
