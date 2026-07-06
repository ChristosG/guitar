from fastapi import APIRouter
from sqlalchemy import text

from app.db import SessionLocal
from app.llm.factory import get_provider

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/live")
def live():
    return {"status": "ok"}


@router.get("/ready")
def ready():
    db_ok = False
    try:
        with SessionLocal() as s:
            s.execute(text("SELECT 1"))
            db_ok = True
    except Exception:
        pass
    models = get_provider().health()
    return {"db": db_ok, "llm": models["llm"], "embed": models["embed"]}
