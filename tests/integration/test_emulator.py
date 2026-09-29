"""Vietful emulator (C-03a) against the test database: Vietful-shaped Products API, auth, admin, faults.

Tests re-seed the `vietful` schema as they need; CDMS tables are never touched.
"""

import time
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from cdms.core.canonical import canonical_product
from cdms.db.session import get_session
from cdms.emulator.main import create_app
from cdms.emulator.models import VietfulSettings

AUTH = {"Authorization": "Bearer test-emulator-token"}
PRODUCT_DTO_FIELDS = {
    "productId", "sku", "partnerSKU", "productName", "assetType", "hasSerial", "hasExpiration", "color",
    "size", "description", "isActive", "units", "categories",
}  # fmt: skip


@pytest.fixture
async def client(db_engine: AsyncEngine) -> AsyncIterator[httpx.AsyncClient]:
    sessions = async_sessionmaker(db_engine, expire_on_commit=False)

    async def test_session() -> AsyncIterator[AsyncSession]:
        async with sessions() as session:
            yield session

    app = create_app()
    app.dependency_overrides[get_session] = test_session
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://emulator") as c:
        await c.put("/_admin/faults", json={"mode": "ok"})
        yield c
        await c.put("/_admin/faults", json={"mode": "ok"})


async def seed(client: httpx.AsyncClient, count: int, seed: int = 42) -> None:
    resp = await client.post("/_admin/seed", json={"count": count, "seed": seed})
    assert resp.status_code == 200, resp.text


async def products(client: httpx.AsyncClient, **params: Any) -> list[dict[str, Any]]:
    resp = await client.get("/api/v1/Products", params=params, headers=AUTH)
    assert resp.status_code == 200, resp.text
    body: list[dict[str, Any]] = resp.json()
    return body


# --- Vietful-compatible Products API ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "headers", [{}, {"Authorization": "Bearer wrong"}, {"Authorization": "Basic test-emulator-token"}]
)
async def test_vietful_routes_need_the_bearer_token(
    client: httpx.AsyncClient, headers: dict[str, str]
) -> None:
    resp = await client.get("/api/v1/Products", headers=headers)
    assert resp.status_code == 401
    assert resp.json() == {"code": "UNAUTHORIZED", "errorMessage": "Missing or invalid bearer token"}
    assert resp.headers["www-authenticate"] == "Bearer"


async def test_products_are_vietful_product_dtos(client: httpx.AsyncClient) -> None:
    await seed(client, 3)
    items = await products(client)

    assert [p["productId"] for p in items] == [1, 2, 3]
    assert [p["partnerSKU"] for p in items] == ["P-00001", "P-00002", "P-00003"]
    for item in items:
        assert set(item) == PRODUCT_DTO_FIELDS
        assert item["units"] and item["categories"]
        canonical_product(item)  # CDMS's normalizer accepts every emulator product


async def test_seed_is_deterministic(client: httpx.AsyncClient) -> None:
    await seed(client, 20, seed=7)
    first = await products(client, PageSize=20)
    await seed(client, 20, seed=7)
    assert await products(client, PageSize=20) == first
    await seed(client, 20, seed=8)
    assert await products(client, PageSize=20) != first


async def test_paging_is_ordered_by_product_id(client: httpx.AsyncClient) -> None:
    await seed(client, 25)

    assert len(await products(client)) == 10  # Vietful default PageSize
    pages = [await products(client, PageIndex=i, PageSize=10) for i in range(4)]

    assert [len(page) for page in pages] == [10, 10, 5, 0]
    assert [p["productId"] for page in pages for p in page] == list(range(1, 26))


async def test_filters(client: httpx.AsyncClient) -> None:
    await seed(client, 30)
    all_items = await products(client, PageSize=30)
    target = all_items[4]

    by_partner = await products(client, PartnerSKUs=f"P-00002, {target['partnerSKU']},P-99999")
    assert [p["partnerSKU"] for p in by_partner] == ["P-00002", target["partnerSKU"]]
    assert [p["sku"] for p in await products(client, SKUs="SKU-00003")] == ["SKU-00003"]

    word = target["productName"].split()[-2].lower()  # the Faker word, searched case-insensitively
    by_keyword = await products(client, Keyword=word, PageSize=30)
    assert target in by_keyword
    assert all(word in p["productName"].lower() or word in p["sku"].lower() for p in by_keyword)
    assert await products(client, Keyword="%") == []  # LIKE wildcards are literal


async def test_get_product_by_partner_sku(client: httpx.AsyncClient) -> None:
    await seed(client, 2)
    resp = await client.get("/api/v1/Products/P-00002", headers=AUTH)
    assert resp.status_code == 200
    assert resp.json() == (await products(client))[1]

    missing = await client.get("/api/v1/Products/P-99999", headers=AUTH)
    assert missing.status_code == 400
    assert missing.json()["code"] == "PRODUCT_NOT_FOUND"


@pytest.mark.parametrize(
    "params", [{"PageSize": 0}, {"PageSize": 1001}, {"PageIndex": -1}, {"PageSize": "x"}]
)
async def test_invalid_query_is_a_vietful_400(client: httpx.AsyncClient, params: dict[str, Any]) -> None:
    resp = await client.get("/api/v1/Products", params=params, headers=AUTH)
    assert resp.status_code == 400
    assert set(resp.json()) == {"code", "errorMessage"}
    assert resp.json()["code"] == "INVALID_REQUEST"


