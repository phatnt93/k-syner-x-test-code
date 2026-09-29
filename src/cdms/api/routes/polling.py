"""`POST /api/v1/polling/run` — ask the worker for a poll run now (docs/api.md)."""

import uuid

from fastapi import APIRouter
from sqlalchemy.exc import InterfaceError, OperationalError

from cdms.api.deps import SessionDep
from cdms.api.errors import AppError
from cdms.jobs.queue import JobKind, enqueue

router = APIRouter(prefix="/api/v1/polling", tags=["polling"])


@router.post("/run", status_code=202)
async def run_now(session: SessionDep) -> dict[str, int]:
    """Enqueue a manual poll; the worker runs it (skipped if a run is already in progress)."""
    try:
        async with session.begin():
            job_id = await enqueue(session, JobKind.POLL_NOW, str(uuid.uuid4()))
    except (OperationalError, InterfaceError) as exc:
        raise AppError(503, "UNAVAILABLE", "Database unavailable") from exc
    assert job_id is not None  # a fresh request id never conflicts
    return {"jobId": job_id}
