import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from cdms.api.errors import install_error_handlers
from cdms.api.routes import health, polling, webhooks
from cdms.config import get_settings
from cdms.db.session import engine


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    yield
    await engine.dispose()


def create_app() -> FastAPI:
    logging.basicConfig(level=get_settings().log_level)
    app = FastAPI(title="CDMS", version="0.1.0", lifespan=lifespan)
    install_error_handlers(app)
    app.include_router(health.router)
    app.include_router(webhooks.router)
    app.include_router(polling.router)
    return app


app = create_app()
