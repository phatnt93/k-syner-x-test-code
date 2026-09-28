"""Current state of each product, shaped after Vietful `ProductDto` (GET /api/v1/Products).

Sample response: docs/project-info/products_res.txt. JSON field → column:

    productId → product_id        sku → sku                 partnerSKU → partner_sku
    productName → product_name    assetType → asset_type    hasSerial → has_serial
    hasExpiration → has_expiration color → color            size → size
    description → description     isActive → is_active      units → units (JSONB)
    categories → categories (JSONB)

`partner_sku` is the business key (decision D1 in docs/assumptions.md). The CDMS columns (`fingerprint`,
`version`, `observed_at`) support change detection: a new observation is a change only when its fingerprint
differs from the stored one.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, CheckConstraint, DateTime, Identity, LargeBinary, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from cdms.db.base import Base


class Product(Base):
    __tablename__ = "products"
    __table_args__ = (CheckConstraint("version >= 1", name="version_positive"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)

    # --- Vietful ProductDto fields ---
    product_id: Mapped[int | None] = mapped_column(BigInteger, index=True)  # Vietful internal id
    sku: Mapped[str | None] = mapped_column(Text)
    partner_sku: Mapped[str] = mapped_column(Text, unique=True)
    product_name: Mapped[str | None] = mapped_column(Text)
    asset_type: Mapped[str | None] = mapped_column(Text)
    has_serial: Mapped[bool | None]
    has_expiration: Mapped[bool | None]
    color: Mapped[str | None] = mapped_column(Text)
    size: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool | None]
    # ["PCS", "BOX"]
    units: Mapped[list[str]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    # [{"categoryCode": "C01", "categoryName": "Phones"}]
    categories: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))

    # --- CDMS change-detection metadata ---
    fingerprint: Mapped[bytes] = mapped_column(LargeBinary)  # SHA-256 of the canonical business fields
    version: Mapped[int] = mapped_column(server_default=text("1"))  # +1 on every detected change
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))  # when the source saw this state
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    def __repr__(self) -> str:
        return f"Product(partner_sku={self.partner_sku!r}, version={self.version})"
