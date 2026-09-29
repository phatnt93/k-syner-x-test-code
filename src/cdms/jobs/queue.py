"""Queue operations on the `job` table (docs/database.md, decision D7).

- `enqueue` runs inside the caller's transaction, so a job exists exactly when the data it refers to exists.
- `claim` takes a batch in its own short transaction: `FOR UPDATE SKIP LOCKED` lets concurrent workers take
  different jobs, the lease (`locked_until`) hands a job to another worker if its owner dies, and `attempts`
  is counted here.
- `complete` / `retry_or_bury` run inside the transaction that did the work (or recorded the failure).
"""

from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum

from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from cdms.config import get_settings
from cdms.db.models import Job, SyncConfig

BACKOFF_BASE_S = 2
BACKOFF_MAX_S = 300


class JobKind(StrEnum):
    WEBHOOK_EVENT = "webhook_event"
    POLL_NOW = "poll_now"


class JobStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    DONE = "DONE"
    DEAD = "DEAD"


@dataclass(frozen=True)
class ClaimedJob:
    id: int
    kind: JobKind
    ref_id: str
    attempts: int  # including this one
    max_attempts: int
    worker_id: str

    @property
    def exhausted(self) -> bool:
        """Claimed too often: earlier attempts crashed their worker before recording a failure."""
        return self.attempts > self.max_attempts


async def enqueue(session: AsyncSession, kind: JobKind, ref_id: str) -> int | None:
    """Add a job; returns its id, or None if a job for (kind, ref_id) already exists."""
    max_attempts = select(SyncConfig.job_max_attempts).where(SyncConfig.id == 1).scalar_subquery()
    result = await session.execute(
        pg_insert(Job)
        .values(kind=kind, ref_id=ref_id, max_attempts=func.coalesce(max_attempts, 5))
        .on_conflict_do_nothing(index_elements=[Job.kind, Job.ref_id])
        .returning(Job.id)
    )
    return result.scalar_one_or_none()


async def claim(session: AsyncSession, worker_id: str, limit: int) -> list[ClaimedJob]:
    """Lease up to `limit` due jobs to `worker_id` (commit right after, to publish the lease)."""
    due = (
        select(Job.id)
        .where(
            Job.status.in_([JobStatus.PENDING, JobStatus.RUNNING]),
            Job.run_after <= func.now(),
            or_(Job.locked_until.is_(None), Job.locked_until < func.now()),
        )
        .order_by(Job.id)
        .limit(limit)
        .with_for_update(skip_locked=True)
        .scalar_subquery()
    )
    rows = await session.execute(
        update(Job)
        .where(Job.id.in_(due))
        .values(
            status=JobStatus.RUNNING,
            attempts=Job.attempts + 1,
            locked_by=worker_id,
            locked_until=func.now() + timedelta(seconds=get_settings().job_lease_seconds),
        )
        .returning(Job.id, Job.kind, Job.ref_id, Job.attempts, Job.max_attempts)
    )
    jobs = [ClaimedJob(r.id, JobKind(r.kind), r.ref_id, r.attempts, r.max_attempts, worker_id) for r in rows]
    return sorted(jobs, key=lambda job: job.id)  # arrival order


async def lock_owned(session: AsyncSession, worker_id: str, job_ids: list[int]) -> set[int]:
    """Row-lock the jobs still leased to us. A job whose lease expired and was re-claimed elsewhere is
    skipped, so
    it is never finished twice."""
    rows = await session.execute(
        select(Job.id)
        .where(Job.id.in_(job_ids), Job.locked_by == worker_id, Job.status == JobStatus.RUNNING)
        .order_by(Job.id)
        .with_for_update()
    )
    return set(rows.scalars())


async def complete(session: AsyncSession, job_ids: list[int], note: str | None = None) -> None:
    await session.execute(
        update(Job)
        .where(Job.id.in_(job_ids))
        .values(status=JobStatus.DONE, locked_by=None, locked_until=None, last_error=note)
    )


async def retry_or_bury(session: AsyncSession, job: ClaimedJob, error: str) -> JobStatus:
    """After a failed attempt: back to PENDING with exponential backoff, or DEAD when attempts are used up."""
    dead = job.attempts >= job.max_attempts
    delay = timedelta(seconds=min(BACKOFF_MAX_S, BACKOFF_BASE_S**job.attempts))
    status = JobStatus.DEAD if dead else JobStatus.PENDING
    await session.execute(
        update(Job)
        .where(Job.id == job.id)
        .values(
            status=status,
            locked_by=None,
            locked_until=None,
            last_error=error[:1000],
            run_after=func.now() if dead else func.now() + delay,
        )
    )
    return status
