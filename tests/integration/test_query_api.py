"""Query / config API (C-07) over data written by the real pipeline, inbox and poll code."""

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from cdms.api.main import create_app
from cdms.core.pipeline import ProductObservation, Source, apply_observations
from cdms.db.session import get_session
from cdms.ingestion.webhook import sign

T0 = datetime(2026, 9, 29, 8, 0, tzinfo=UTC)


def override(engine: AsyncEngine) -> Any:
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def test_session() -> AsyncIterator[AsyncSession]:
        async with sessions() as session:
            yield session

    return test_session


def obs(sku: str, at: datetime, source: Source = Source.POLLING, **fields: Any) -> ProductObservation:
    raw = {"partnerSKU": sku, "sku": f"SKU-{sku}", "productName": f"Kẽm {sku}", "isActive": True} | fields
    return ProductObservation.from_raw(raw, observed_at=at, source=source, source_ref=f"test:{sku}")


@pytest.fixture
async def api(db_engine: AsyncEngine) -> AsyncIterator[httpx.AsyncClient]:
    async with db_engine.begin() as conn:
        await conn.execute(
            text("TRUNCATE inbox_event, job, product_changes, products, poll_run RESTART IDENTITY")
        )
        await conn.execute(
            text(
                "UPDATE sync_config SET poll_enabled = true, poll_interval_seconds = 30, poll_page_size = 100"
            )
        )
    sessions = async_sessionmaker(db_engine)
    async with sessions() as session, session.begin():
        await apply_observations(session, [obs(f"P-{i}", T0, isActive=i % 2 == 0) for i in range(1, 6)])
    async with sessions() as session, session.begin():  # P-1 changes twice more, P-2 once via webhook
        await apply_observations(session, [obs("P-1", T0 + timedelta(minutes=1), color="Red")])
    async with sessions() as session, session.begin():
        await apply_observations(
            session,
            [
                obs("P-1", T0 + timedelta(minutes=2), color="Blue"),
                obs("P-2", T0 + timedelta(minutes=2), Source.WEBHOOK, isActive=True, size="L"),
            ],
        )
    app = create_app()
    app.dependency_overrides[get_session] = override(db_engine)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://cdms") as client:
        yield client


