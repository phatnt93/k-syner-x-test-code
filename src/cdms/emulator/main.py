"""Vietful emulator app.

Run: `uvicorn cdms.emulator.main:app --port 8101` (+ `--loop cdms.loop:selector_loop` on Windows).
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from cdms.config import get_settings
from cdms.db.session import SessionLocal, engine
from cdms.emulator.callbacks import run_sender
from cdms.emulator.errors import install_error_handlers
from cdms.emulator.routes import admin, ops, vietful

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    if settings.inventory_api_token is None:
        log.warning("INVENTORY_API_TOKEN is not set: every /api/v1 call will fail with 500 NOT_CONFIGURED")
    if settings.webhook_secret is None:
        log.warning("WEBHOOK_SECRET is not set: webhook callbacks cannot be signed and stay undelivered")
    # The Callback Client: delivers queued webhook events while the process runs.
    stop = asyncio.Event()
    sender = asyncio.create_task(run_sender(SessionLocal, stop))
    yield
    stop.set()
    await sender
    await engine.dispose()


def create_app() -> FastAPI:
    logging.basicConfig(level=get_settings().log_level)
    app = FastAPI(title="Vietful Inventory Service emulator", version="0.1.0", lifespan=lifespan)
    install_error_handlers(app)
    # The CDMS /ui console (another origin) drives the admin API from the browser.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=get_settings().emulator_cors_origins,
        allow_methods=["GET", "POST", "PUT"],
        allow_headers=["content-type"],
    )
    app.include_router(ops)
    app.include_router(vietful)
    app.include_router(admin)
    return app


app = create_app()
