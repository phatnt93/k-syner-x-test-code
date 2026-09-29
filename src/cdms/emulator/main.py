"""Vietful emulator app.

Run: `uvicorn cdms.emulator.main:app --port 8101` (+ `--loop cdms.loop:selector_loop` on Windows).
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from cdms.config import get_settings
from cdms.db.session import engine
from cdms.emulator.errors import install_error_handlers
from cdms.emulator.routes import admin, ops, vietful

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    if get_settings().inventory_api_token is None:
        log.warning("INVENTORY_API_TOKEN is not set: every /api/v1 call will fail with 500 NOT_CONFIGURED")
    yield
    await engine.dispose()


def create_app() -> FastAPI:
    logging.basicConfig(level=get_settings().log_level)
    app = FastAPI(title="Vietful Inventory Service emulator", version="0.1.0", lifespan=lifespan)
    install_error_handlers(app)
    app.include_router(ops)
    app.include_router(vietful)
    app.include_router(admin)
    return app


app = create_app()
