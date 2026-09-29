"""Webhook inbox: one row per distinct event received (docs/webhook-solution.md, decision D5).

`UNIQUE (source, event_id)` is the transport idempotency key: a redelivered event finds its row and is
answered `200 duplicate` instead of being processed again.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Identity,
    Index,
    LargeBinary,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from cdms.db.base import Base


class InboxEvent(Base):
    __tablename__ = "inbox_event"
    __table_args__ = (
        UniqueConstraint("source", "event_id"),
        CheckConstraint("status IN ('PENDING', 'PROCESSED', 'IGNORED', 'DEAD')", name="status"),
        Index(None, "status", "received_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    source: Mapped[str] = mapped_column(Text, server_default=text("'vietful'"))
    event_id: Mapped[str] = mapped_column(Text)  # envelope `id`
    event_type: Mapped[str] = mapped_column(Text)  # envelope `event`
    # SHA-256 of the canonical JSON of the body: same id + same content = duplicate, different content =
    # conflict.
    body_sha256: Mapped[bytes] = mapped_column(LargeBinary)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    event_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))  # envelope `timestamp` (Unix seconds)
    # PENDING: waiting for the worker · PROCESSED · IGNORED: event type CDMS does not process · DEAD: gave up.
    status: Mapped[str] = mapped_column(Text)
    # created / updated / unchanged / stale / invalid
    outcome: Mapped[dict[str, int] | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    def __repr__(self) -> str:
        return f"InboxEvent(id={self.id}, {self.event_type} {self.event_id!r}, {self.status})"
