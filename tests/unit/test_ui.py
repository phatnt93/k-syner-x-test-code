"""The /ui console page and the emulator's CORS for it (no database needed)."""

import httpx
import pytest

from cdms.api.main import create_app
from cdms.config import get_settings
from cdms.emulator.main import create_app as create_emulator


async def get_ui(path: str = "/ui") -> httpx.Response:
    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://cdms") as client:
        return await client.get(path)


async def test_console_is_served_with_the_emulator_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "ui_emulator_url", 'http://emu.example:8101/"x')
    resp = await get_ui()
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert "<title>CDMS console</title>" in resp.text
    assert 'const EMULATOR = "http://emu.example:8101/\\"x";' in resp.text  # JSON-escaped, not raw
    assert "__EMULATOR_URL__" not in resp.text


async def test_emulator_url_defaults_to_the_inventory_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "ui_emulator_url", None)
    monkeypatch.setattr(get_settings(), "inventory_base_url", "http://localhost:8101/")
    assert 'const EMULATOR = "http://localhost:8101";' in (await get_ui()).text


async def test_root_redirects_to_the_console() -> None:
    resp = await get_ui("/")
    assert resp.status_code == 307 and resp.headers["location"] == "/ui"


@pytest.mark.parametrize(
    ("origin", "allowed"), [("http://localhost:8100", True), ("http://evil.example", False)]
)
async def test_emulator_allows_only_the_console_origin(origin: str, allowed: bool) -> None:
    transport = httpx.ASGITransport(app=create_emulator())
    async with httpx.AsyncClient(transport=transport, base_url="http://emulator") as client:
        resp = await client.options(
            "/_admin/mutate",
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type",
            },
        )
    assert (resp.headers.get("access-control-allow-origin") == origin) is allowed
