from fastapi import APIRouter

router = APIRouter(prefix="/health", tags=["Health"])


@router.get("/live")
async def liveness() -> dict[str, str]:
    """Confirm that the application can serve requests."""
    return {"status": "ok"}
