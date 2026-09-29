"""Scheduled polling of the inventory Products API (decision D8, docs/architecture.md "Scheduled polling").

A run scans every page (Vietful has no "changed since" filter), turns each page into observations and applies
them through the single change pipeline — one transaction per page, together with the run's counters.
Re-scanning is harmless: unchanged products are UNCHANGED. Only one run at a time across all worker processes
(advisory lock).
"""

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

import httpx
from sqlalchemy import func, insert, select, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from cdms.config import get_settings
from cdms.core.canonical import InvalidProductError
from cdms.core.pipeline import Outcome, ProductObservation, Source, apply_observations
from cdms.db.models import PollRun, SyncConfig
from cdms.ingestion.inventory_client import ClientConfig, InventoryClient, InventoryError, Sleep
from cdms.logs import bind

log = logging.getLogger(__name__)

# pg_try_advisory_lock key reserved for the poller ("CDMS" poll, arbitrary but fixed).
POLL_LOCK_KEY = 0x43444D5301
# Deadlock / serialization failure: roll back and redo the page (docs/exactly-once.md).
_RETRY_SQLSTATES = {"40P01", "40001"}
_PAGE_TX_ATTEMPTS = 3

Clock = Callable[[], datetime]


class Trigger(StrEnum):
    SCHEDULE = "SCHEDULE"
    MANUAL = "MANUAL"


class RunStatus(StrEnum):
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    ABORTED = "ABORTED"


@dataclass(frozen=True)
class PollResult:
    run_id: int
    status: RunStatus
    pages: int
    counts: dict[str, int]  # created / updated / unchanged / stale / invalid
    error: str | None = None


def utc_now() -> datetime:
    return datetime.now(UTC)


async def load_config(engine: AsyncEngine) -> SyncConfig:
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        config = await session.get(SyncConfig, 1)
    if config is None:
        raise RuntimeError("sync_config row is missing (run `alembic upgrade head`)")
    return config


async def run_poll(
    engine: AsyncEngine,
    *,
    trigger: Trigger = Trigger.SCHEDULE,
    transport: httpx.AsyncBaseTransport | None = None,
    now: Clock = utc_now,
    sleep: Sleep = asyncio.sleep,
) -> PollResult | None:
    """Scan all pages once. Returns None when another run holds the lock (the tick is skipped)."""
    # Session-level advisory lock on a dedicated connection, held for the whole run. If this process dies the
    # connection closes and PostgreSQL releases the lock, so a crashed run never blocks polling forever.
    async with engine.connect() as lock_conn:
        acquired: bool = (
            await lock_conn.execute(select(func.pg_try_advisory_lock(POLL_LOCK_KEY)))
        ).scalar_one()
        await lock_conn.commit()  # the session lock survives the commit; no transaction stays open
        if not acquired:
            log.info("poll skipped: another run is in progress")
            return None
        try:
            return await _run_locked(engine, trigger, transport, now, sleep)
        finally:
            try:
                await lock_conn.execute(select(func.pg_advisory_unlock(POLL_LOCK_KEY)))
                await lock_conn.commit()
            except DBAPIError:
                log.warning("could not release the poll lock; it is released when the connection closes")


async def _run_locked(
    engine: AsyncEngine,
    trigger: Trigger,
    transport: httpx.AsyncBaseTransport | None,
    now: Clock,
    sleep: Sleep,
) -> PollResult:
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    config = await load_config(engine)

    async with sessions() as session, session.begin():
        # We hold the lock, so any RUNNING run belongs to a process that died mid-scan (host crash, kill).
        await session.execute(
            update(PollRun)
            .where(PollRun.status == RunStatus.RUNNING)
            .values(
                status=RunStatus.ABORTED, finished_at=func.now(), error="process stopped before the run ended"
            )
        )
        run_id = (
            await session.execute(insert(PollRun).values(trigger=trigger).returning(PollRun.id))
        ).scalar_one()

    with bind(
        run_id=run_id, trigger=str(trigger)
    ):  # every line of the run, the HTTP client's retries included
        return await _scan(sessions, config, run_id, transport, now, sleep)


