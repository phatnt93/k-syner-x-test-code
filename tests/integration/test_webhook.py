"""Webhook + job queue (C-05) against the test database: the CDMS API accepts, `run_job_batch` processes.

Every test starts with empty inbox / job / product tables.
"""

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from cdms.api.main import create_app
from cdms.config import get_settings
from cdms.db.models import InboxEvent, Job, PollRun, Product, ProductChange
from cdms.db.session import get_session
from cdms.emulator.main import create_app as create_emulator
from cdms.ingestion import webhook
from cdms.ingestion.webhook import sign
from cdms.jobs import queue
from cdms.jobs.runner import run_job_batch

SECRET = "test-webhook-secret"
TS = 1_790_000_000  # envelope timestamp (Unix seconds)


def product(sku: str, **fields: Any) -> dict[str, Any]:
    return {"partnerSKU": sku, "sku": sku, "productName": "Kẽm", "isActive": True, "units": ["PCS"]} | fields


def envelope(
    event_id: str, *items: dict[str, Any], ts: int = TS, event: str = "PRODUCT_UPSERTED"
) -> dict[str, Any]:
    body: dict[str, Any] = {"id": event_id, "timestamp": ts, "event": event}
    if items:
        body["items"] = list(items)
    return body


def session_override(engine: AsyncEngine) -> Any:
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def test_session() -> AsyncIterator[AsyncSession]:
        async with sessions() as session:
            yield session

    return test_session


@pytest.fixture
async def api(db_engine: AsyncEngine) -> AsyncIterator[httpx.AsyncClient]:
    async with db_engine.begin() as conn:
        await conn.execute(
            text("TRUNCATE inbox_event, job, product_changes, products, poll_run RESTART IDENTITY")
        )
    app = create_app()
    app.dependency_overrides[get_session] = session_override(db_engine)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://cdms") as client:
        yield client


async def post(
    client: httpx.AsyncClient, body: dict[str, Any] | bytes, signature: str | None = None
) -> httpx.Response:
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    headers = {"content-type": "application/json", "x-vf-hmacsha256": signature or sign(raw, SECRET)}
    return await client.post("/api/v1/webhooks/vietful", content=raw, headers=headers)


async def rows(engine: AsyncEngine, statement: Any) -> list[Any]:
    async with engine.connect() as conn:
        return list((await conn.execute(statement)).all())


async def drain(engine: AsyncEngine, worker: str = "w1", **kwargs: Any) -> int:
    total = 0
    while claimed := await run_job_batch(engine, worker, **kwargs):
        total += claimed
    return total


# --- accept -------------------------------------------------------------------------------------------------


