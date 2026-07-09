from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.routers import artifacts, curriculum, health, jobs, knowledge, students

app = FastAPI(title="Guitar Tutor Copilot API")

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
