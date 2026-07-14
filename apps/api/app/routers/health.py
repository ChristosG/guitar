from fastapi import APIRouter
from sqlalchemy import text

from app.db import SessionLocal
from app.llm.embed_factory import get_embedder
from app.llm.factory import get_provider

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/live")
def live():
    return {"status": "ok"}


@router.get("/ready")
def ready():
    """The `{db, llm, embed}` contract is UNCHANGED — only its authorship is.

    `embed` used to come from the chat provider's `health()`, back when one
    vLLM server served both. Since Plan 13 Task 1.1 split the seams (Claude has
    no embeddings endpoint), this endpoint composes the two independent probes
    itself. Same three keys, same meaning, so `README.md`'s documented
    `{"db":true,"llm":true,"embed":true}` check and every deploy script that
    greps it keep working.
    """
    db_ok = False
    try:
        with SessionLocal() as s:
            s.execute(text("SELECT 1"))
            db_ok = True
    except Exception:
        pass
    return {
        "db": db_ok,
        "llm": get_provider().health()["llm"],
        "embed": get_embedder().health(),
    }
