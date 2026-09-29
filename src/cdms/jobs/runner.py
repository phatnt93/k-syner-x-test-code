"""Job runner of the worker: claim a batch, do the work, record the result (docs/webhook-solution.md §3).

Webhook events of a batch are processed together in one transaction (fewer commits, I-02). If that transaction
fails, each event is retried on its own, so one bad event cannot hold back the others; a failing event is
retried with backoff and finally marked DEAD.
"""

import asyncio
import contextlib
import logging
import os
import socket

import httpx
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from cdms.config import get_settings
from cdms.faults import crash
from cdms.ingestion import webhook
from cdms.ingestion.polling import Trigger, run_poll
from cdms.jobs import queue
from cdms.jobs.queue import ClaimedJob, JobKind, JobStatus

log = logging.getLogger(__name__)

BATCH_SIZE = 100
IDLE_S = 0.5
ERROR_BACKOFF_S = 5.0


def default_worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


async def run_job_batch(
    engine: AsyncEngine,
    worker_id: str,
    *,
    batch_size: int = BATCH_SIZE,
    poll_transport: httpx.AsyncBaseTransport | None = None,
) -> int:
    """Claim and run one batch; returns the number of jobs claimed (0 = queue empty)."""
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session, session.begin():
        jobs = await queue.claim(session, worker_id, batch_size)
    if not jobs:
        return 0

    exhausted = [job for job in jobs if job.exhausted]
    for job in exhausted:
        await _fail(sessions, job, "attempts exhausted (worker stopped during earlier attempts)")
    runnable = [job for job in jobs if not job.exhausted]

    events = [job for job in runnable if job.kind == JobKind.WEBHOOK_EVENT]
    if events:
        await _run_webhook_events(sessions, worker_id, events)
    for job in runnable:
        if job.kind == JobKind.POLL_NOW:
            await _run_poll_now(engine, sessions, worker_id, job, poll_transport)
    return len(jobs)


async def run_jobs(engine: AsyncEngine, stop: asyncio.Event, worker_id: str | None = None) -> None:
    """Worker loop: drain the queue, then check again every `IDLE_S`."""
    worker_id = worker_id or default_worker_id()
    while not stop.is_set():
        try:
            claimed = await run_job_batch(engine, worker_id)
            delay = 0.0 if claimed else IDLE_S
        except Exception:
            log.exception("job batch failed; retrying in %.0fs", ERROR_BACKOFF_S)
            delay = ERROR_BACKOFF_S
        if delay:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=delay)


async def _run_webhook_events(
    sessions: async_sessionmaker[AsyncSession], worker_id: str, jobs: list[ClaimedJob]
) -> None:
    try:
        await _process_events(sessions, worker_id, jobs)
        return
    except Exception as exc:
        if len(jobs) == 1:
            await _fail(sessions, jobs[0], _describe(exc))
            return
        log.warning(
            "webhook batch of %d failed (%s); processing the events one by one", len(jobs), _describe(exc)
        )
    for job in jobs:
        try:
            await _process_events(sessions, worker_id, [job])
        except Exception as exc:
            await _fail(sessions, job, _describe(exc))


async def _process_events(
    sessions: async_sessionmaker[AsyncSession], worker_id: str, jobs: list[ClaimedJob]
) -> None:
    async with sessions() as session, session.begin():
        owned = await queue.lock_owned(session, worker_id, [job.id for job in jobs])
        mine = [job for job in jobs if job.id in owned]
        if not mine:
            return
        outcomes = await webhook.process_events(session, [int(job.ref_id) for job in mine])
        await queue.complete(session, [job.id for job in mine])  # same transaction as the changes
        if get_settings().fault_crash_in_job_batch:
            crash(f"FAULT_CRASH_IN_JOB_BATCH: {len(mine)} events applied, dying before the commit")
    log.info("processed %d webhook events: %s", len(mine), _total(outcomes))


async def _run_poll_now(
    engine: AsyncEngine,
    sessions: async_sessionmaker[AsyncSession],
    worker_id: str,
    job: ClaimedJob,
    transport: httpx.AsyncBaseTransport | None,
) -> None:
    """A manual poll request. The poll records its result in `poll_run`; the job only says it was handled."""
    try:
        result = await run_poll(engine, trigger=Trigger.MANUAL, transport=transport)
    except Exception as exc:
        await _fail(sessions, job, _describe(exc))
        return
    note = "skipped: another poll run was in progress" if result is None else f"poll_run {result.run_id}"
    async with sessions() as session, session.begin():
        if await queue.lock_owned(session, worker_id, [job.id]):
            await queue.complete(session, [job.id], note)


async def _fail(sessions: async_sessionmaker[AsyncSession], job: ClaimedJob, error: str) -> None:
    async with sessions() as session, session.begin():
        if not await queue.lock_owned(session, job.worker_id, [job.id]):
            return  # the lease expired and another worker owns the job now
        status = await queue.retry_or_bury(session, job, error)
        if job.kind == JobKind.WEBHOOK_EVENT:
            await webhook.mark_failed(session, int(job.ref_id), error, dead=status == JobStatus.DEAD)
    log.error(
        "job %d (%s %s) attempt %d failed → %s: %s", job.id, job.kind, job.ref_id, job.attempts, status, error
    )


def _total(outcomes: dict[int, dict[str, int]]) -> dict[str, int]:
    total: dict[str, int] = {}
    for outcome in outcomes.values():
        for key, value in outcome.items():
            total[key] = total.get(key, 0) + value
    return total


def _describe(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:1000]
