"""Emulator callbacks (C-03b) end to end: mutation → outbox → signed POST to CDMS → inbox → worker.

Both apps run in-process over ASGI transports on the test database; nothing is mocked in between.
"""

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from cdms.api.main import create_app as create_cdms
from cdms.config import get_settings
from cdms.core.canonical import canonical_product
from cdms.core.fingerprint import fingerprint
from cdms.db.models import InboxEvent, Product, ProductChange
from cdms.db.session import get_session
from cdms.emulator.callbacks import deliver_due
from cdms.emulator.main import create_app as create_emulator
from cdms.emulator.models import VietfulCallback, VietfulSettings
from cdms.ingestion.polling import Trigger, run_poll
from cdms.jobs.runner import run_job_batch

ENDPOINT = "http://cdms/api/v1/webhooks/vietful"
AUTH = {"Authorization": "Bearer test-emulator-token"}


def override(engine: AsyncEngine) -> Any:
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def test_session() -> AsyncIterator[AsyncSession]:
        async with sessions() as session:
            yield session

    return test_session


class FlakyTransport(httpx.AsyncBaseTransport):
    """Answers 503 for the first `failures` requests, then forwards to the real app."""

    def __init__(self, inner: httpx.AsyncBaseTransport, failures: int) -> None:
        self.inner = inner
        self.failures = failures
        self.seen = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.seen += 1
        if self.seen <= self.failures:
            return httpx.Response(503, text="CDMS restarting")
        return await self.inner.handle_async_request(request)


class World:
    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine
        self.sessions = async_sessionmaker(engine, expire_on_commit=False)
        emulator = create_emulator()
        emulator.dependency_overrides[get_session] = override(engine)
        cdms = create_cdms()
        cdms.dependency_overrides[get_session] = override(engine)
        self.emulator_transport = httpx.ASGITransport(app=emulator)
        self.cdms_transport = httpx.ASGITransport(app=cdms)
        self.emulator = httpx.AsyncClient(transport=self.emulator_transport, base_url="http://emulator")

    async def admin(self, method: str, path: str, **body: Any) -> Any:
        resp = await self.emulator.request(method, path, json=body or None)
        assert resp.status_code < 300, resp.text
        return resp.json()

    async def deliver(self, transport: httpx.AsyncBaseTransport | None = None) -> int:
        return await deliver_due(self.sessions, transport=transport or self.cdms_transport)

    async def process(self) -> None:
        while await run_job_batch(self.engine, "w1"):
            pass

    async def callbacks(self) -> list[Any]:
        async with self.engine.connect() as conn:
            return list((await conn.execute(select(VietfulCallback).order_by(VietfulCallback.id))).all())


@pytest.fixture
async def world(db_engine: AsyncEngine) -> AsyncIterator[World]:
    async with db_engine.begin() as conn:
        await conn.execute(
            text("TRUNCATE inbox_event, job, product_changes, products, poll_run RESTART IDENTITY")
        )
    w = World(db_engine)
    await w.admin("PUT", "/_admin/faults", mode="ok")
    await w.admin("PUT", "/_admin/callback", duplicateRate=0, maxRetries=5, retryDelayMs=0, concurrency=4)
    await w.admin("POST", "/_admin/seed", count=10, seed=42)
    resp = await w.emulator.post("/api/v1/WebhookSubscribers", json={"endpoint": ENDPOINT}, headers=AUTH)
    assert resp.status_code == 204
    yield w
    await w.emulator.aclose()


async def test_mutation_webhook_reaches_cdms_and_polling_agrees(world: World) -> None:
    await run_poll(
        world.engine, trigger=Trigger.MANUAL, transport=world.emulator_transport
    )  # CDMS has v1 of all
    mutated = (await world.admin("POST", "/_admin/mutate", count=3, seed=5, notify="webhook"))["mutations"]
    assert [c.status for c in await world.callbacks()] == ["PENDING"] * 3

    assert await world.deliver() == 3
    callbacks = await world.callbacks()
    assert [(c.status, c.last_status, c.deliveries) for c in callbacks] == [("DELIVERED", 202, 1)] * 3
    await world.process()

    async with world.engine.connect() as conn:
        changes = (await conn.execute(select(ProductChange).where(ProductChange.version == 2))).all()
        states = {p.partner_sku: p for p in (await conn.execute(select(Product))).all()}
    assert sorted(c.partner_sku for c in changes) == sorted(m["partnerSKU"] for m in mutated)
    assert {c.source for c in changes} == {"WEBHOOK"}
    for m in mutated:  # CDMS holds exactly the emulator's new state
        assert states[m["partnerSKU"]].fingerprint == fingerprint(canonical_product(m["data"]))

    # Polling sees the same data afterwards and writes nothing (cross-mechanism duplicate). A rescan that
    # starts within the clock skew of the webhook's timestamp may count some items STALE instead of UNCHANGED
    # (ISSUES I-13) — either way no change row is written and nothing is reverted.
    rescan = await run_poll(world.engine, trigger=Trigger.MANUAL, transport=world.emulator_transport)
    assert rescan is not None
    assert rescan.counts["created"] == rescan.counts["updated"] == 0
    assert rescan.counts["unchanged"] + rescan.counts["stale"] == 10
    async with world.engine.connect() as conn:
        assert len((await conn.execute(select(ProductChange))).all()) == 10 + 3