async def test_webhook_subscriber_is_stored(client: httpx.AsyncClient, db_engine: AsyncEngine) -> None:
    endpoint = "http://localhost:8100/api/v1/webhooks/vietful"
    resp = await client.post("/api/v1/WebhookSubscribers", json={"endpoint": endpoint}, headers=AUTH)
    assert resp.status_code == 204
    async with db_engine.connect() as conn:
        stored = (await conn.execute(select(VietfulSettings.webhook_endpoint))).scalar_one()
    assert stored == endpoint


# --- admin --------------------------------------------------------------------------------------------------


async def test_seed_logs_one_created_mutation_per_product(client: httpx.AsyncClient) -> None:
    await seed(client, 5)
    log = (await client.get("/_admin/mutations")).json()

    assert [m["kind"] for m in log["items"]] == ["CREATED"] * 5
    assert [m["data"] for m in log["items"]] == await products(client)
    assert log["nextCursor"] is None


async def test_mutate_changes_real_values_and_logs_them(client: httpx.AsyncClient) -> None:
    await seed(client, 10)
    before = {p["partnerSKU"]: p for p in await products(client, PageSize=10)}

    resp = await client.post("/_admin/mutate", json={"count": 4, "seed": 1})

    assert resp.status_code == 200
    mutations = resp.json()["mutations"]
    assert len(mutations) == 4
    after = {p["partnerSKU"]: p for p in await products(client, PageSize=10)}
    for m in mutations:
        sku = m["partnerSKU"]
        assert m["kind"] == "UPDATED" and 1 <= len(m["changedFields"]) <= 2
        assert m["data"] == after[sku]
        changed = {f for f in PRODUCT_DTO_FIELDS if before[sku][f] != after[sku][f]}
        assert set(m["changedFields"]) >= changed - {"units", "categories"}  # lists may only be reordered
        # Every mutation is a real change for CDMS, never a reorder only.
        assert canonical_product(before[sku]) != canonical_product(after[sku])
    untouched = set(before) - {m["partnerSKU"] for m in mutations}
    assert all(before[sku] == after[sku] for sku in untouched)


async def test_mutate_respects_fields_and_never_changes_the_key(client: httpx.AsyncClient) -> None:
    await seed(client, 5)
    mutations = (await client.post("/_admin/mutate", json={"count": 50, "fields": ["isActive"]})).json()[
        "mutations"
    ]

    assert len(mutations) == 5  # capped at the catalogue size
    assert all(m["changedFields"] == ["isActive"] for m in mutations)
    assert sorted(p["partnerSKU"] for p in await products(client)) == [f"P-0000{i}" for i in range(1, 6)]


async def test_create_products(client: httpx.AsyncClient) -> None:
    await seed(client, 3)
    item = {"partnerSKU": "X-1", "productName": "  Kẽm ", "units": ["PCS"]}

    resp = await client.post("/_admin/products", json={"count": 2, "items": [item]})

    assert resp.status_code == 201
    created = resp.json()["mutations"]
    assert [m["partnerSKU"] for m in created] == ["P-00004", "P-00005", "X-1"]
    assert created[2]["data"]["sku"] == "X-1"  # Vietful: sku defaults to partnerSKU
    duplicate = await client.post("/_admin/products", json={"items": [item]})
    assert duplicate.status_code == 409


async def test_mutation_log_paging(client: httpx.AsyncClient) -> None:
    await seed(client, 7)
    first = (await client.get("/_admin/mutations", params={"limit": 5})).json()
    second = (
        await client.get("/_admin/mutations", params={"limit": 5, "cursor": first["nextCursor"]})
    ).json()

    assert len(first["items"]) == 5 and first["nextCursor"] == str(first["items"][-1]["id"])
    assert len(second["items"]) == 2 and second["nextCursor"] is None


# --- faults -------------------------------------------------------------------------------------------------


async def test_down_fails_vietful_routes_but_not_admin(client: httpx.AsyncClient) -> None:
    await seed(client, 1)
    assert (await client.put("/_admin/faults", json={"mode": "down"})).status_code == 200

    resp = await client.get("/api/v1/Products", headers=AUTH)
    assert resp.status_code == 503
    assert resp.json()["code"] == "SERVICE_UNAVAILABLE"
    assert (await client.get("/api/v1/Products", headers={})).status_code == 503  # down before auth
    assert (await client.get("/_admin/faults")).json() == {"mode": "down", "latencyMs": 0, "errorRate": 0.0}

    await client.put("/_admin/faults", json={"mode": "ok"})
    assert (await client.get("/api/v1/Products", headers=AUTH)).status_code == 200


async def test_slow_and_flaky(client: httpx.AsyncClient) -> None:
    await seed(client, 1)
    await client.put("/_admin/faults", json={"mode": "slow", "latencyMs": 200})
    started = time.perf_counter()
    assert (await client.get("/api/v1/Products", headers=AUTH)).status_code == 200
    assert time.perf_counter() - started >= 0.2

    await client.put("/_admin/faults", json={"mode": "flaky", "errorRate": 1.0})
    assert (await client.get("/api/v1/Products", headers=AUTH)).status_code == 500
    await client.put("/_admin/faults", json={"mode": "flaky", "errorRate": 0.0})
    assert (await client.get("/api/v1/Products", headers=AUTH)).status_code == 200
