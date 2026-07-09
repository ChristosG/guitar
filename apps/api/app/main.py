from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.db import SessionLocal
from app.jobs.sweep import sweep_orphaned_jobs
from app.routers import artifacts, curriculum, health, jobs, knowledge, students


@asynccontextmanager
async def lifespan(app: FastAPI):
    """On startup (before serving any request), fail every `GenerationJob`
    left `pending`/`running` by a previous process's restart — see
    `app.jobs.sweep.sweep_orphaned_jobs`'s own docstring for why this is
    needed at all. Opens and closes its own short-lived `SessionLocal()`
    (same "own session" reasoning as `run_curriculum_job`): this runs before
    any request could exist, so there is no request-scoped `Depends(get_db)`
    session to reuse. Nothing runs after `yield` — this app has no
    shutdown-time cleanup.
    """
    db = SessionLocal()
    try:
        sweep_orphaned_jobs(db)
    finally:
        db.close()
    yield


app = FastAPI(title="Guitar Tutor Copilot API", lifespan=lifespan)

# The app owns CORS (spec §5.4): the browser calls the API directly (REST + SSE),
# so allow the web origin(s). Allowlist, not "*", because credentials are allowed.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.cors_origins.split(",") if o.strip()],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router)
app.include_router(knowledge.router)
app.include_router(curriculum.router)
app.include_router(students.router)
app.include_router(artifacts.router)
app.include_router(jobs.router)
