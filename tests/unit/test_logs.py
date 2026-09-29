"""Structured logs and correlation ids (task C-14)."""

import json
import logging
import sys
from collections.abc import Iterator

import httpx
import pytest

from cdms.api.main import create_app
from cdms.logs import ContextFilter, JsonFormatter, TextFormatter, bind, context


class Capture(logging.Handler):
    """Records as the real handler sees them: after the context filter."""

    def __init__(self) -> None:
        super().__init__()
        self.addFilter(ContextFilter())
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@pytest.fixture
def captured() -> Iterator[Capture]:
    handler = Capture()
    root = logging.getLogger()
    root.addHandler(handler)
    yield handler
    root.removeHandler(handler)


def record(msg: str = "hello %s", *args: object, **extra: object) -> logging.LogRecord:
    rec = logging.LogRecord("cdms.test", logging.INFO, __file__, 1, msg, args or ("world",), None)
    rec.__dict__.update(extra)
    ContextFilter().filter(rec)
    return rec


def test_json_line_has_the_standard_keys_and_the_fields() -> None:
    with bind(request_id="r-1"):
        line = JsonFormatter().format(record(event_id="evt-1", inbox_id=7))

    entry = json.loads(line)
    assert entry["level"] == "INFO"
    assert entry["logger"] == "cdms.test"
    assert entry["msg"] == "hello world"
    assert entry["ts"].endswith("+00:00")
    assert (entry["request_id"], entry["event_id"], entry["inbox_id"]) == ("r-1", "evt-1", 7)


def test_json_line_carries_the_traceback() -> None:
    try:
        raise ValueError("boom")
    except ValueError:
        rec = logging.LogRecord("cdms.test", logging.ERROR, __file__, 1, "failed", (), sys.exc_info())
    entry = json.loads(JsonFormatter().format(rec))
    assert "ValueError: boom" in entry["exc"]


def test_text_line_appends_the_fields() -> None:
    with bind(run_id=3):
        line = TextFormatter().format(record(pages=2))
    assert line.endswith("INFO cdms.test: hello world [pages=2 run_id=3]")


def test_bind_nests_and_restores() -> None:
    with bind(worker_id="w1"):
        with bind(job_id=5):
            assert context() == {"worker_id": "w1", "job_id": 5}
        assert context() == {"worker_id": "w1"}
    assert context() == {}


def test_extra_wins_over_the_bound_context() -> None:
    with bind(request_id="outer"):
        assert record(request_id="inner").request_id == "inner"  # type: ignore[attr-defined]


# --- request id --------------------------------------------------------------------------------------------


async def get(path: str, headers: dict[str, str] | None = None, app: object = None) -> httpx.Response:
    transport = httpx.ASGITransport(app=app or create_app(), raise_app_exceptions=False)  # type: ignore[arg-type]
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path, headers=headers)


async def test_response_gets_a_generated_request_id() -> None:
    resp = await get("/health")
    assert len(resp.headers["x-request-id"]) == 32


async def test_callers_request_id_is_kept_and_an_unsafe_one_replaced() -> None:
    assert (await get("/health", {"x-request-id": "lb-42.a:b"})).headers["x-request-id"] == "lb-42.a:b"
    replaced = (await get("/health", {"x-request-id": "bad id\twith spaces"})).headers["x-request-id"]
    assert replaced != "bad id\twith spaces" and len(replaced) == 32


async def test_log_lines_of_a_request_carry_its_id(captured: Capture) -> None:
    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/webhooks/vietful",
            content=b'{"id":"evt-9","timestamp":1,"event":"X"}',
            headers={"x-request-id": "req-1", "x-vf-hmacsha256": "wrong"},
        )
    assert resp.status_code == 401
    [rejected] = [r for r in captured.records if r.getMessage() == "webhook rejected: INVALID_SIGNATURE"]
    assert rejected.request_id == "req-1"  # type: ignore[attr-defined]


async def test_unhandled_error_logs_and_returns_the_request_id(captured: Capture) -> None:
    app = create_app()

    async def boom() -> None:
        raise RuntimeError("boom")

    app.add_api_route("/boom", boom)
    resp = await get("/boom", {"x-request-id": "req-500"}, app=app)

    assert resp.status_code == 500
    assert resp.headers["x-request-id"] == "req-500"
    [error] = [r for r in captured.records if r.getMessage() == "unhandled error"]
    assert error.request_id == "req-500"  # type: ignore[attr-defined]
