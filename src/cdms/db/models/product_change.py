"""The Change Database: append-only history of every real product change (docs/solution.md, section 14).

One row per detected change; rows are never updated or deleted. The constraints are the exactly-once
safety net: even with a bug in the pipeline, PostgreSQL rejects a second row for the same version and a
"change" whose fingerprint equals the previous one.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
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


class ProductChange(Base):
    __tablename__ = "product_changes"
    __table_args__ = (
        # One logical change per (product, version): two racing writers cannot both store version n.
        UniqueConstraint("partner_sku", "version"),
        CheckConstraint("version >= 1", name="version_positive"),
        CheckConstraint("change_type IN ('CREATED', 'UPDATED')", name="change_type"),
        CheckConstraint("source IN ('POLLING', 'WEBHOOK', 'EXCEL')", name="source"),
        # A "change" that changes nothing can never be stored.
        CheckConstraint("prev_fingerprint IS DISTINCT FROM fingerprint", name="fingerprint_changed"),
        # Only the first version has no previous state, and only it is CREATED.
        CheckConstraint("(version = 1) = (prev_fingerprint IS NULL)", name="first_version_has_no_prev"),
        CheckConstraint("(version = 1) = (change_type = 'CREATED')", name="created_is_first_version"),
        Index(None, "recorded_at"),
        Index(None, "source", "recorded_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)  # global order

    # Matching key is partner_sku only (decision D1); the FK guarantees no history without a product row.
    partner_sku: Mapped[str] = mapped_column(Text, ForeignKey("products.partner_sku", ondelete="RESTRICT"))
    version: Mapped[int]
    change_type: Mapped[str] = mapped_column(Text)  # CREATED | UPDATED

    prev_fingerprint: Mapped[bytes | None] = mapped_column(LargeBinary)  # NULL for CREATED
    fingerprint: Mapped[bytes] = mapped_column(LargeBinary)

    # Full new state (canonical ProductDto fields, camelCase as received).
    data: Mapped[dict[str, Any]] = mapped_column(JSONB)
    # {field: [old, new]} for UPDATED; {} for CREATED.
    diff: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))

    source: Mapped[str] = mapped_column(Text)  # POLLING | WEBHOOK | EXCEL
    # What produced it: poll run id + page, webhook event id, or upload id + row number.
    source_ref: Mapped[str] = mapped_column(Text)

    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))  # when the source saw this state
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    def __repr__(self) -> str:
        return f"ProductChange(partner_sku={self.partner_sku!r}, version={self.version}, {self.change_type})"
