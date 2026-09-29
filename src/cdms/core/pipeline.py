"""The single write path for `products` / `product_changes` (docs/solution.md 3-8, docs/exactly-once.md).

Polling, webhook and Excel adapters only build `ProductObservation`s and call `apply_observation(s)`. The
caller owns the transaction: run these inside `async with session.begin():` together with the inbox / job /
counter updates of the same unit of work, so everything commits atomically. On `IntegrityError` or a deadlock
the caller rolls back and retries the whole unit; a retry is idempotent.
"""

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import insert, select, text, update
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
            await _advance_observed_at(session, [obs])
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

    Same outcomes as calling `apply_observation` for each item in `partner_sku` order, but cheaper when most
    items did not change (a poll re-scan): docs/ISSUES.md I-01.

    1. Lock every existing row of the batch in one `SELECT … ORDER BY partner_sku FOR UPDATE` (sorted, so two
       concurrent batches cannot deadlock on these rows) and read version / fingerprint / observed_at.
    2. Decide STALE / UNCHANGED for those rows here; advance `observed_at` of the UNCHANGED-and-newer ones in
       one statement. The rows stay locked until commit, so the decisions cannot go stale.
    3. Everything else — new products, real changes, and every occurrence of a key that appears more than once
       in the batch (input order matters there) — goes through `apply_observation`, sorted by key; the sort is
       stable, so duplicates keep their input order.
    """
    results: list[ApplyResult | None] = [None] * len(observations)
    occurrences = Counter(obs.partner_sku for obs in observations)
    existing = {
        row.partner_sku: row
        for row in await session.execute(
            select(Product.partner_sku, Product.version, Product.fingerprint, Product.observed_at)
            .where(Product.partner_sku.in_(sorted(occurrences)))
            .order_by(Product.partner_sku)
            .with_for_update()
        )
    }

    advance: list[ProductObservation] = []
    slow: list[int] = []
    for index, obs in enumerate(observations):
        current = existing.get(obs.partner_sku)
        if current is None or occurrences[obs.partner_sku] > 1:
            slow.append(index)
        elif obs.observed_at < current.observed_at:
            results[index] = ApplyResult(obs.partner_sku, Outcome.STALE, current.version)
        elif fingerprint(obs.product) == current.fingerprint:
            results[index] = ApplyResult(obs.partner_sku, Outcome.UNCHANGED, current.version)
            if obs.observed_at > current.observed_at:
                advance.append(obs)
        else:
            slow.append(index)
    if advance:
        await _advance_observed_at(session, advance)

    for index in sorted(slow, key=lambda i: observations[i].partner_sku):
        results[index] = await apply_observation(session, observations[index])
    return [result for result in results if result is not None]


async def _advance_observed_at(session: AsyncSession, observations: Sequence[ProductObservation]) -> None:
    """UNCHANGED with a newer observation: remember that the state was still current at that later time.

    Otherwise an older, different state delivered late (e.g. a webhook retry) would be accepted as newer and
    revert the product (D4). Also fills `product_id` when it was unknown. Not a change: `updated_at` is kept.
    """
    # One statement for the whole batch (an executemany costs a round trip per row), with three array
    # parameters instead of a VALUES list: the SQL text never changes, so it is compiled once, not per page.
    await session.execute(
        _ADVANCE_OBSERVED_AT,
        {
            "skus": [obs.partner_sku for obs in observations],
            "observed": [obs.observed_at for obs in observations],
            "product_ids": [obs.product["productId"] for obs in observations],
        },
    )


# Raw SQL does not fire the ORM `onupdate` of `updated_at`, which is what we want: this is not a change.
_ADVANCE_OBSERVED_AT = text(
    """
    UPDATE products AS p
    SET observed_at = v.observed_at, product_id = COALESCE(v.product_id, p.product_id)
    FROM unnest(CAST(:skus AS text[]), CAST(:observed AS timestamptz[]), CAST(:product_ids AS bigint[]))
         AS v (partner_sku, observed_at, product_id)
    WHERE p.partner_sku = v.partner_sku
    """
)


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
