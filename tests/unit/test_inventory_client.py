"""Timeouts / retries of the inventory client (requirements 9.3); fake transport, no real sleep."""

from collections.abc import Callable

import httpx
import pytest

from cdms.ingestion.inventory_client import ClientConfig, InventoryClient, InventoryError

CONFIG = ClientConfig(
    base_url="http://inventory", token="t0ken", connect_timeout_s=1, read_timeout_s=2, max_attempts=3
)


def client_for(
    responses: list[httpx.Response | Exception], seen: list[httpx.Request], delays: list[float]
) -> InventoryClient:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        outcome = responses.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)

    return InventoryClient(CONFIG, transport=httpx.MockTransport(handler), sleep=fake_sleep)


def ok(body: object) -> httpx.Response:
    return httpx.Response(200, json=body)


async def test_sends_vietful_query_and_bearer_token() -> None:
    seen: list[httpx.Request] = []
    async with client_for([ok([{"partnerSKU": "P-1"}])], seen, []) as client:
        assert await client.list_products(2, 50) == [{"partnerSKU": "P-1"}]
    [request] = seen
    assert request.url.path == "/api/v1/Products"
    assert dict(request.url.params) == {"PageIndex": "2", "PageSize": "50"}
    assert request.headers["authorization"] == "Bearer t0ken"


@pytest.mark.parametrize(
    "failure",
    [
        lambda: httpx.Response(503),
        lambda: httpx.Response(500),
        lambda: httpx.Response(429),
        lambda: httpx.ConnectError("refused"),
        lambda: httpx.ReadTimeout("slow"),
    ],
)
async def test_transient_failures_are_retried(failure: Callable[[], httpx.Response | Exception]) -> None:
    seen: list[httpx.Request] = []
    delays: list[float] = []
    async with client_for([failure(), failure(), ok([])], seen, delays) as client:
        assert await client.list_products(0, 10) == []
    assert len(seen) == 3
    assert len(delays) == 2
    assert 0.5 <= delays[0] <= 0.75 and 1.0 <= delays[1] <= 1.5  # exponential backoff + jitter


async def test_gives_up_after_max_attempts() -> None:
    seen: list[httpx.Request] = []
    delays: list[float] = []
    async with client_for([httpx.Response(503, text="down")] * 3, seen, delays) as client:
        with pytest.raises(InventoryError, match="after 3 attempts: HTTP 503: down"):
            await client.list_products(0, 10)
    assert len(seen) == 3 and len(delays) == 2


@pytest.mark.parametrize("status", [400, 401, 404])
async def test_client_errors_are_not_retried(status: int) -> None:
    seen: list[httpx.Request] = []
    async with client_for([httpx.Response(status)], seen, []) as client:
        with pytest.raises(InventoryError, match=f"HTTP {status}"):
            await client.list_products(0, 10)
    assert len(seen) == 1


@pytest.mark.parametrize(
    "response",
    [ok({"items": []}), ok([1, 2]), httpx.Response(200, text="<html>")],
    ids=["object", "ints", "html"],
)
async def test_unexpected_body_is_an_error(response: httpx.Response) -> None:
    async with client_for([response], [], []) as client:
        with pytest.raises(InventoryError):
            await client.list_products(0, 10)
