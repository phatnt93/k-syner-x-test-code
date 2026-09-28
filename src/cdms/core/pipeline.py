"""The single write path for `products` / `product_changes` (docs/solution.md 3-8, docs/exactly-once.md).

Polling, webhook and Excel adapters only build `ProductObservation`s and call `apply_observation(s)`. The
caller owns the transaction: run these inside `async with session.begin():` together with the inbox / job /
counter updates of the same unit of work, so everything commits atomically. On `IntegrityError` or a deadlock
the caller rolls back and retries the whole unit; a retry is idempotent.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from cdms.core.canonical import CanonicalProduct, canonical_product
from cdms.core.fingerprint import FINGERPRINT_FIELDS, fingerprint
from cdms.db.models import Product, ProductChange


class Source(StrEnum):
    POLLING = "POLLING"
    WEBHOOK = "WEBHOOK"
    EXCEL = "EXCEL"


class Outcome(StrEnum):
    CREATED = "CREATED"
    UPDATED = "UPDATED"
    UNCHANGED = "UNCHANGED"
    STALE = "STALE"


@dataclass(frozen=True)
class ProductObservation:
    """One full product state seen by a source at `observed_at` (decision D4)."""

    product: CanonicalProduct
    observed_at: datetime
    source: Source
    # What produced it: poll run id + page, webhook event id, or upload id + row number.
    source_ref: str

    def __post_init__(self) -> None:
        if self.observed_at.tzinfo is None:
            raise ValueError("observed_at must be timezone-aware")

    @classmethod
    def from_raw(
        cls, raw: Mapping[str, Any], *, observed_at: datetime, source: Source, source_ref: str
    ) -> "ProductObservation":
        """Normalize a ProductDto-like mapping; raises `InvalidProductError`."""
        return cls(canonical_product(raw), observed_at, source, source_ref)

    @property
    def partner_sku(self) -> str:
        sku: str = self.product["partnerSKU"]
        return sku


@dataclass(frozen=True)
class ApplyResult:
    partner_sku: str
    outcome: Outcome
    version: int  # the product's version after this observation


# Canonical field → products column.
_COLUMNS: dict[str, InstrumentedAttribute[Any]] = {
    "productId": Product.product_id,
    "sku": Product.sku,
    "partnerSKU": Product.partner_sku,
    "productName": Product.product_name,
    "assetType": Product.asset_type,
    "hasSerial": Product.has_serial,
    "hasExpiration": Product.has_expiration,
    "color": Product.color,
    "size": Product.size,
    "description": Product.description,
    "isActive": Product.is_active,
    "units": Product.units,
    "categories": Product.categories,
}


def _column_values(product: CanonicalProduct) -> dict[str, Any]:
    return {column.key: product[field] for field, column in _COLUMNS.items()}


async def apply_observation(session: AsyncSession, obs: ProductObservation) -> ApplyResult:
    """Insert-or-lock the product row, then decide CREATED / UPDATED / UNCHANGED / STALE."""
    sku = obs.partner_sku
    fp = fingerprint(obs.product)

    # 1. Insert-or-lock. A concurrent insert of the same key waits here until the other transaction ends.
    created = await session.execute(
        pg_insert(Product)
        .values(**_column_values(obs.product), fingerprint=fp, version=1, observed_at=obs.observed_at)
        .on_conflict_do_nothing(index_elements=[Product.partner_sku])
        .returning(Product.version)
    )
    if created.scalar_one_or_none() is not None:
        await _record_change(session, obs, version=1, prev_fingerprint=None, fp=fp, diff={})
        return ApplyResult(sku, Outcome.CREATED, 1)

    # 2. The row exists: lock it, so writers of the same product take turns and decide on the latest state.
    current = (
        await session.execute(
            select(
                Product.version,
                Product.fingerprint,
                Product.observed_at,
                *(column.label(field) for field, column in _COLUMNS.items()),
            )
            .where(Product.partner_sku == sku)
            .with_for_update()
        )
    ).one()

    # 3. Decide.
    if obs.observed_at < current.observed_at:
        return ApplyResult(sku, Outcome.STALE, current.version)

    if fp == current.fingerprint:
        if obs.observed_at > current.observed_at:
            # Remember that the state was still current at this later time: otherwise an older, different
            # state delivered late (e.g. a webhook retry) would be accepted as newer and revert the product.
            await session.execute(
                update(Product)
                .where(Product.partner_sku == sku)
                .values(
                    observed_at=obs.observed_at,
                    product_id=func.coalesce(obs.product["productId"], Product.product_id),
                    updated_at=Product.updated_at,  # not a change: keep the last-change time
                )
            )
        return ApplyResult(sku, Outcome.UNCHANGED, current.version)

    new_version = current.version + 1
    diff = {
        field: [current._mapping[field], obs.product[field]]
        for field in FINGERPRINT_FIELDS
        if current._mapping[field] != obs.product[field]
    }
    await session.execute(
        update(Product)
        .where(Product.partner_sku == sku)
        .values(
            **_column_values(obs.product), fingerprint=fp, version=new_version, observed_at=obs.observed_at
        )
    )
    await _record_change(
        session, obs, version=new_version, prev_fingerprint=current.fingerprint, fp=fp, diff=diff
    )
    return ApplyResult(sku, Outcome.UPDATED, new_version)


async def apply_observations(
    session: AsyncSession, observations: Sequence[ProductObservation]
) -> list[ApplyResult]:
    """Apply a batch (a poll page, an Excel chunk) in one transaction; results are in input order.

    Rows are locked in `partner_sku` order so two concurrent batches cannot deadlock. The sort is stable, so
    several observations of the same product (duplicate Excel rows) keep their input order.
    """
    results: list[ApplyResult | None] = [None] * len(observations)
    for index in sorted(range(len(observations)), key=lambda i: observations[i].partner_sku):
        results[index] = await apply_observation(session, observations[index])
    return [result for result in results if result is not None]


async def _record_change(
    session: AsyncSession,
    obs: ProductObservation,
    *,
    version: int,
    prev_fingerprint: bytes | None,
    fp: bytes,
    diff: dict[str, Any],
) -> None:
    await session.execute(
        insert(ProductChange).values(
            partner_sku=obs.partner_sku,
            version=version,
            change_type=Outcome.CREATED if version == 1 else Outcome.UPDATED,
            prev_fingerprint=prev_fingerprint,
            fingerprint=fp,
            data=obs.product,
            diff=diff,
            source=obs.source,
            source_ref=obs.source_ref,
            observed_at=obs.observed_at,
        )
    )
