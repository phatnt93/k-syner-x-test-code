"""Emulator tables, all in the PostgreSQL schema `vietful` (the emulator's own data, not CDMS state)."""

from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, Float, Identity, Integer, Text, func, text
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
        {"schema": SCHEMA},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    fault_mode: Mapped[str] = mapped_column(Text, server_default=text("'ok'"))
    fault_latency_ms: Mapped[int] = mapped_column(server_default=text("0"))
    fault_error_rate: Mapped[float] = mapped_column(Float, server_default=text("0"))
    # Set by POST /api/v1/WebhookSubscribers; callbacks are sent here (C-03b).
    webhook_endpoint: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