async def test_duplicate_deliveries_are_absorbed(world: World) -> None:
    await world.admin(
        "PUT", "/_admin/callback", duplicateRate=1.0, maxRetries=5, retryDelayMs=0, concurrency=1
    )
    await world.admin("POST", "/_admin/mutate", count=4, seed=6, notify="webhook")

    await world.deliver()
    await world.process()

    assert [c.deliveries for c in await world.callbacks()] == [2] * 4  # every event sent twice
    async with world.engine.connect() as conn:
        inbox = (await conn.execute(select(InboxEvent))).all()
        changes = (await conn.execute(select(ProductChange))).all()
    assert len(inbox) == 4  # the second copy was answered 200 duplicate
    assert len(changes) == 4  # products unknown to CDMS before: one CREATED each, no more


async def test_failed_delivery_is_retried_until_cdms_answers(world: World) -> None:
    await world.admin("POST", "/_admin/mutate", count=1, seed=7, notify="webhook")
    flaky = FlakyTransport(world.cdms_transport, failures=2)

    await world.deliver(flaky)
    [first] = await world.callbacks()
    assert (first.status, first.attempts, first.last_status) == ("PENDING", 1, 503)
    await world.deliver(flaky)
    await world.deliver(flaky)

    [done] = await world.callbacks()
    assert (done.status, done.attempts, done.deliveries, done.last_status) == ("DELIVERED", 2, 3, 202)
    await world.process()
    async with world.engine.connect() as conn:
        assert len((await conn.execute(select(ProductChange))).all()) == 1


async def test_retry_waits_for_the_configured_delay(world: World) -> None:
    await world.admin(
        "PUT", "/_admin/callback", duplicateRate=0, maxRetries=5, retryDelayMs=60_000, concurrency=1
    )
    await world.admin("POST", "/_admin/mutate", count=1, seed=8, notify="webhook")
    flaky = FlakyTransport(world.cdms_transport, failures=1)

    assert await world.deliver(flaky) == 1
    assert await world.deliver(flaky) == 0  # not due for a minute


async def test_sender_gives_up_after_max_retries(world: World) -> None:
    await world.admin("PUT", "/_admin/callback", duplicateRate=0, maxRetries=1, retryDelayMs=0, concurrency=1)
    await world.admin("POST", "/_admin/mutate", count=1, seed=9, notify="webhook")
    down = FlakyTransport(world.cdms_transport, failures=100)

    await world.deliver(down)
    await world.deliver(down)
    assert await world.deliver(down) == 0

    [failed] = await world.callbacks()
    assert (failed.status, failed.attempts) == ("FAILED", 2)
    listed = await world.admin("GET", "/_admin/callbacks?status=FAILED")
    assert [c["eventId"] for c in listed["items"]] == [failed.event_id]


async def test_notify_needs_a_subscriber(world: World) -> None:
    async with world.engine.begin() as conn:
        await conn.execute(update(VietfulSettings).values(webhook_endpoint=None))
    resp = await world.emulator.post("/_admin/mutate", json={"count": 1, "notify": "webhook"})
    assert resp.status_code == 409
    assert resp.json()["code"] == "NO_SUBSCRIBER"
    assert await world.callbacks() == []  # the mutation was rolled back with it


async def test_without_secret_nothing_is_sent(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    await world.admin("POST", "/_admin/mutate", count=1, seed=10, notify="webhook")
    monkeypatch.setattr(get_settings(), "webhook_secret", None)

    await world.deliver()

    [callback] = await world.callbacks()
    assert (callback.status, callback.deliveries) == ("PENDING", 0)
    assert "WEBHOOK_SECRET" in callback.last_error


async def test_seed_clears_the_outbox(world: World) -> None:
    await world.admin("POST", "/_admin/mutate", count=2, notify="webhook")
    await world.admin("POST", "/_admin/seed", count=5, seed=1)
    assert await world.callbacks() == []
