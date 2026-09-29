import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import InterfaceError, OperationalError
from starlette.exceptions import HTTPException as StarletteHTTPException

log = logging.getLogger(__name__)


class AppError(Exception):
    """Expected error rendered as `{"error": {code, message, details}}` (docs/api.md conventions)."""

    def __init__(self, status: int, code: str, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details or {}


def _body(code: str, message: str, details: Any = None) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, "details": details or {}}}


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(_body(exc.code, exc.message, exc.details), status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            _body("INVALID_PAYLOAD", "Request validation failed", {"errors": exc.errors()}), status_code=422
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = "NOT_FOUND" if exc.status_code == 404 else f"HTTP_{exc.status_code}"
        return JSONResponse(_body(code, str(exc.detail)), status_code=exc.status_code)

    @app.exception_handler(OperationalError)
    @app.exception_handler(InterfaceError)
    async def _database_unavailable(_: Request, exc: Exception) -> JSONResponse:
        log.warning("database unavailable: %s", type(exc).__name__)
        return JSONResponse(_body("UNAVAILABLE", "Database unavailable"), status_code=503)

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception) -> JSONResponse:
        # Runs outside RequestIdMiddleware (in Starlette's ServerErrorMiddleware): pass the id explicitly.
        request_id = getattr(request.state, "request_id", None)
        log.exception("unhandled error", exc_info=exc, extra={"request_id": request_id})
        headers = {"x-request-id": request_id} if request_id else None
        return JSONResponse(
            _body("INTERNAL_ERROR", "Internal server error"), status_code=500, headers=headers
        )