async def get(client: httpx.AsyncClient, path: str, **params: Any) -> Any:
    resp = await client.get(path, params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_products_are_listed_with_vietful_field_names(api: httpx.AsyncClient) -> None:
    body = await get(api, "/api/v1/products")
    assert [p["partnerSKU"] for p in body["items"]] == ["P-1", "P-2", "P-3", "P-4", "P-5"]
    assert body["nextCursor"] is None
    p1 = body["items"][0]
    assert (p1["version"], p1["color"], p1["productName"], p1["isActive"]) == (3, "Blue", "Kẽm P-1", True)
    assert len(p1["fingerprint"]) == 64  # hex SHA-256
    assert {"productId", "observedAt", "updatedAt"} <= set(p1)


async def test_product_paging_and_filters(api: httpx.AsyncClient) -> None:
    first = await get(api, "/api/v1/products", limit=2)
    second = await get(api, "/api/v1/products", limit=2, cursor=first["nextCursor"])
    last = await get(api, "/api/v1/products", limit=2, cursor=second["nextCursor"])
    assert [p["partnerSKU"] for p in first["items"] + second["items"] + last["items"]] == [
        "P-1", "P-2", "P-3", "P-4", "P-5",
    ]  # fmt: skip
    assert first["nextCursor"] == "P-2" and last["nextCursor"] is None

    assert [p["partnerSKU"] for p in (await get(api, "/api/v1/products", q="p-3"))["items"]] == ["P-3"]
    active = await get(api, "/api/v1/products", isActive="true")
    # P-1 (later states) and P-2 (webhook) became active; P-3 / P-5 were created inactive
    assert [p["partnerSKU"] for p in active["items"]] == ["P-1", "P-2", "P-4"]


async def test_product_detail_and_history(api: httpx.AsyncClient) -> None:
    assert (await get(api, "/api/v1/products/P-1"))["version"] == 3

    history = await get(api, "/api/v1/products/P-1/changes")
    assert [(c["version"], c["changeType"]) for c in history["items"]] == [
        (1, "CREATED"), (2, "UPDATED"), (3, "UPDATED"),
    ]  # fmt: skip
    assert history["items"][2]["diff"] == {"color": ["Red", "Blue"]}
    assert history["items"][2]["prevFingerprint"] == history["items"][1]["fingerprint"]
    assert history["items"][0]["prevFingerprint"] is None

    missing = await api.get("/api/v1/products/NOPE")
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "PRODUCT_NOT_FOUND"
    assert (await api.get("/api/v1/products/NOPE/changes")).status_code == 404


async def test_change_feed(api: httpx.AsyncClient) -> None:
    feed = await get(api, "/api/v1/changes")
    assert [c["id"] for c in feed["items"]] == [1, 2, 3, 4, 5, 6, 7, 8]
    newest = await get(api, "/api/v1/changes", order="desc", limit=3)
    assert [c["id"] for c in newest["items"]] == [8, 7, 6] and newest["nextCursor"] == "6"
    older = await get(api, "/api/v1/changes", order="desc", limit=3, cursor="6")
    assert [c["id"] for c in older["items"]] == [5, 4, 3]

    webhook = await get(api, "/api/v1/changes", source="WEBHOOK")
    assert [(c["partnerSKU"], c["version"]) for c in webhook["items"]] == [("P-2", 2)]
    assert [c["version"] for c in (await get(api, "/api/v1/changes", partnerSKU="P-1"))["items"]] == [1, 2, 3]
    future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    assert (await get(api, "/api/v1/changes", since=future))["items"] == []
    assert (await api.get("/api/v1/changes", params={"cursor": "abc"})).status_code == 422


async def test_config_is_read_and_partially_updated(api: httpx.AsyncClient) -> None:
    config = await get(api, "/api/v1/config")
    assert (config["pollIntervalSeconds"], config["pollPageSize"], config["pollEnabled"]) == (30, 100, True)

    resp = await api.put("/api/v1/config", json={"pollIntervalSeconds": 60, "pollEnabled": False})
    assert resp.status_code == 200
    assert (resp.json()["pollIntervalSeconds"], resp.json()["pollEnabled"], resp.json()["pollPageSize"]) == (
        60, False, 100,
    )  # fmt: skip
    assert (await get(api, "/api/v1/config"))["pollIntervalSeconds"] == 60


@pytest.mark.parametrize(
    "body",
    [{"pollIntervalSeconds": 4}, {"pollPageSize": 501}, {"httpMaxAttempts": 0}, {"pollEnabled": "maybe"}],
)
async def test_config_rejects_out_of_range_values(api: httpx.AsyncClient, body: dict[str, Any]) -> None:
    resp = await api.put("/api/v1/config", json=body)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "INVALID_PAYLOAD"


async def test_webhook_events_and_stats(api: httpx.AsyncClient, db_engine: AsyncEngine) -> None:
    for i, event in enumerate(["PRODUCT_UPSERTED", "INV_CHANGED", "PRODUCT_UPSERTED"]):
        body = {"id": f"evt-{i}", "timestamp": 1_790_000_000, "event": event, "items": [{"partnerSKU": "X"}]}
        raw = json.dumps(body).encode()
        await api.post(
            "/api/v1/webhooks/vietful",
            content=raw,
            headers={"x-vf-hmacsha256": sign(raw, "test-webhook-secret")},
        )
    async with db_engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO poll_run (trigger, status, pages) VALUES ('SCHEDULE', 'SUCCEEDED', 3)")
        )

    events = await get(api, "/api/v1/webhooks/events")
    assert [(e["eventId"], e["status"], e["items"]) for e in events["items"]] == [
        ("evt-2", "PENDING", 1), ("evt-1", "IGNORED", 1), ("evt-0", "PENDING", 1),
    ]  # fmt: skip
    assert "payload" not in events["items"][0]
    ignored = await get(api, "/api/v1/webhooks/events", status="IGNORED")
    assert [e["eventId"] for e in ignored["items"]] == ["evt-1"]
    detail = await get(api, f"/api/v1/webhooks/events/{events['items'][0]['id']}")
    assert detail["payload"]["id"] == "evt-2"
    assert (await api.get("/api/v1/webhooks/events/999")).status_code == 404

    runs = await get(api, "/api/v1/polling/runs")
    assert [(r["trigger"], r["status"], r["pages"]) for r in runs["items"]] == [("SCHEDULE", "SUCCEEDED", 3)]

    stats = await get(api, "/api/v1/stats")
    assert stats["products"] == 5
    assert stats["changesBySource"] == {"POLLING": 7, "WEBHOOK": 1}
    assert stats["changesByType"] == {"CREATED": 5, "UPDATED": 3}
    assert stats["inboxByStatus"] == {"PENDING": 2, "IGNORED": 1}
    assert stats["jobsByStatus"] == {"PENDING": 2} and stats["jobBacklog"] == 2
    assert stats["oldestPendingJobAgeSeconds"] >= 0
    assert stats["lastPollRun"]["pages"] == 3


async def test_database_down_is_503_on_every_endpoint(api: httpx.AsyncClient) -> None:
    dead = create_async_engine(
        "postgresql+psycopg://nobody:nothing@127.0.0.1:1/none", connect_args={"connect_timeout": 1}
    )
    app = create_app()
    app.dependency_overrides[get_session] = override(dead)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://cdms") as client:
        resp = await client.get("/api/v1/stats")
    await dead.dispose()
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "UNAVAILABLE"
