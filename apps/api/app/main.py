import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.auth.middleware import SessionAuthMiddleware
from app.brain.lexical import warm_index
from app.brain.media import sweep_orphaned_media
from app.config import settings
from app.db import SessionLocal
from app.i18n import LOCALE_HEADER

log = logging.getLogger(__name__)
from app.jobs.sweep import (
    sweep_expired_records,
    sweep_interrupted_lessons,
    sweep_orphaned_jobs,
    sweep_stuck_compiles,
    sweep_stuck_ingests,
)
from app.llm.errors import LLMNotConfigured
from app.routers import (
    artifacts,
    auth,
    backup,
    blueprint,
    canon,
    chat,
    curriculum,
    health,
    jobs,
    knowledge,
    lessons,
    library,
    notes,
    prompts,
    settings as settings_router,
    students,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Three things, before the first request is served.

    1. Fail every `GenerationJob` left `pending`/`running` by a previous
       process's restart — see `app.jobs.sweep.sweep_orphaned_jobs`.

    1b. Put every lesson left `drafting` by that same restart back to `queued`,
       NOT `failed` (`sweep_interrupted_lessons`). A restart is not a bad lesson.
       Nothing is auto-enqueued here — there is no worker and no executor at
       startup; the tutor presses Resume, which is a REQUEST, which is the only
       thing in this app that can schedule a BackgroundTask.

    2. Warm the BM25 index (Plan 13, Stage 4.3). It is an in-process structure
       built by scanning every chunk; built LAZILY it would be built inside
       whichever request happened to search first — putting a corpus scan on the
       tutor's very first question and nowhere else, which is the classic "it's
       slow the first time and nobody can reproduce it" bug. `warm_index` never
       raises: a cold index is a slow first search, not a dead app.

    3. SET ASIDE page scans whose `KnowledgeSource` no longer exists (Stage 7.3,
       `app.brain.media.sweep_orphaned_media`). Every DELETE this app served
       before that module existed leaked its book's JPEGs — ~25MB per copy of
       the tutor's 77-page scan — and this boot pass is the only thing that can
       ever collect them. Cheap (one `listdir` of a directory with a handful of
       entries) and idempotent, so it costs nothing on the boots that find none.

       It RENAMES rather than deletes, into `<media_dir>/_superseded/<stamp>/`,
       and reaps a batch thirty days later. That is not fussiness about a leaked
       JPEG: this is the only destructive path in the app driven by an inference
       ("no row refers to it") instead of an instruction, the inference is made
       against WHATEVER DATABASE THIS PROCESS IS CONNECTED TO, and a database
       that was replaced, restored or renamed makes every book the tutor owns an
       orphan at once — including each one's `source.pdf`, which is the original
       he uploaded and not a regenerable render. The desktop shell now moves
       `<data>/media` aside together with any database it displaces, so the two
       can no longer drift apart on a first run; this is the same guarantee for
       every drift nobody has thought of. See `app.brain.media`'s docstring.

    Opens and closes its own short-lived `SessionLocal()` (same "own session"
    reasoning as `run_curriculum_job`): this runs before any request could exist,
    so there is no request-scoped `Depends(get_db)` session to reuse. Nothing runs
    after `yield` — this app has no shutdown-time cleanup.
    """
    db = SessionLocal()
    try:
        sweep_orphaned_jobs(db)
        sweep_interrupted_lessons(db)
        sweep_stuck_compiles(db)
        sweep_stuck_ingests(db)
        sweep_orphaned_media(db)
        sweep_expired_records(db)
        warm_index(db)
    finally:
        db.close()

    # WARM THE EMBEDDER — the check `llm/embedder.py`'s docstring has always
    # promised ("fails at STARTUP with a clear error rather than at the tutor's
    # first question") but nothing actually performed. An image built without
    # the baked ONNX weights used to boot green and then 500 on the first
    # search and fail every ingest, days after the broken build. Loud in the
    # log, but NON-FATAL: chat/curriculum (BM25 + full-context) still work
    # without dense embeddings, and a dead app helps the tutor even less than
    # a degraded one.
    try:
        from app.llm.embed_factory import get_embedder

        get_embedder().embed(["warm-up"], is_query=True)
        log.info("embedding model warmed OK")
    except Exception:
        log.exception(
            "EMBEDDING MODEL FAILED TO LOAD — dense retrieval is DOWN "
            "(search/grounding degrade to BM25). Rebuild the image with the "
            "baked e5 weights (see apps/api/Dockerfile)."
        )
    yield


app = FastAPI(title="Guitar Tutor Copilot API", lifespan=lifespan)


@app.exception_handler(LLMNotConfigured)
async def _llm_not_configured(request: Request, exc: LLMNotConfigured) -> JSONResponse:
    """409, never a 500 — see `llm/errors.py::LLMNotConfigured`.

    An app-wide handler rather than a try/except per router, because
    `get_provider()` is called from six modules and the exception can surface
    from any request that reaches one of them. The point of the handler is that
    NOTHING has to remember: no call site can accidentally let this become a
    stack trace in a non-technical user's browser on the very first day.
    """
    return JSONResponse(
        status_code=409,
        content={"detail": {"code": "llm_not_configured", "message": str(exc)}},
    )


# ORDER IS LOAD-BEARING. Starlette PREPENDS on `add_middleware`, so the LAST one
# added is the OUTERMOST — and CORS must be outermost, or the auth gate's 401
# leaves the stack without an `Access-Control-Allow-Origin` header, which the
# browser reports as an unreadable `TypeError: Failed to fetch` with no status
# and nothing in the API log. See `app/auth/middleware.py`, point 3, and the
# regression test that asserts ACAO is present on the 401.
app.add_middleware(SessionAuthMiddleware)

# The app owns CORS (spec §5.4): the browser calls the API directly (REST + SSE),
# so allow the web origin(s). Allowlist, not "*", because credentials are allowed.
#
# `allow_headers` is an EXPLICIT list, not `["*"]` (Plan 13, Stage 5.1). Starlette's
# wildcard works by echoing whatever `Access-Control-Request-Headers` the preflight
# asked for, which is fine but untestable-by-inspection: nothing anywhere would tell
# you whether `X-App-Locale` — the header EVERY request now carries, including the
# SSE POST — is actually allowed. It is a custom header, so it is NOT a CORS
# "simple" header: get it wrong and every single browser call fails preflight, with
# no server log to show for it. Named here, and pinned by a test.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.cors_origins.split(",") if o.strip()],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["Content-Type", "Authorization", "Accept", LOCALE_HEADER],
)

app.include_router(health.router)
app.include_router(auth.router)
app.include_router(settings_router.router)
app.include_router(knowledge.router)
app.include_router(curriculum.router)
app.include_router(students.router)
app.include_router(artifacts.router)
app.include_router(jobs.router)
app.include_router(chat.router)
app.include_router(notes.router)
app.include_router(library.router)
app.include_router(lessons.router)
app.include_router(prompts.router)
app.include_router(blueprint.router)
app.include_router(canon.router)
app.include_router(backup.router)
