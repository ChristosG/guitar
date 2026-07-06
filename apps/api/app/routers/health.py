from fastapi import APIRouter

router = APIRouter(prefix="/health", tags=["health"])

@router.get("/live")
def live():
    return {"status": "ok"}
