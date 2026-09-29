"""`POST /api/v1/webhooks/vietful` — the Callback Client's endpoint (docs/webhook-solution.md)."""

import logging
from typing import Annotated, Literal

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.exc import InterfaceError, OperationalError

from cdms.api.deps import SessionDep
from cdms.api.errors import AppError
from cdms.api.paging import Cursor, Limit, int_cursor, next_cursor
from cdms.config import get_settings
from cdms.db.models import InboxEvent
from cdms.faults import crash
from cdms.ingestion import webhook
from cdms.schemas.api import InboxEventDetail, InboxEventOut, Page

router = APIRouter(prefix="/api/v1/webhooks", tags=["webhooks"])
log = logging.getLogger(__name__)


@router.post(
    "/vietful",
    status_code=202,
    responses={
        200: {"description": "Duplicate delivery of an event already stored"},
        401: {"description": "INVALID_SIGNATURE"},
        409: {"description": "EVENT_ID_CONFLICT"},
        422: {"description": "INVALID_PAYLOAD"},
        503: {"description": "UNAVAILABLE — nothing stored, retry later"},
    },
)
async def receive(request: Request, session: SessionDep) -> JSONResponse:
    """Verify, store durably, answer. Processing happens in the worker; the answer only means "stored"."""
    secret = get_settings().webhook_secret
    if secret is None:
        raise AppError(503, "UNAVAILABLE", "Webhook receiver is not configured (WEBHOOK_SECRET)")
    body = await request.body()
    event_id: str | None = None
    try:
        webhook.verify_signature(
            body, request.headers.get(webhook.SIGNATURE_HEADER), secret.get_secret_value()
        )
        envelope, payload = webhook.parse(body)
        event_id = envelope.id
        async with session.begin():
            accepted = await webhook.accept(session, envelope, payload)
        if get_settings().fault_crash_after_inbox_commit and not accepted.duplicate:
            crash("FAULT_CRASH_AFTER_INBOX_COMMIT: event stored, dying before the response")
    except webhook.WebhookRejected as exc:
        log.warning("webhook rejected: %s", exc.code, extra={"event_id": event_id, "status": exc.status})
        raise AppError(exc.status, exc.code, exc.message) from exc
    except (OperationalError, InterfaceError) as exc:  # database unreachable: the sender must retry
        log.warning("webhook not stored: database unavailable", extra={"event_id": event_id, "status": 503})
        raise AppError(503, "UNAVAILABLE", "Database unavailable") from exc

    fields = {"event_id": envelope.id, "event_type": envelope.event, "inbox_id": accepted.inbox_id}
    log.info("webhook %s", "duplicate" if accepted.duplicate else "accepted", extra=fields)
    if accepted.duplicate:
        return JSONResponse({"status": "duplicate", "inboxId": accepted.inbox_id}, status_code=200)
    return JSONResponse({"status": "accepted", "inboxId": accepted.inbox_id}, status_code=202)


@router.get("/events", response_model=Page[InboxEventOut], response_model_by_alias=True)
async def list_events(
    session: SessionDep,
    limit: Limit = 50,
    cursor: Cursor = None,
    status: Annotated[Literal["PENDING", "PROCESSED", "IGNORED", "DEAD"] | None, Query()] = None,
) -> Page[InboxEventOut]:
    """The webhook inbox, newest first: what arrived, whether it was processed, and its outcome."""
    query = select(InboxEvent).order_by(InboxEvent.id.desc()).limit(limit + 1)
    if cursor:
        query = query.where(InboxEvent.id < int_cursor(cursor))
    if status:
        query = query.where(InboxEvent.status == status)
    rows = list((await session.execute(query)).scalars())
    page, nxt = next_cursor(rows, limit, lambda e: e.id)
    return Page[InboxEventOut](items=[_event(e, InboxEventOut) for e in page], next_cursor=nxt)


@router.get("/events/{inbox_id}", response_model=InboxEventDetail, response_model_by_alias=True)
async def get_event(session: SessionDep, inbox_id: int) -> InboxEventDetail:
    event = await session.get(InboxEvent, inbox_id)
    if event is None:
        raise AppError(404, "EVENT_NOT_FOUND", f"No inbox event {inbox_id}")
    return _event(event, InboxEventDetail)


def _event[E: InboxEventOut](event: InboxEvent, model: type[E]) -> E:
    items = event.payload.get("items")
    return model.model_validate(
        {
            "id": event.id,
            "event_id": event.event_id,
            "event_type": event.event_type,
            "status": event.status,
            "items": len(items) if isinstance(items, list) else 0,
            "outcome": event.outcome,
            "error": event.error,
            "event_ts": event.event_ts,
            "received_at": event.received_at,
            "processed_at": event.processed_at,
            "payload": event.payload,
        }
    )
