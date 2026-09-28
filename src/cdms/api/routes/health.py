from fastapi import APIRouter
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from cdms.api.deps import SessionDep
from cdms.api.errors import AppError

router = APIRouter(tags=["ops"])


@router.get("/health")
async def health() -> dict[str, str]:
    """Liveness: the process answers; no dependency checks."""
    return {"status": "ok"}


@router.get("/ready")
async def ready(session: SessionDep) -> dict[str, str]:
    """Readiness: the database answers."""
    try:
        await session.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        raise AppError(503, "UNAVAILABLE", "Database unavailable") from exc
    return {"status": "ready"}
