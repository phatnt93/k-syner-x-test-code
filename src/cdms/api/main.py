from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from cdms.api.errors import install_error_handlers
from cdms.api.request_id import RequestIdMiddleware
from cdms.api.routes import health, ops, polling, products, ui, webhooks
from cdms.config import get_settings
from cdms.db.session import engine
from cdms.logs import setup_logging


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    yield
    await engine.dispose()


def create_app() -> FastAPI:
    settings = get_settings()
    setup_logging(settings.log_level, settings.log_format)
    app = FastAPI(title="CDMS", version="0.1.0", lifespan=lifespan)
    app.add_middleware(RequestIdMiddleware)
    install_error_handlers(app)
    app.include_router(health.router)
    app.include_router(webhooks.router)
    app.include_router(polling.router)
    app.include_router(products.router)
    app.include_router(ops.router)
    app.include_router(ui.router)
    return app


app = create_app()
