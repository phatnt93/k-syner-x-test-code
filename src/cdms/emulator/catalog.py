"""Emulator data operations: seed, create, mutate, read.

Every create / change is logged in `vietful.mutations` in the same transaction, so the log is exactly
what CDMS should end up with."""

from collections.abc import Sequence
from typing import Any

from sqlalchemy import func, insert, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from cdms.emulator.data import MUTABLE_FIELDS, ProductFaker
from cdms.emulator.models import VietfulCallback, VietfulMutation, VietfulProduct, VietfulSettings
from cdms.emulator.schemas import CallbackSettings, Faults, ProductInput, product_dto

_CHUNK = 1_000  # rows per INSERT when seeding


async def seed(session: AsyncSession, count: int, seed_value: int) -> None:
    """Replace the whole catalogue with `count` Faker products; product ids restart at 1."""
    await session.execute(
        text("TRUNCATE vietful.products, vietful.mutations, vietful.callbacks RESTART IDENTITY")
    )
    faker = ProductFaker(seed_value)
    rows = [faker.product(number) for number in range(1, count + 1)]
    for start in range(0, len(rows), _CHUNK):
        await _insert(session, rows[start : start + _CHUNK])


async def create(
    session: AsyncSession, count: int, items: Sequence[ProductInput], seed_value: int | None
) -> list[VietfulMutation]:
    """Add Faker products (numbered after the current max id) and explicit products."""
    faker = ProductFaker(seed_value)
    next_number = (
        await session.execute(select(func.coalesce(func.max(VietfulProduct.product_id), 0)))
    ).scalar_one()
    rows = [faker.product(next_number + offset) for offset in range(1, count + 1)]
    rows += [_input_columns(item) for item in items]
    return await _insert(session, rows) if rows else []


async def mutate(
    session: AsyncSession, count: int, fields: Sequence[str] | None, seed_value: int | None
) -> list[VietfulMutation]:
    """Change 1-2 fields of `count` random products; every change really differs from the old value."""
    faker = ProductFaker(seed_value)
    ids = (
        await session.execute(select(VietfulProduct.product_id).order_by(VietfulProduct.product_id))
    ).scalars()
    all_ids = list(ids)
    chosen = sorted(faker.rng.sample(all_ids, k=min(count, len(all_ids))))
    products = (
        await session.execute(
            select(VietfulProduct)
            .where(VietfulProduct.product_id.in_(chosen))
            .order_by(VietfulProduct.product_id)
            .with_for_update()
        )
    ).scalars()

    allowed = list(fields or MUTABLE_FIELDS)
    column_to_field = {column: field for field, column in MUTABLE_FIELDS.items()}
    mutations: list[dict[str, Any]] = []
    for product in products:
        current = {column: getattr(product, column) for column in MUTABLE_FIELDS.values()}
        changes = faker.mutate(current, allowed)
        for column, value in changes.items():
            setattr(product, column, value)  # the locked row; flushed as one UPDATE
        product.updated_at = func.now()
        mutations.append(
            {
                "kind": "UPDATED",
                "product_id": product.product_id,
                "partner_sku": product.partner_sku,
                "changed_fields": sorted(column_to_field[column] for column in changes),
                "data": product_dto(product),
            }
        )
    return await _log(session, mutations)


async def mutations_page(session: AsyncSession, after_id: int, limit: int) -> list[VietfulMutation]:
    result = await session.execute(
        select(VietfulMutation).where(VietfulMutation.id > after_id).order_by(VietfulMutation.id).limit(limit)
    )
    return list(result.scalars())


async def get_faults(session: AsyncSession) -> Faults:
    row = await session.get(VietfulSettings, 1)
    if row is None:  # the migration inserts it; tolerate a hand-cleaned table
        return Faults()
    return Faults(mode=row.fault_mode, latencyMs=row.fault_latency_ms, errorRate=row.fault_error_rate)  # type: ignore[arg-type]


async def get_callback_settings(session: AsyncSession) -> CallbackSettings:
    row = await session.get(VietfulSettings, 1)
    if row is None:
        return CallbackSettings()
    return CallbackSettings(
        duplicateRate=row.callback_duplicate_rate,
        maxRetries=row.callback_max_retries,
        retryDelayMs=row.callback_retry_delay_ms,
        concurrency=row.callback_concurrency,
    )


async def callbacks_page(
    session: AsyncSession, after_id: int, limit: int, status: str | None
) -> list[VietfulCallback]:
    query = select(VietfulCallback).where(VietfulCallback.id > after_id)
    if status:
        query = query.where(VietfulCallback.status == status)
    return list((await session.execute(query.order_by(VietfulCallback.id).limit(limit))).scalars())


async def put_settings(session: AsyncSession, **values: Any) -> None:
    """Upsert columns of the single settings row."""
    await session.execute(
        pg_insert(VietfulSettings)
        .values(id=1, **values)
        .on_conflict_do_update(index_elements=[VietfulSettings.id], set_={**values, "updated_at": func.now()})
    )


async def _insert(session: AsyncSession, rows: list[dict[str, Any]]) -> list[VietfulMutation]:
    products = (await session.execute(insert(VietfulProduct).returning(VietfulProduct), rows)).scalars().all()
    return await _log(
        session,
        [
            {
                "kind": "CREATED",
                "product_id": product.product_id,
                "partner_sku": product.partner_sku,
                "changed_fields": [],
                "data": product_dto(product),
            }
            for product in products
        ],
    )


async def _log(session: AsyncSession, entries: list[dict[str, Any]]) -> list[VietfulMutation]:
    if not entries:
        return []
    result = await session.execute(insert(VietfulMutation).returning(VietfulMutation), entries)
    return list(result.scalars())


def _input_columns(item: ProductInput) -> dict[str, Any]:
    return {
        "sku": item.sku or item.partnerSKU,
        "partner_sku": item.partnerSKU,
        "product_name": item.productName,
        "asset_type": item.assetType,
        "has_serial": item.hasSerial,
        "has_expiration": item.hasExpiration,
        "color": item.color,
        "size": item.size,
        "description": item.description,
        "is_active": item.isActive,
        "units": item.units,
        "categories": [category.model_dump() for category in item.categories],
    }
