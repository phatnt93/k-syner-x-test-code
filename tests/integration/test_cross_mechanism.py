"""Cross-mechanism duplicates (requirements §13.3, task C-09): the same product state reaching CDMS
through polling and webhook, in every order and concurrently, is stored once; an older state never
reverts a newer one.

The emulator, the CDMS webhook endpoint, the poller and the job runner run in-process on the test
database with nothing mocked in between. Webhook timestamps come from the emulator (database clock,
whole seconds); the poll clock is injected with a margin of several seconds so the order of the two
sources is explicit and the tests do not depend on clock skew (ISSUES I-13).
"""

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from cdms.api.main import create_app as create_cdms
from cdms.core.canonical import canonical_product
from cdms.core.fingerprint import fingerprint
from cdms.db.models import InboxEvent, Product, ProductChange
from cdms.db.session import get_session
from cdms.emulator.callbacks import deliver_due
from cdms.emulator.main import create_app as create_emulator
from cdms.ingestion.polling import PollResult, Trigger, run_poll
from cdms.ingestion.webhook import sign
from cdms.jobs.runner import run_job_batch

ENDPOINT = "http://cdms/api/v1/webhooks/vietful"
AUTH = {"Authorization": "Bearer test-emulator-token"}
SECRET = "test-webhook-secret"


def clock(offset: timedelta) -> Callable[[], datetime]:
    return lambda: datetime.now(UTC) + offset


LATER = clock(timedelta(seconds=10))  # a poll that clearly started after the mutations
EARLIER = clock(timedelta(seconds=-10))  # a poll whose request started before them
# The v1 snapshot: well before the mutations. Webhook timestamps are whole seconds, so a mutation in
# the same second as a real-clock poll can look older than that poll (see the test for it below).
V1 = clock(timedelta(seconds=-60))


def override(engine: AsyncEngine) -> Any:
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def test_session() -> AsyncIterator[AsyncSession]:
        async with sessions() as session:
            yield session

    return test_session


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
        self.cdms = httpx.AsyncClient(transport=self.cdms_transport, base_url="http://cdms")

    async def reset_cdms(self) -> None:
        async with self.engine.begin() as conn:
            await conn.execute(
                text("TRUNCATE inbox_event, job, product_changes, products, poll_run RESTART IDENTITY")
            )

    async def admin(self, method: str, path: str, **body: Any) -> Any:
        resp = await self.emulator.request(method, path, json=body or None)
        assert resp.status_code < 300, resp.text
        return resp.json()

    async def mutate(self, count: int, *, webhook: bool, seed: int | None = None) -> list[dict[str, Any]]:
        body = await self.admin(
            "POST", "/_admin/mutate", count=count, seed=seed, notify="webhook" if webhook else "none"
        )
        mutations: list[dict[str, Any]] = body["mutations"]
        return mutations

    async def poll(self, now: Callable[[], datetime] | None = None) -> PollResult:
        kwargs = {"now": now} if now else {}
        result = await run_poll(
            self.engine, trigger=Trigger.MANUAL, transport=self.emulator_transport, **kwargs
        )
        assert result is not None and result.status == "SUCCEEDED", result
        return result

    async def deliver(self) -> None:
        while await deliver_due(self.sessions, transport=self.cdms_transport):
            pass

    async def process(self) -> None:
        while await run_job_batch(self.engine, "w1"):
            pass

    async def send(self, event_id: str, ts: datetime, data: dict[str, Any]) -> int:
        """A signed event posted directly, e.g. Vietful re-sending a state under a new event id."""
        raw = json.dumps(
            {"id": event_id, "timestamp": int(ts.timestamp()), "event": "PRODUCT_UPSERTED", "items": [data]}
        ).encode()
        headers = {"content-type": "application/json", "x-vf-hmacsha256": sign(raw, SECRET)}
        resp = await self.cdms.post("/api/v1/webhooks/vietful", content=raw, headers=headers)
        assert resp.status_code in (200, 202), resp.text
        return resp.status_code

    async def history(self, sku: str) -> list[Any]:
        async with self.engine.connect() as conn:
            statement = (
                select(ProductChange).where(ProductChange.partner_sku == sku).order_by(ProductChange.version)
            )
            return list((await conn.execute(statement)).all())

    async def product(self, sku: str) -> Any:
        async with self.engine.connect() as conn:
            return (await conn.execute(select(Product).where(Product.partner_sku == sku))).one()

    async def outcomes(self) -> list[dict[str, int]]:
        async with self.engine.connect() as conn:
            events = (await conn.execute(select(InboxEvent).order_by(InboxEvent.id))).all()
        assert {e.status for e in events} <= {"PROCESSED"}
        return [e.outcome for e in events]


@pytest.fixture
async def world(db_engine: AsyncEngine) -> AsyncIterator[World]:
    w = World(db_engine)
    await w.reset_cdms()
    await w.admin("PUT", "/_admin/faults", mode="ok")
    await w.admin("PUT", "/_admin/callback", duplicateRate=0, maxRetries=5, retryDelayMs=0, concurrency=4)
    await w.admin("POST", "/_admin/seed", count=1, seed=42)  # one product: every mutation hits it
    resp = await w.emulator.post("/api/v1/WebhookSubscribers", json={"endpoint": ENDPOINT}, headers=AUTH)
    assert resp.status_code == 204
    await w.poll(V1)  # CDMS holds v1
    yield w
    await w.emulator.aclose()
    await w.cdms.aclose()


def fp(data: dict[str, Any]) -> bytes:
    return fingerprint(canonical_product(data))


def no_writes(outcome: dict[str, int]) -> bool:
    return outcome["created"] == outcome["updated"] == 0 and outcome["unchanged"] + outcome["stale"] == 1


