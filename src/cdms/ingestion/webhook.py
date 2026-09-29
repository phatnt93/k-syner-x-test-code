"""Webhook callback: accept durably in the API, process in the worker (docs/webhook-solution.md, decision D5).

Accept (API request, one small transaction): verify the HMAC on the raw body, validate the envelope, insert
the inbox row (`UNIQUE (source, event_id)` stops redeliveries) and its job. Process (worker): turn the items
of a batch of events into observations, apply them with one `apply_observations` call and mark events + jobs
done in the same transaction.
"""

import base64
import hashlib
import hmac
import json
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from cdms.core.canonical import InvalidProductError
from cdms.core.pipeline import ProductObservation, Source, apply_observations
from cdms.db.models import InboxEvent
from cdms.jobs.queue import JobKind, enqueue

SOURCE = "vietful"
SIGNATURE_HEADER = "x-vf-hmacsha256"
MAX_BODY_BYTES = 1_000_000
MAX_ITEMS = 500
PRODUCT_UPSERTED = "PRODUCT_UPSERTED"  # emulator extension E2: full ProductDto states
# Stored but not processed: INV_CHANGED needs the inventory entity (C-12, out of scope); other Vietful events
# (IR_*, OR_*) are not product data. Vietful sends every event to the one subscriber, so they must be
# accepted.
PROCESSED_EVENTS = frozenset({PRODUCT_UPSERTED})


class InboxStatus(StrEnum):
    PENDING = "PENDING"
    PROCESSED = "PROCESSED"
    IGNORED = "IGNORED"
    DEAD = "DEAD"


class WebhookRejected(Exception):
    """The request is refused before anything is stored."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


class Envelope(BaseModel):
    """Vietful webhook body: common fields + `items` of product events; extra fields stay in the payload."""

    model_config = ConfigDict(extra="allow")

    id: str = Field(min_length=1, max_length=200)
    timestamp: int = Field(gt=0)  # Unix seconds, UTC
    event: str = Field(min_length=1, max_length=100)
    items: list[dict[str, Any]] | None = None


@dataclass(frozen=True)
class Accepted:
    inbox_id: int
    duplicate: bool


def sign(body: bytes, secret: str) -> str:
    """`x-vf-hmacsha256` value: Base64(HMAC-SHA256(raw body, secret))."""
    return base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()


def verify_signature(body: bytes, signature: str | None, secret: str) -> None:
    if not signature or not hmac.compare_digest(signature.strip().encode(), sign(body, secret).encode()):
        raise WebhookRejected(401, "INVALID_SIGNATURE", f"Missing or invalid {SIGNATURE_HEADER} header")


def parse(body: bytes) -> tuple[Envelope, dict[str, Any]]:
    if len(body) > MAX_BODY_BYTES:
        raise WebhookRejected(413, "PAYLOAD_TOO_LARGE", f"Body larger than {MAX_BODY_BYTES} bytes")
    try:
        payload = json.loads(body)
        envelope = Envelope.model_validate(payload)
    except (ValueError, ValidationError) as exc:  # JSONDecodeError is a ValueError
        raise WebhookRejected(422, "INVALID_PAYLOAD", f"Invalid webhook envelope: {exc}") from exc
    if envelope.event == PRODUCT_UPSERTED and not (envelope.items and len(envelope.items) <= MAX_ITEMS):
        raise WebhookRejected(422, "INVALID_PAYLOAD", f"{PRODUCT_UPSERTED} needs 1-{MAX_ITEMS} items")
    return envelope, payload


def content_hash(payload: dict[str, Any]) -> bytes:
    """Hash of the canonical JSON, so a sender that re-serializes the same event (key order, spacing) is still
    a
    duplicate, not a conflict."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).digest()


async def accept(session: AsyncSession, envelope: Envelope, payload: dict[str, Any]) -> Accepted:
    """Store the event and its job (caller commits). `WebhookRejected(409)`: id reused for other data."""
    body_hash = content_hash(payload)
    status = InboxStatus.PENDING if envelope.event in PROCESSED_EVENTS else InboxStatus.IGNORED
    inbox_id = (
        await session.execute(
            pg_insert(InboxEvent)
            .values(
                source=SOURCE,
                event_id=envelope.id,
                event_type=envelope.event,
                body_sha256=body_hash,
                payload=payload,
                event_ts=datetime.fromtimestamp(envelope.timestamp, UTC),
                status=status,
            )
            .on_conflict_do_nothing(index_elements=[InboxEvent.source, InboxEvent.event_id])
            .returning(InboxEvent.id)
        )
    ).scalar_one_or_none()

    if inbox_id is None:  # seen before (the conflicting insert waited for the first one to commit)
        existing = (
            await session.execute(
                select(InboxEvent.id, InboxEvent.body_sha256).where(
                    InboxEvent.source == SOURCE, InboxEvent.event_id == envelope.id
                )
            )
        ).one()
        if existing.body_sha256 != body_hash:
            raise WebhookRejected(
                409, "EVENT_ID_CONFLICT", f"Event id {envelope.id!r} was received with other data"
            )
        return Accepted(existing.id, duplicate=True)

    if status == InboxStatus.PENDING:
        await enqueue(session, JobKind.WEBHOOK_EVENT, str(inbox_id))
    return Accepted(inbox_id, duplicate=False)


@dataclass(frozen=True)
class Processed:
    event_id: str
    outcome: dict[str, int]  # created / updated / unchanged / stale / invalid


async def process_events(session: AsyncSession, inbox_ids: list[int]) -> dict[int, Processed]:
    """Apply the items of these events (in the given order) and mark them PROCESSED; caller owns the
    transaction.

    All items of the batch go through one `apply_observations` call: one lock pass, the fast path for
    unchanged
    products and a single commit for many events (docs/project-info/ISSUES.md I-02).
    """
    events = {
        event.id: event
        for event in (await session.execute(select(InboxEvent).where(InboxEvent.id.in_(inbox_ids)))).scalars()
    }
    observations: list[ProductObservation] = []
    owners: list[int] = []
    outcomes: dict[int, Counter[str]] = {}
    for inbox_id in inbox_ids:
        event = events[inbox_id]
        counts = outcomes[inbox_id] = Counter()
        for item in event.payload.get("items") or []:
            try:
                observations.append(
                    ProductObservation.from_raw(
                        item,
                        observed_at=event.event_ts,  # when Vietful saw this state (D4)
                        source=Source.WEBHOOK,
                        source_ref=f"webhook:{event.event_id}",
                    )
                )
                owners.append(inbox_id)
            except InvalidProductError:
                counts["invalid"] += 1

    for owner, applied in zip(owners, await apply_observations(session, observations), strict=True):
        outcomes[owner][applied.outcome.lower()] += 1

    summary: dict[int, Processed] = {}
    for inbox_id, counts in outcomes.items():
        outcome = {key: counts[key] for key in ("created", "updated", "unchanged", "stale", "invalid")}
        await session.execute(
            update(InboxEvent)
            .where(InboxEvent.id == inbox_id)
            .values(status=InboxStatus.PROCESSED, outcome=outcome, error=None, processed_at=func.now())
        )
        summary[inbox_id] = Processed(events[inbox_id].event_id, outcome)
    return summary


async def mark_failed(session: AsyncSession, inbox_id: int, error: str, *, dead: bool) -> None:
    values: dict[str, Any] = {"error": error[:1000]}
    if dead:
        values |= {"status": InboxStatus.DEAD, "processed_at": func.now()}
    await session.execute(update(InboxEvent).where(InboxEvent.id == inbox_id).values(**values))
