"""`GET /ui` — the static test console (docs/api.md).

The page calls the CDMS API (same origin) and the emulator admin API directly from the browser (CORS on the
emulator), so the D12 boundary holds: CDMS code never talks to the emulator for the page.
"""

import json
from functools import cache
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, RedirectResponse

from cdms.config import get_settings

router = APIRouter(tags=["ui"], include_in_schema=False)

_PAGE = Path(__file__).resolve().parents[2] / "ui" / "static" / "index.html"


@cache
def _template() -> str:
    return _PAGE.read_text(encoding="utf-8")


@router.get("/ui")
async def console() -> HTMLResponse:
    settings = get_settings()
    emulator_url = settings.ui_emulator_url or settings.inventory_base_url
    # JSON-encoded so the value is a safe JS string literal.
    html = (
        _template()
        .replace('"__EMULATOR_URL__"', json.dumps(emulator_url.rstrip("/")))
        .replace('"__WEBHOOK_ENDPOINT__"', json.dumps(settings.ui_webhook_endpoint))
    )
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


@router.get("/")
async def root() -> RedirectResponse:
    return RedirectResponse("/ui")