async def test_event_is_stored_with_a_job_and_answered_202(
    api: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    resp = await post(api, envelope("evt-1", product("P-1"), product("P-2")))

    assert resp.status_code == 202
    assert resp.json() == {"status": "accepted", "inboxId": 1}
    [event] = await rows(db_engine, select(InboxEvent))
    assert (event.event_id, event.event_type, event.status) == ("evt-1", "PRODUCT_UPSERTED", "PENDING")
    assert event.event_ts == datetime.fromtimestamp(TS, UTC)
    [job] = await rows(db_engine, select(Job))
    assert (job.kind, job.ref_id, job.status, job.attempts, job.max_attempts) == (
        "webhook_event",
        "1",
        "PENDING",
        0,
        5,
    )
    assert await rows(db_engine, select(Product)) == []  # nothing processed in the request


async def test_redelivery_is_a_duplicate(api: httpx.AsyncClient, db_engine: AsyncEngine) -> None:
    body = envelope("evt-1", product("P-1"))
    await post(api, body)

    again = await post(api, body)
    # the same event re-serialized by the sender (other key order / spacing) is still the same event
    reserialized = await post(api, json.dumps(dict(reversed(list(body.items()))), indent=2).encode())

    assert again.status_code == reserialized.status_code == 200
    assert again.json() == reserialized.json() == {"status": "duplicate", "inboxId": 1}
    assert len(await rows(db_engine, select(Job))) == 1


async def test_same_id_with_other_data_is_a_conflict(api: httpx.AsyncClient, db_engine: AsyncEngine) -> None:
    await post(api, envelope("evt-1", product("P-1")))
    resp = await post(api, envelope("evt-1", product("P-1", productName="other")))

    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "EVENT_ID_CONFLICT"
    [event] = await rows(db_engine, select(InboxEvent))
    assert event.payload["items"][0]["productName"] == "Kẽm"  # never overwritten


@pytest.mark.parametrize("signature", ["", "bm90IGl0", sign(b"{}", SECRET)])
async def test_bad_signature_is_401_and_stores_nothing(
    api: httpx.AsyncClient, db_engine: AsyncEngine, signature: str
) -> None:
    raw = json.dumps(envelope("evt-1", product("P-1"))).encode()
    resp = await api.post(
        "/api/v1/webhooks/vietful", content=raw, headers={"x-vf-hmacsha256": signature} if signature else {}
    )
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "INVALID_SIGNATURE"
    assert await rows(db_engine, select(InboxEvent)) == []


async def test_invalid_envelope_is_422(api: httpx.AsyncClient, db_engine: AsyncEngine) -> None:
    resp = await post(api, envelope("evt-1"))  # PRODUCT_UPSERTED without items
    assert resp.status_code == 422
    assert await rows(db_engine, select(InboxEvent)) == []


async def test_other_events_are_stored_as_ignored(api: httpx.AsyncClient, db_engine: AsyncEngine) -> None:
    body = envelope("inv-1", event="INV_CHANGED") | {"warehouseCode": "W1", "items": [{"changeQty": 5}]}
    resp = await post(api, body)

    assert resp.status_code == 202
    [event] = await rows(db_engine, select(InboxEvent))
    assert event.status == "IGNORED"
    assert await rows(db_engine, select(Job)) == []


async def test_unconfigured_secret_is_503(api: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "webhook_secret", None)
    resp = await post(api, envelope("evt-1", product("P-1")))
    assert resp.status_code == 503


async def test_database_down_is_503(api: httpx.AsyncClient) -> None:
    dead = create_async_engine(
        "postgresql+psycopg://nobody:nothing@127.0.0.1:1/none", connect_args={"connect_timeout": 1}
    )
    app = create_app()
    app.dependency_overrides[get_session] = session_override(dead)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://cdms") as client:
        resp = await post(client, envelope("evt-1", product("P-1")))
    await dead.dispose()
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "UNAVAILABLE"


# --- process ------------------------------------------------------------------------------------------------


async def test_worker_applies_events_and_marks_them_processed(
    api: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    await post(api, envelope("evt-1", product("P-1"), product("P-2")))
    await post(api, envelope("evt-2", product("P-1", productName="new"), {"partnerSKU": " "}, ts=TS + 5))

    assert await drain(db_engine) == 2

    events = await rows(db_engine, select(InboxEvent).order_by(InboxEvent.id))
    assert [e.status for e in events] == ["PROCESSED", "PROCESSED"]
    assert events[0].outcome == {"created": 2, "updated": 0, "unchanged": 0, "stale": 0, "invalid": 0}
    assert events[1].outcome == {"created": 0, "updated": 1, "unchanged": 0, "stale": 0, "invalid": 1}
    assert {j.status for j in await rows(db_engine, select(Job))} == {"DONE"}
    changes = await rows(
        db_engine, select(ProductChange).order_by(ProductChange.partner_sku, ProductChange.version)
    )
    assert [(c.partner_sku, c.version, c.source, c.source_ref) for c in changes] == [
        ("P-1", 1, "WEBHOOK", "webhook:evt-1"),
        ("P-1", 2, "WEBHOOK", "webhook:evt-2"),  # both events in one batch, still applied in arrival order
        ("P-2", 1, "WEBHOOK", "webhook:evt-1"),
    ]
    assert changes[1].observed_at == datetime.fromtimestamp(TS + 5, UTC)


async def test_old_event_after_a_newer_one_is_stale(api: httpx.AsyncClient, db_engine: AsyncEngine) -> None:
    await post(api, envelope("evt-new", product("P-1", productName="B"), ts=TS + 60))
    await post(
        api, envelope("evt-old", product("P-1", productName="A"), ts=TS)
    )  # delayed retry of an old event
    await drain(db_engine)

    [state] = await rows(db_engine, select(Product))
    assert (state.product_name, state.version) == ("B", 1)
    old = (await rows(db_engine, select(InboxEvent).where(InboxEvent.event_id == "evt-old")))[0]
    assert old.outcome["stale"] == 1


async def test_same_state_under_another_event_id_is_unchanged(
    api: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    await post(api, envelope("evt-1", product("P-1")))
    await post(api, envelope("evt-2", product("P-1"), ts=TS + 1))
    await drain(db_engine)

    assert len(await rows(db_engine, select(ProductChange))) == 1
    second = (await rows(db_engine, select(InboxEvent).where(InboxEvent.event_id == "evt-2")))[0]
    assert second.outcome["unchanged"] == 1


async def test_failing_event_is_retried_then_dead_without_blocking_others(
    api: httpx.AsyncClient, db_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    for i in range(3):
        await post(api, envelope(f"evt-{i}", product(f"P-{i}")))
    real = webhook.process_events

    async def breaks_on_event_2(session: AsyncSession, inbox_ids: list[int]) -> dict[int, dict[str, int]]:
        if 2 in inbox_ids:
            raise RuntimeError("boom")
        return await real(session, inbox_ids)

    monkeypatch.setattr(webhook, "process_events", breaks_on_event_2)
    async with db_engine.begin() as conn:
        await conn.execute(update(Job).values(max_attempts=2))

    await run_job_batch(db_engine, "w1")  # batch fails → events retried one by one

    jobs = {j.ref_id: j for j in await rows(db_engine, select(Job))}
    assert (jobs["1"].status, jobs["3"].status) == ("DONE", "DONE")
    assert (jobs["2"].status, jobs["2"].attempts, jobs["2"].last_error) == (
        "PENDING",
        1,
        "RuntimeError: boom",
    )
    assert jobs["2"].run_after > datetime.now(UTC)  # backoff
    assert {p.partner_sku for p in await rows(db_engine, select(Product))} == {"P-0", "P-2"}

    async with db_engine.begin() as conn:  # skip the backoff wait
        await conn.execute(update(Job).values(run_after=func.now() - text("interval '1 second'")))
    await run_job_batch(db_engine, "w1")

    [dead_job] = await rows(db_engine, select(Job).where(Job.ref_id == "2"))
    [dead_event] = await rows(db_engine, select(InboxEvent).where(InboxEvent.id == 2))
    assert (dead_job.status, dead_job.attempts) == ("DEAD", 2)
    assert (dead_event.status, dead_event.error) == ("DEAD", "RuntimeError: boom")
    assert await run_job_batch(db_engine, "w1") == 0  # a DEAD job is never claimed again


async def test_crashed_worker_lease_expires_and_another_worker_finishes(
    api: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    await post(api, envelope("evt-1", product("P-1")))
    sessions = async_sessionmaker(db_engine)
    async with sessions() as session, session.begin():
        assert len(await queue.claim(session, "crashed-worker", 10)) == 1  # ...and the worker dies here

    assert await run_job_batch(db_engine, "w2") == 0  # still leased
    async with db_engine.begin() as conn:
        await conn.execute(update(Job).values(locked_until=func.now() - text("interval '1 second'")))
    assert await run_job_batch(db_engine, "w2") == 1

    [job] = await rows(db_engine, select(Job))
    assert (job.status, job.attempts) == ("DONE", 2)
    assert len(await rows(db_engine, select(ProductChange))) == 1


async def test_job_that_keeps_crashing_its_worker_ends_dead(
    api: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    await post(api, envelope("evt-1", product("P-1")))
    async with db_engine.begin() as conn:  # 5 earlier claims whose workers died without recording anything
        await conn.execute(update(Job).values(attempts=5, status="RUNNING", locked_until=func.now()))

    await run_job_batch(db_engine, "w1")

    [job] = await rows(db_engine, select(Job))
    assert job.status == "DEAD"
    assert await rows(db_engine, select(Product)) == []


async def test_concurrent_workers_process_each_event_once(
    api: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    for i in range(40):  # 20 products, each created then changed
        name = "v1" if i < 20 else "v2"
        await post(api, envelope(f"evt-{i}", product(f"P-{i % 20}", productName=name), ts=TS + i))

    counts = await asyncio.gather(*(drain(db_engine, f"w{n}", batch_size=5) for n in range(4)))

    assert sum(counts) == 40
    assert {j.status for j in await rows(db_engine, select(Job))} == {"DONE"}
    changes = await rows(db_engine, select(ProductChange))
    assert len(changes) == 40
    assert {(c.partner_sku, c.version) for c in changes} == {(f"P-{i}", v) for i in range(20) for v in (1, 2)}


# --- manual poll through the queue --------------------------------------------------------------------------


async def test_polling_run_endpoint_enqueues_a_manual_poll(
    api: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    emulator = create_emulator()
    emulator.dependency_overrides[get_session] = session_override(db_engine)
    transport = httpx.ASGITransport(app=emulator)
    async with httpx.AsyncClient(transport=transport, base_url="http://emulator") as admin:
        await admin.put("/_admin/faults", json={"mode": "ok"})
        await admin.post("/_admin/seed", json={"count": 3, "seed": 1})

    resp = await api.post("/api/v1/polling/run")
    assert resp.status_code == 202
    job_id = resp.json()["jobId"]

    assert await run_job_batch(db_engine, "w1", poll_transport=transport) == 1

    [job] = await rows(db_engine, select(Job).where(Job.id == job_id))
    [run] = await rows(db_engine, select(PollRun))
    assert (run.trigger, run.status, run.created) == ("MANUAL", "SUCCEEDED", 3)
    assert (job.kind, job.status, job.last_error) == ("poll_now", "DONE", f"poll_run {run.id}")


# --- crash hooks of the failure scenarios (docs/testing.md F1 / F2) -----------------------------------------


class SimulatedCrash(BaseException):
    """Stands in for os._exit: a BaseException skips `except Exception` handlers, like a dead process."""


def fake_crash(reason: str) -> None:
    raise SimulatedCrash(reason)


async def test_api_crash_after_inbox_commit_then_retry_is_a_duplicate(
    api: httpx.AsyncClient, db_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cdms.api.routes import webhooks as route

    body = envelope("evt-1", product("P-1"))
    monkeypatch.setattr(get_settings(), "fault_crash_after_inbox_commit", True)
    monkeypatch.setattr(route, "crash", fake_crash)
    with pytest.raises(SimulatedCrash):  # the sender gets no answer
        await post(api, body)
    assert len(await rows(db_engine, select(InboxEvent))) == 1  # ...but the event is durable

    monkeypatch.setattr(get_settings(), "fault_crash_after_inbox_commit", False)
    retry = await post(api, body)  # the sender retries after the restart
    assert retry.status_code == 200 and retry.json()["status"] == "duplicate"
    await drain(db_engine)
    assert len(await rows(db_engine, select(ProductChange))) == 1


async def test_worker_crash_inside_the_batch_commits_nothing(
    api: httpx.AsyncClient, db_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cdms.jobs import runner

    for i in range(3):
        await post(api, envelope(f"evt-{i}", product(f"P-{i}")))
    monkeypatch.setattr(get_settings(), "fault_crash_in_job_batch", True)
    monkeypatch.setattr(runner, "crash", fake_crash)
    with pytest.raises(SimulatedCrash):
        await run_job_batch(db_engine, "doomed-worker")

    assert {e.status for e in await rows(db_engine, select(InboxEvent))} == {"PENDING"}
    assert await rows(db_engine, select(ProductChange)) == []  # the batch transaction was never committed
    assert {(j.status, j.attempts) for j in await rows(db_engine, select(Job))} == {("RUNNING", 1)}  # leased

    monkeypatch.setattr(get_settings(), "fault_crash_in_job_batch", False)
    async with db_engine.begin() as conn:  # the lease of the dead worker runs out
        await conn.execute(update(Job).values(locked_until=func.now() - text("interval '1 second'")))
    assert await drain(db_engine, "w2") == 3
    assert {(j.status, j.attempts) for j in await rows(db_engine, select(Job))} == {("DONE", 2)}
    assert len(await rows(db_engine, select(ProductChange))) == 3