async def _scan(
    sessions: async_sessionmaker[AsyncSession],
    config: SyncConfig,
    run_id: int,
    transport: httpx.AsyncBaseTransport | None,
    now: Clock,
    sleep: Sleep,
) -> PollResult:
    counts = dict.fromkeys(("created", "updated", "unchanged", "stale", "invalid"), 0)
    pages = 0
    try:
        client_config = _client_config(config)
        async with InventoryClient(client_config, transport=transport, sleep=sleep) as client:
            page_index = 0
            while True:
                # observed_at = when the request started: the page shows the state at that time or later, so
                # this never claims more freshness than we have (D4).
                observed_at = now()
                items = await client.list_products(page_index, config.poll_page_size)
                page_counts = await _apply_page(sessions, run_id, page_index, items, observed_at)
                pages += 1
                for key, value in page_counts.items():
                    counts[key] += value
                if len(items) < config.poll_page_size:  # short / empty page = end of the catalogue
                    break
                page_index += 1
    except Exception as exc:
        error = _describe(exc)
        await _finish(sessions, run_id, RunStatus.FAILED, error)
        log.error("poll run %d failed after %d pages: %s", run_id, pages, error)
        if not isinstance(exc, InventoryError | DBAPIError):
            raise
        return PollResult(run_id, RunStatus.FAILED, pages, counts, error)

    await _finish(sessions, run_id, RunStatus.SUCCEEDED, None)
    log.info(
        "poll run %d succeeded: %d pages, %s",
        run_id,
        pages,
        counts,
        extra={"pages": pages, "outcome": counts},
    )
    return PollResult(run_id, RunStatus.SUCCEEDED, pages, counts)


async def _apply_page(
    sessions: async_sessionmaker[AsyncSession],
    run_id: int,
    page_index: int,
    items: list[dict[str, Any]],
    observed_at: datetime,
) -> dict[str, int]:
    """One transaction: the page's changes and the run's counters commit together or not at all."""
    source_ref = f"poll_run:{run_id}:page:{page_index}"
    observations: list[ProductObservation] = []
    invalid = 0
    for position, item in enumerate(items):
        try:
            observations.append(
                ProductObservation.from_raw(
                    item, observed_at=observed_at, source=Source.POLLING, source_ref=source_ref
                )
            )
        except InvalidProductError as exc:
            invalid += 1
            log.warning("poll run %d page %d item %d invalid: %s", run_id, page_index, position, exc)

    for attempt in range(1, _PAGE_TX_ATTEMPTS + 1):
        try:
            async with sessions() as session, session.begin():
                results = await apply_observations(session, observations)
                counts = {
                    "created": sum(r.outcome == Outcome.CREATED for r in results),
                    "updated": sum(r.outcome == Outcome.UPDATED for r in results),
                    "unchanged": sum(r.outcome == Outcome.UNCHANGED for r in results),
                    "stale": sum(r.outcome == Outcome.STALE for r in results),
                    "invalid": invalid,
                }
                await session.execute(
                    update(PollRun)
                    .where(PollRun.id == run_id)
                    .values(
                        pages=PollRun.pages + 1,
                        items=PollRun.items + len(items),
                        **{key: getattr(PollRun, key) + value for key, value in counts.items()},
                    )
                )
            return counts
        except DBAPIError as exc:
            if attempt == _PAGE_TX_ATTEMPTS or not _retryable_db_error(exc):
                raise
            log.warning(
                "poll run %d page %d: %s, retrying the page", run_id, page_index, type(exc.orig).__name__
            )
    raise AssertionError("unreachable")  # pragma: no cover


async def _finish(
    sessions: async_sessionmaker[AsyncSession], run_id: int, status: RunStatus, error: str | None
) -> None:
    try:
        async with sessions() as session, session.begin():
            await session.execute(
                update(PollRun)
                .where(PollRun.id == run_id)
                .values(status=status, error=error, finished_at=func.now())
            )
    except DBAPIError:
        # Database unreachable: the run stays RUNNING and the next run marks it ABORTED.
        log.exception("could not record the end of poll run %d", run_id)


def _client_config(config: SyncConfig) -> ClientConfig:
    settings = get_settings()
    if settings.inventory_api_token is None:
        raise InventoryError("INVENTORY_API_TOKEN is not set")
    return ClientConfig(
        base_url=settings.inventory_base_url,
        token=settings.inventory_api_token.get_secret_value(),
        connect_timeout_s=config.http_connect_timeout_ms / 1000,
        read_timeout_s=config.http_read_timeout_ms / 1000,
        max_attempts=config.http_max_attempts,
    )


def _retryable_db_error(exc: DBAPIError) -> bool:
    # A unique violation means another writer won a race on a new product; redoing the page sees its row.
    return isinstance(exc, IntegrityError) or getattr(exc.orig, "sqlstate", None) in _RETRY_SQLSTATES


def _describe(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:1000]
