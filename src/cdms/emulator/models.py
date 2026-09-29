"""Emulator tables, all in the PostgreSQL schema `vietful` (the emulator's own data, not CDMS state)."""

from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, Float, Identity, Index, Integer, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from cdms.db.base import Base

SCHEMA = "vietful"


class VietfulProduct(Base):
    """What Vietful's Products API serves: one row per product, Vietful `ProductDto` fields as snake_case."""

    __tablename__ = "products"
    __table_args__ = ({"schema": SCHEMA},)

    # Vietful's "product identity number": int32, list endpoint is ordered by it.
    product_id: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    sku: Mapped[str] = mapped_column(Text)
    partner_sku: Mapped[str] = mapped_column(Text, unique=True)
    product_name: Mapped[str | None] = mapped_column(Text)
    asset_type: Mapped[str | None] = mapped_column(Text)
    has_serial: Mapped[bool | None]
    has_expiration: Mapped[bool | None]
    color: Mapped[str | None] = mapped_column(Text)
    size: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool | None]
    units: Mapped[list[str]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    categories: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class VietfulMutation(Base):
    """Append-only log of every product the emulator created or changed: the ground truth that CDMS's change
    history is checked against (docs/database.md, invariant 5)."""

    __tablename__ = "mutations"
    __table_args__ = (
        CheckConstraint("kind IN ('CREATED', 'UPDATED')", name="kind"),
        {"schema": SCHEMA},
    )

    id: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    kind: Mapped[str] = mapped_column(Text)
    product_id: Mapped[int]
    partner_sku: Mapped[str] = mapped_column(Text, index=True)
    changed_fields: Mapped[list[str]] = mapped_column(JSONB)  # camelCase ProductDto names; [] for CREATED
    data: Mapped[dict[str, Any]] = mapped_column(JSONB)  # full ProductDto after the mutation
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class VietfulSettings(Base):
    """Single row (id = 1) of runtime switches, shared by every emulator process."""

    __tablename__ = "settings"
    __table_args__ = (
        CheckConstraint("id = 1", name="single_row"),
        CheckConstraint("fault_mode IN ('ok', 'down', 'slow', 'flaky')", name="fault_mode"),
        CheckConstraint("fault_error_rate >= 0 AND fault_error_rate <= 1", name="fault_error_rate"),
        CheckConstraint(
            "callback_duplicate_rate >= 0 AND callback_duplicate_rate <= 1", name="duplicate_rate"
        ),
        CheckConstraint("callback_max_retries BETWEEN 0 AND 100", name="max_retries"),
        CheckConstraint("callback_retry_delay_ms BETWEEN 0 AND 3600000", name="retry_delay_ms"),
        CheckConstraint("callback_concurrency BETWEEN 1 AND 100", name="concurrency"),
        {"schema": SCHEMA},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    fault_mode: Mapped[str] = mapped_column(Text, server_default=text("'ok'"))
    fault_latency_ms: Mapped[int] = mapped_column(server_default=text("0"))
    fault_error_rate: Mapped[float] = mapped_column(Float, server_default=text("0"))
    # Set by POST /api/v1/WebhookSubscribers; callbacks are sent here.
    webhook_endpoint: Mapped[str | None] = mapped_column(Text)
    # How callbacks are (re)delivered (PUT /_admin/callback). Vietful retries a non-2xx after 15 minutes; the
    # emulator retries after `callback_retry_delay_ms` (extension E3) so tests and demos do not wait.
    callback_duplicate_rate: Mapped[float] = mapped_column(Float, server_default=text("0"))
    callback_max_retries: Mapped[int] = mapped_column(server_default=text("5"))
    callback_retry_delay_ms: Mapped[int] = mapped_column(server_default=text("2000"))
    callback_concurrency: Mapped[int] = mapped_column(server_default=text("4"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class VietfulCallback(Base):
    """Outbox of webhook events.

    Written in the same transaction as the mutation it announces, then delivered by the sender loop
    (`cdms.emulator.callbacks`): a crash of the emulator never loses an event, like a real sender that stores
    before it sends.
    """

    __tablename__ = "callbacks"
    __table_args__ = (
        CheckConstraint("status IN ('PENDING', 'DELIVERED', 'FAILED')", name="status"),
        Index(None, "next_attempt_at", postgresql_where=text("status = 'PENDING'")),
        {"schema": SCHEMA},
    )

    id: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    event_id: Mapped[str] = mapped_column(Text, unique=True)  # envelope `id`, kept on every retry / duplicate
    event_type: Mapped[str] = mapped_column(Text)
    mutation_id: Mapped[int | None] = mapped_column(Integer)
    body: Mapped[str] = mapped_column(Text)  # exact JSON sent (and signed) on every delivery
    status: Mapped[str] = mapped_column(Text, server_default=text("'PENDING'"))
    attempts: Mapped[int] = mapped_column(server_default=text("0"))  # deliveries that got no 2xx
    deliveries: Mapped[int] = mapped_column(server_default=text("0"))  # every POST sent, duplicates included
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_status: Mapped[int | None] = mapped_column(Integer)  # HTTP status of the last delivery
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
