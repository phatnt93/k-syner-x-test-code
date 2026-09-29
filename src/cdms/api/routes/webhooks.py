"""`POST /api/v1/webhooks/vietful` — the Callback Client's endpoint (docs/webhook-solution.md)."""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import InterfaceError, OperationalError

from cdms.api.deps import SessionDep
from cdms.api.errors import AppError
from cdms.config import get_settings
from cdms.ingestion import webhook

router = APIRouter(prefix="/api/v1/webhooks", tags=["webhooks"])


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
    try:
        webhook.verify_signature(
            body, request.headers.get(webhook.SIGNATURE_HEADER), secret.get_secret_value()
        )
        envelope, payload = webhook.parse(body)
        async with session.begin():
            accepted = await webhook.accept(session, envelope, payload)
    except webhook.WebhookRejected as exc:
        raise AppError(exc.status, exc.code, exc.message) from exc
    except (OperationalError, InterfaceError) as exc:  # database unreachable: the sender must retry
        raise AppError(503, "UNAVAILABLE", "Database unavailable") from exc

    if accepted.duplicate:
        return JSONResponse({"status": "duplicate", "inboxId": accepted.inbox_id}, status_code=200)
    return JSONResponse({"status": "accepted", "inboxId": accepted.inbox_id}, status_code=202)
