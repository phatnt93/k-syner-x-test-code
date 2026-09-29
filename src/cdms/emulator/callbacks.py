"""The emulator as Vietful's Callback Client: webhook events for its product mutations.

`enqueue` writes events to the `vietful.callbacks` outbox in the mutation's transaction. `deliver_due` (run in
a loop by the emulator process) POSTs due events to the subscribed endpoint, signed like Vietful
(`x-vf-hmacsha256` = Base64(HMAC-SHA256(body, WEBHOOK_SECRET))), and retries every non-2xx.
Test knobs (`PUT /_admin/callback`): duplicate rate (a delivered event is sent once more), retry delay,
max retries, concurrency. With concurrency > 1 events may arrive out of order — on purpose, CDMS must cope.

The signing is implemented here again, not imported from CDMS, so the two sides check each other (D12).
"""

import asyncio
import base64
import contextlib
import hashlib
import hmac
import json
import logging
import random
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta

import httpx
from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cdms.config import get_settings
from cdms.emulator.models import VietfulCallback, VietfulMutation, VietfulSettings

log = logging.getLogger(__name__)

PRODUCT_UPSERTED = "PRODUCT_UPSERTED"  # extension E2: Vietful documents no product event
CLAIM_LEASE = timedelta(seconds=30)  # a sender that dies mid-delivery frees its events after this
BATCH = 100
HTTP_TIMEOUT_S = 10.0
IDLE_S = 0.2


class NoSubscriber(Exception):
    """notify=webhook but no endpoint was registered with POST /api/v1/WebhookSubscribers."""


@dataclass(frozen=True)
class SenderConfig:
    endpoint: str | None
    duplicate_rate: float
    max_retries: int
    retry_delay: timedelta
    concurrency: int


@dataclass(frozen=True)
class Delivery:
    id: int
    body: str
    attempts: int


@dataclass(frozen=True)
class Result:
    delivered: bool
    sent: int  # POSTs made, duplicates included
    status: int | None
    error: str | None


def sign(body: bytes, secret: str) -> str:
    return base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()


async def enqueue(session: AsyncSession, mutations: Sequence[VietfulMutation]) -> None:
    """One `PRODUCT_UPSERTED` event per mutation, with the full product state (caller commits)."""
    endpoint = (await session.execute(select(VietfulSettings.webhook_endpoint))).scalar_one_or_none()
    if not endpoint:
        raise NoSubscriber("no webhook subscriber: POST /api/v1/WebhookSubscribers first")
    rows = []
    for mutation in mutations:
        event_id = str(uuid.uuid4())
        body = {
            "id": event_id,
            "timestamp": int(mutation.created_at.timestamp()),  # Unix seconds, like Vietful
            "event": PRODUCT_UPSERTED,
            "items": [mutation.data],
        }
        rows.append(
            {
                "event_id": event_id,
                "event_type": PRODUCT_UPSERTED,
                "mutation_id": mutation.id,
                "body": json.dumps(body, ensure_ascii=False),
            }
        )
    if rows:
        await session.execute(insert(VietfulCallback), rows)


async def deliver_due(
    sessions: async_sessionmaker[AsyncSession],
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    rng: random.Random | None = None,
) -> int:
    """Deliver one batch of due events; returns how many were attempted (0 = nothing due)."""
    rng = rng or random.Random()
    async with sessions() as session, session.begin():
        row = await session.get(VietfulSettings, 1)
        if row is None:
            return 0
        config = SenderConfig(
            row.webhook_endpoint,
            row.callback_duplicate_rate,
            row.callback_max_retries,
            timedelta(milliseconds=row.callback_retry_delay_ms),
            row.callback_concurrency,
        )
        due = (
            select(VietfulCallback.id)
            .where(VietfulCallback.status == "PENDING", VietfulCallback.next_attempt_at <= func.now())
            .order_by(VietfulCallback.id)
            .limit(BATCH)
            .with_for_update(skip_locked=True)
            .scalar_subquery()
        )
        claimed = [
            Delivery(row.id, row.body, row.attempts)
            for row in await session.execute(
                update(VietfulCallback)
                .where(VietfulCallback.id.in_(due))
                .values(next_attempt_at=func.now() + CLAIM_LEASE)
                .returning(VietfulCallback.id, VietfulCallback.body, VietfulCallback.attempts)
            )
        ]
    if not claimed:
        return 0
    claimed.sort(key=lambda d: d.id)

    secret = get_settings().webhook_secret
    endpoint = config.endpoint
    semaphore = asyncio.Semaphore(config.concurrency)
    async with httpx.AsyncClient(transport=transport, timeout=HTTP_TIMEOUT_S) as http:

        async def send(delivery: Delivery) -> Result:
            if secret is None or not endpoint:
                return Result(False, 0, None, "WEBHOOK_SECRET or subscriber endpoint not configured")
            body = delivery.body.encode()
            headers = {
                "content-type": "application/json",
                "x-vf-hmacsha256": sign(body, secret.get_secret_value()),
            }
            async with semaphore:
                try:
                    resp = await http.post(endpoint, content=body, headers=headers)
                except httpx.HTTPError as exc:
                    return Result(False, 1, None, f"{type(exc).__name__}: {exc}"[:500])
                if resp.status_code >= 300:
                    return Result(False, 1, resp.status_code, resp.text[:500])
                sent = 1
                if rng.random() < config.duplicate_rate:  # redeliver an already delivered event
                    with contextlib.suppress(httpx.HTTPError):
                        await http.post(endpoint, content=body, headers=headers)
                        sent += 1
                return Result(True, sent, resp.status_code, None)

        results = await asyncio.gather(*(send(delivery) for delivery in claimed))

    async with sessions() as session, session.begin():
        for delivery, result in zip(claimed, results, strict=True):
            values: dict[str, object] = {
                "deliveries": VietfulCallback.deliveries + result.sent,
                "last_status": result.status,
                "last_error": result.error,
            }
            if result.delivered:
                values |= {"status": "DELIVERED", "delivered_at": func.now()}
            else:
                attempts = delivery.attempts + 1
                gave_up = attempts > config.max_retries
                values |= {
                    "attempts": attempts,
                    "status": "FAILED" if gave_up else "PENDING",
                    "next_attempt_at": func.now() + config.retry_delay,
                }
                log.warning(
                    "callback %d not delivered (attempt %d): %s",
                    delivery.id,
                    attempts,
                    result.error,
                    extra={"callback_id": delivery.id, "attempt": attempts, "http_status": result.status},
                )
            await session.execute(
                update(VietfulCallback).where(VietfulCallback.id == delivery.id).values(**values)
            )
    return len(claimed)


async def run_sender(sessions: async_sessionmaker[AsyncSession], stop: asyncio.Event) -> None:
    """Background loop of the emulator process."""
    while not stop.is_set():
        try:
            busy = await deliver_due(sessions)
        except Exception:
            log.exception("callback sender failed; retrying")
            busy = 0
        if not busy:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=IDLE_S)
