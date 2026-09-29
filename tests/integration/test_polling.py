"""Polling (C-04) end to end in one process.

The real emulator app (ASGI transport) serves the emulator schema of the test database; `run_poll` scans it
through the HTTP client and the change pipeline into `products`.

Every test starts from empty CDMS product / poll tables and a freshly seeded emulator.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from cdms.config import get_settings
from cdms.core.canonical import canonical_product
from cdms.core.fingerprint import fingerprint
from cdms.db.models import PollRun, Product, ProductChange, SyncConfig
from cdms.db.session import get_session
from cdms.emulator.main import create_app
from cdms.ingestion.polling import POLL_LOCK_KEY, RunStatus, Trigger, run_poll

PAGE_SIZE = 10
T0 = datetime(2026, 9, 29, 8, 0, tzinfo=UTC)


class Emulator:
    """The emulator app plus an admin client for it, sharing the test database."""

    def __init__(self, engine: AsyncEngine) -> None:
        sessions = async_sessionmaker(engine, expire_on_commit=False)

        async def test_session() -> AsyncIterator[AsyncSession]:
            async with sessions() as session:
                yield session

        app = create_app()
        app.dependency_overrides[get_session] = test_session
        self.transport = httpx.ASGITransport(app=app)
        self.admin = httpx.AsyncClient(transport=self.transport, base_url="http://emulator")

    async def call(self, method: str, path: str, **body: Any) -> Any:
        resp = await self.admin.request(method, path, json=body or None)
        assert resp.status_code < 300, resp.text
        return resp.json()

    async def products(self) -> list[dict[str, Any]]:
        resp = await self.admin.get(
            "/api/v1/Products",
            params={"PageSize": 1000},
            headers={"Authorization": "Bearer test-emulator-token"},
        )
        assert resp.status_code == 200, resp.text
        items: list[dict[str, Any]] = resp.json()
        return items


@pytest.fixture
async def emulator(db_engine: AsyncEngine) -> AsyncIterator[Emulator]:
    async with db_engine.begin() as conn:
        await conn.execute(text("TRUNCATE product_changes, products, poll_run RESTART IDENTITY"))
        await conn.execute(update(SyncConfig).values(poll_page_size=PAGE_SIZE, http_max_attempts=3))
    emu = Emulator(db_engine)
    await emu.call("PUT", "/_admin/faults", mode="ok")
    await emu.call("POST", "/_admin/seed", count=25, seed=42)
    yield emu
    await emu.call("PUT", "/_admin/faults", mode="ok")
    await emu.admin.aclose()


class Clock:
    def __init__(self) -> None:
        self.current = T0

    def __call__(self) -> datetime:
        self.current += timedelta(seconds=1)
        return self.current


async def no_sleep(_: float) -> None:
    return None


async def poll(engine: AsyncEngine, emu: Emulator, clock: Clock | None = None) -> Any:
    return await run_poll(
        engine, trigger=Trigger.MANUAL, transport=emu.transport, now=clock or Clock(), sleep=no_sleep
    )


async def scalar(engine: AsyncEngine, statement: Any) -> Any:
    async with engine.connect() as conn:
        return (await conn.execute(statement)).scalar_one()


async def poll_run(engine: AsyncEngine, run_id: int) -> Any:
    async with engine.connect() as conn:
        return (await conn.execute(select(PollRun).where(PollRun.id == run_id))).one()


async def test_first_poll_creates_every_product(db_engine: AsyncEngine, emulator: Emulator) -> None:
    result = await poll(db_engine, emulator)

    assert result.status == RunStatus.SUCCEEDED
    assert result.pages == 3  # 10 + 10 + 5
    assert result.counts == {"created": 25, "updated": 0, "unchanged": 0, "stale": 0, "invalid": 0}
    async with db_engine.connect() as conn:
        stored = {p.partner_sku: p.fingerprint for p in (await conn.execute(select(Product))).all()}
    expected = {p["partnerSKU"]: fingerprint(canonical_product(p)) for p in await emulator.products()}
    assert stored == expected
    run = await poll_run(db_engine, result.run_id)
    assert (run.status, run.pages, run.items, run.created) == ("SUCCEEDED", 3, 25, 25)
    assert run.finished_at is not None


async def test_repeated_poll_stores_nothing(db_engine: AsyncEngine, emulator: Emulator) -> None:
    await poll(db_engine, emulator)
    result = await poll(db_engine, emulator)

    assert result.counts["unchanged"] == 25
    assert await scalar(db_engine, select(func.count()).select_from(ProductChange)) == 25


async def test_poll_records_exactly_the_emulator_mutations(
    db_engine: AsyncEngine, emulator: Emulator
) -> None:
    await poll(db_engine, emulator)
    mutated = (await emulator.call("POST", "/_admin/mutate", count=6, seed=3))["mutations"]

    result = await poll(db_engine, emulator)

    assert result.counts["updated"] == 6 and result.counts["unchanged"] == 19
    async with db_engine.connect() as conn:
        updates = (
            await conn.execute(select(ProductChange).where(ProductChange.change_type == "UPDATED"))
        ).all()
    assert {c.partner_sku for c in updates} == {m["partnerSKU"] for m in mutated}
    by_sku = {m["partnerSKU"]: m for m in mutated}
    for change in updates:
        assert change.data == canonical_product(by_sku[change.partner_sku]["data"])
        assert change.source == "POLLING"
        assert change.source_ref.startswith(f"poll_run:{result.run_id}:page:")
        assert change.version == 2


async def test_observed_at_is_the_page_request_start(db_engine: AsyncEngine, emulator: Emulator) -> None:
    clock = Clock()
    await poll(db_engine, emulator, clock)
    async with db_engine.connect() as conn:
        observed = dict((await conn.execute(select(Product.partner_sku, Product.observed_at))).all())
    assert observed["P-00001"] == T0 + timedelta(seconds=1)  # page 0
    assert observed["P-00011"] == T0 + timedelta(seconds=2)  # page 1
    assert observed["P-00025"] == T0 + timedelta(seconds=3)  # page 2


async def test_exact_multiple_of_page_size_ends_on_an_empty_page(
    db_engine: AsyncEngine, emulator: Emulator
) -> None:
    await emulator.call("POST", "/_admin/seed", count=20, seed=42)
    result = await poll(db_engine, emulator)
    assert result.pages == 3 and result.counts["created"] == 20


async def test_inventory_down_fails_the_run_and_the_next_run_catches_up(
    db_engine: AsyncEngine, emulator: Emulator
) -> None:
    await emulator.call("PUT", "/_admin/faults", mode="down")
    failed = await poll(db_engine, emulator)

    assert failed.status == RunStatus.FAILED
    assert "after 3 attempts: HTTP 503" in failed.error
    run = await poll_run(db_engine, failed.run_id)
    assert (run.status, run.pages) == ("FAILED", 0)
    assert "503" in run.error

    await emulator.call("PUT", "/_admin/faults", mode="ok")
    recovered = await poll(db_engine, emulator)
    assert recovered.status == RunStatus.SUCCEEDED and recovered.counts["created"] == 25


async def test_invalid_items_are_counted_not_stored(db_engine: AsyncEngine, emulator: Emulator) -> None:
    await emulator.call("POST", "/_admin/products", items=[{"partnerSKU": "   "}])
    result = await poll(db_engine, emulator)
    assert result.counts["invalid"] == 1 and result.counts["created"] == 25


async def test_run_left_running_by_a_crash_is_aborted(db_engine: AsyncEngine, emulator: Emulator) -> None:
    async with db_engine.begin() as conn:
        crashed = (
            await conn.execute(text("INSERT INTO poll_run (trigger) VALUES ('SCHEDULE') RETURNING id"))
        ).scalar_one()

    await poll(db_engine, emulator)

    run = await poll_run(db_engine, crashed)
    assert run.status == "ABORTED" and run.finished_at is not None


async def test_only_one_run_at_a_time(db_engine: AsyncEngine, emulator: Emulator) -> None:
    async with db_engine.connect() as other_worker:
        await other_worker.execute(select(func.pg_advisory_lock(POLL_LOCK_KEY)))
        assert await poll(db_engine, emulator) is None
        await other_worker.execute(select(func.pg_advisory_unlock(POLL_LOCK_KEY)))
    assert (await poll(db_engine, emulator)).status == RunStatus.SUCCEEDED


async def test_missing_token_fails_the_run(
    db_engine: AsyncEngine, emulator: Emulator, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "inventory_api_token", None)
    result = await poll(db_engine, emulator)
    assert result.status == RunStatus.FAILED and "INVENTORY_API_TOKEN" in result.error
