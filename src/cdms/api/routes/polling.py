"""Polling API: ask the worker for a poll run now, and the poll run history (docs/api.md)."""

import uuid

from fastapi import APIRouter
from sqlalchemy import select

from cdms.api.deps import SessionDep
from cdms.api.paging import Cursor, Limit, int_cursor, next_cursor
from cdms.db.models import PollRun
from cdms.jobs.queue import JobKind, enqueue
from cdms.schemas.api import Page, PollRunOut

router = APIRouter(prefix="/api/v1/polling", tags=["polling"])


@router.post("/run", status_code=202)
async def run_now(session: SessionDep) -> dict[str, int]:
    """Enqueue a manual poll; the worker runs it (skipped if a run is already in progress)."""
    async with session.begin():  # database down → 503 via the app's error handler
        job_id = await enqueue(session, JobKind.POLL_NOW, str(uuid.uuid4()))
    assert job_id is not None  # a fresh request id never conflicts
    return {"jobId": job_id}


@router.get("/runs", response_model=Page[PollRunOut], response_model_by_alias=True)
async def list_runs(session: SessionDep, limit: Limit = 20, cursor: Cursor = None) -> Page[PollRunOut]:
    """Poll run history, newest first, with the counters of each run."""
    query = select(PollRun).order_by(PollRun.id.desc()).limit(limit + 1)
    if cursor:
        query = query.where(PollRun.id < int_cursor(cursor))
    rows = list((await session.execute(query)).scalars())
    page, nxt = next_cursor(rows, limit, lambda r: r.id)
    return Page[PollRunOut](items=[PollRunOut.model_validate(r) for r in page], next_cursor=nxt)
