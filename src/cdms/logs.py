"""Logging setup shared by cdms-api, cdms-worker and the emulator (task C-14).

`LOG_FORMAT=json` writes one JSON object per line (`ts`, `level`, `logger`, `msg`, correlation fields, `exc`);
`text` (the default) is the readable form for development. Correlation fields come from two places:

- `bind(**fields)` — context for everything logged inside a block, also across `await` (contextvars): the
  HTTP `request_id`, a poll `run_id`, the `worker_id` / `job_id` of the job runner;
- `log.info(..., extra={"event_id": ...})` — fields of a single line. Names must not clash with LogRecord
  attributes (`created`, `msg`, `name`, ... — logging raises KeyError): nest such values, e.g. `outcome`.

The webhook `event_id` (Vietful's envelope id) ties a delivery together: the API logs it with the request id
and inbox id when it accepts the event, the worker with the job id and outcome when it applies it.
"""

import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

_context: ContextVar[dict[str, Any]] = ContextVar("cdms_log_context", default={})  # noqa: B039  (never mutated)

# Attributes every LogRecord has; anything else on a record came from `extra=` or the context filter.
# `color_message` is uvicorn's ANSI-coloured copy of the message.
_STANDARD = set(logging.makeLogRecord({}).__dict__) | {"message", "asctime", "taskName", "color_message"}
_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


@contextmanager
def bind(**fields: Any) -> Iterator[None]:
    """Add fields to every log record emitted inside the block (nested blocks add to the outer ones)."""
    token = _context.set(_context.get() | fields)
    try:
        yield
    finally:
        _context.reset(token)


def context() -> dict[str, Any]:
    return dict(_context.get())


class ContextFilter(logging.Filter):
    """Copies the bound context onto the record, without overwriting fields passed with `extra=`."""

    def filter(self, record: logging.LogRecord) -> bool:
        for key, value in _context.get().items():
            if not hasattr(record, key):
                setattr(record, key, value)
        return True


def _fields(record: logging.LogRecord) -> dict[str, Any]:
    return {key: value for key, value in record.__dict__.items() if key not in _STANDARD}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        entry |= _fields(record)
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False, default=str)


class TextFormatter(logging.Formatter):
    def __init__(self) -> None:
        super().__init__(_FORMAT)

    def formatMessage(self, record: logging.LogRecord) -> str:
        line = super().formatMessage(record)
        if fields := _fields(record):
            line += " [" + " ".join(f"{key}={value}" for key, value in fields.items()) + "]"
        return line


def setup_logging(level: str, fmt: str) -> None:
    """Configure the root logger; uvicorn's loggers are routed through it so every line has one format."""
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter() if fmt == "json" else TextFormatter())
    handler.addFilter(ContextFilter())
    handler.set_name("cdms")
    root = logging.getLogger()
    # Idempotent (create_app runs once per app instance); other handlers, e.g. pytest's capture, are kept.
    root.handlers[:] = [h for h in root.handlers if h.get_name() != "cdms"] + [handler]
    root.setLevel(level)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True
