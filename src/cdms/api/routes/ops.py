"""Operational API: runtime config (`sync_config`, D9) and stats for the console and load tests."""

from fastapi import APIRouter
from sqlalchemy import func, select, update
from sqlalchemy.orm import InstrumentedAttribute

from cdms.api.deps import SessionDep
from cdms.db.models import InboxEvent, Job, PollRun, Product, ProductChange, SyncConfig
from cdms.schemas.api import PollRunOut, Stats, SyncConfigOut, SyncConfigUpdate

router = APIRouter(prefix="/api/v1", tags=["ops"])


@router.get("/config", response_model=SyncConfigOut, response_model_by_alias=True)
async def get_config(session: SessionDep) -> SyncConfigOut:
    return SyncConfigOut.model_validate(await _config(session))


@router.put("/config", response_model=SyncConfigOut, response_model_by_alias=True)
async def put_config(session: SessionDep, body: SyncConfigUpdate) -> SyncConfigOut:
    """Change the fields sent; the worker applies them on its next tick (no restart)."""
    changes = body.model_dump(exclude_unset=True, exclude_none=True)
    async with session.begin():
        if changes:
            await session.execute(update(SyncConfig).where(SyncConfig.id == 1).values(**changes))
        config = await _config(session)
    return SyncConfigOut.model_validate(config)


@router.get("/stats", response_model=Stats, response_model_by_alias=True)
async def stats(session: SessionDep) -> Stats:
    """Counts for the console / load tests: products, changes by source / type, inbox, jobs, last poll."""

    async def grouped(column: InstrumentedAttribute[str]) -> dict[str, int]:
        rows = await session.execute(select(column, func.count()).group_by(column))
        return {key: count for key, count in rows.all()}

    products = (await session.execute(select(func.count()).select_from(Product))).scalar_one()
    jobs = await grouped(Job.status)
    oldest = (
        await session.execute(
            select(func.extract("epoch", func.now() - func.min(Job.created_at))).where(
                Job.status == "PENDING"
            )
        )
    ).scalar_one()
    last_run = (
        await session.execute(select(PollRun).order_by(PollRun.id.desc()).limit(1))
    ).scalar_one_or_none()
    return Stats(
        products=products,
        changes_by_source=await grouped(ProductChange.source),
        changes_by_type=await grouped(ProductChange.change_type),
        inbox_by_status=await grouped(InboxEvent.status),
        jobs_by_status=jobs,
        job_backlog=jobs.get("PENDING", 0) + jobs.get("RUNNING", 0),
        oldest_pending_job_age_seconds=float(oldest) if oldest is not None else None,
        last_poll_run=PollRunOut.model_validate(last_run) if last_run else None,
    )


async def _config(session: SessionDep) -> SyncConfig:
    config = await session.get(SyncConfig, 1, populate_existing=True)
    if config is None:
        raise RuntimeError("sync_config row is missing (run `alembic upgrade head`)")
    return config