async def test_poll_then_webhook_of_the_same_state_stores_it_once(world: World) -> None:
    [m] = await world.mutate(1, webhook=True)

    polled = await world.poll(LATER)
    await world.deliver()
    await world.process()

    assert polled.counts["updated"] == 1
    [outcome] = await world.outcomes()
    assert outcome["stale"] == 1  # the webhook's timestamp is older than the poll that already stored it
    history = await world.history(m["partnerSKU"])
    assert [(c.version, c.source) for c in history] == [(1, "POLLING"), (2, "POLLING")]
    assert (await world.product(m["partnerSKU"])).fingerprint == fp(m["data"])


async def test_webhook_then_poll_of_the_same_state_stores_it_once(world: World) -> None:
    [m] = await world.mutate(1, webhook=True)

    await world.deliver()
    await world.process()
    polled = await world.poll(LATER)

    [outcome] = await world.outcomes()
    assert outcome["updated"] == 1
    assert (polled.counts["updated"], polled.counts["unchanged"]) == (0, 1)
    history = await world.history(m["partnerSKU"])
    assert [(c.version, c.source) for c in history] == [(1, "POLLING"), (2, "WEBHOOK")]
    product = await world.product(m["partnerSKU"])
    assert product.fingerprint == fp(m["data"])
    assert product.version == 2
    assert product.observed_at > history[-1].observed_at  # the newer poll only advanced observed_at


async def test_poll_that_started_before_the_webhook_changes_nothing(world: World) -> None:
    [m] = await world.mutate(1, webhook=True)
    await world.deliver()
    await world.process()

    polled = await world.poll(EARLIER)

    assert (polled.counts["updated"], polled.counts["stale"]) == (0, 1)
    assert [c.version for c in await world.history(m["partnerSKU"])] == [1, 2]


async def test_late_webhooks_do_not_revert_a_newer_polled_state(world: World) -> None:
    older, newest = await world.mutate(1, webhook=True, seed=1) + await world.mutate(1, webhook=True, seed=2)
    assert fp(older["data"]) != fp(newest["data"])

    await world.poll(LATER)  # sees only the newest state
    await world.deliver()  # both callbacks arrive after the poll
    await world.process()

    assert all(no_writes(o) for o in await world.outcomes())
    history = await world.history(newest["partnerSKU"])
    assert [c.version for c in history] == [1, 2]
    assert history[-1].fingerprint == fp(newest["data"])
    assert (await world.product(newest["partnerSKU"])).fingerprint == fp(newest["data"])


async def test_resent_state_under_a_new_event_id_is_not_stored_again(world: World) -> None:
    older, newest = await world.mutate(1, webhook=False, seed=3) + await world.mutate(
        1, webhook=False, seed=4
    )
    await world.poll(LATER)
    sku = newest["partnerSKU"]

    # Same newest state, a new event id, a newer timestamp: a new event for the inbox, no change for CDMS.
    assert await world.send("resend-newest", datetime.now(UTC) + timedelta(seconds=20), newest["data"]) == 202
    # An older state under a new event id with its original timestamp: stale, never applied.
    assert await world.send("resend-older", datetime.fromisoformat(older["createdAt"]), older["data"]) == 202
    await world.process()

    assert [(o["unchanged"], o["stale"]) for o in await world.outcomes()] == [(1, 0), (0, 1)]
    history = await world.history(sku)
    assert [c.version for c in history] == [1, 2]
    assert (await world.product(sku)).fingerprint == fp(newest["data"])


async def test_webhook_older_than_the_last_poll_waits_for_the_next_poll(world: World) -> None:
    """Known limitation (README, ISSUES I-13): ordering trusts source timestamps, and webhook
    timestamps have whole-second precision. A change whose webhook timestamp is not newer than the
    state CDMS already observed is judged stale; the next poll stores it — late, but once and never
    reverted."""
    await world.poll(LATER)  # CDMS already "observed" the product 10 s in the future
    [m] = await world.mutate(1, webhook=True)
    await world.deliver()
    await world.process()

    [outcome] = await world.outcomes()
    assert outcome["stale"] == 1
    assert [c.version for c in await world.history(m["partnerSKU"])] == [1]

    polled = await world.poll(clock(timedelta(seconds=20)))
    assert polled.counts["updated"] == 1
    history = await world.history(m["partnerSKU"])
    assert [(c.version, c.source) for c in history] == [(1, "POLLING"), (2, "POLLING")]
    assert history[-1].fingerprint == fp(m["data"])


@pytest.mark.parametrize("round_", range(3))
async def test_concurrent_poll_and_webhook_processing_store_each_state_once(
    world: World, round_: int
) -> None:
    await world.admin("POST", "/_admin/seed", count=20, seed=round_)  # emulator partnerSKUs are sequential
    await world.reset_cdms()
    first = await world.poll(V1)  # v1 of the 20 new products
    assert first.counts["created"] == 20
    mutations = await world.mutate(20, webhook=True, seed=round_)
    await world.deliver()

    # The poller and the job runner apply the same 20 new states at the same time.
    await asyncio.gather(world.poll(), world.process())

    for m in mutations:
        history = await world.history(m["partnerSKU"])
        assert [c.version for c in history] == [1, 2], m["partnerSKU"]
        assert history[-1].fingerprint == fp(m["data"])
        assert (await world.product(m["partnerSKU"])).fingerprint == fp(m["data"])
    # Every state became exactly one change, whichever source won.
    assert sum(no_writes(o) for o in await world.outcomes()) + sum(
        o["updated"] for o in await world.outcomes()
    ) == len(mutations)
