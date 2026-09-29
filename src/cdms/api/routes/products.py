"""Read API over the Change Database: current products and their change history (docs/api.md "Query")."""

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Query
from sqlalchemy import or_, select

from cdms.api.deps import SessionDep
from cdms.api.errors import AppError
from cdms.api.paging import Cursor, Limit, int_cursor, next_cursor
from cdms.db.models import Product, ProductChange
from cdms.schemas.api import ChangeOut, Page, ProductOut

router = APIRouter(prefix="/api/v1", tags=["query"])

Order = Annotated[Literal["asc", "desc"], Query(description="By change id; `asc` = feed order")]


@router.get("/products", response_model=Page[ProductOut], response_model_by_alias=True)
async def list_products(
    session: SessionDep,
    limit: Limit = 50,
    cursor: Cursor = None,
    q: Annotated[str | None, Query(description="Substring of partnerSKU, sku or productName")] = None,
    is_active: Annotated[bool | None, Query(alias="isActive")] = None,
) -> Page[ProductOut]:
    """Current state of each product, ordered by partnerSKU."""
    query = select(Product).order_by(Product.partner_sku).limit(limit + 1)
    if cursor:
        query = query.where(Product.partner_sku > cursor)
    if q and q.strip():
        term = q.strip()
        query = query.where(
            or_(
                Product.partner_sku.icontains(term, autoescape=True),
                Product.sku.icontains(term, autoescape=True),
                Product.product_name.icontains(term, autoescape=True),
            )
        )
    if is_active is not None:
        query = query.where(Product.is_active.is_(is_active))
    rows = list((await session.execute(query)).scalars())
    page, nxt = next_cursor(rows, limit, lambda p: p.partner_sku)
    return Page[ProductOut](items=[ProductOut.model_validate(p) for p in page], next_cursor=nxt)


@router.get("/products/{partner_sku}", response_model=ProductOut, response_model_by_alias=True)
async def get_product(session: SessionDep, partner_sku: str) -> ProductOut:
    product = (
        await session.execute(select(Product).where(Product.partner_sku == partner_sku))
    ).scalar_one_or_none()
    if product is None:
        raise AppError(404, "PRODUCT_NOT_FOUND", f"No product with partnerSKU {partner_sku!r}")
    return ProductOut.model_validate(product)


@router.get("/products/{partner_sku}/changes", response_model=Page[ChangeOut], response_model_by_alias=True)
async def product_changes(
    session: SessionDep, partner_sku: str, limit: Limit = 50, cursor: Cursor = None
) -> Page[ChangeOut]:
    """Version history of one product (v1, v2, …) with the diff of each change."""
    exists = (await session.execute(select(Product.id).where(Product.partner_sku == partner_sku))).first()
    if exists is None:
        raise AppError(404, "PRODUCT_NOT_FOUND", f"No product with partnerSKU {partner_sku!r}")
    query = (
        select(ProductChange)
        .where(ProductChange.partner_sku == partner_sku)
        .order_by(ProductChange.version)
        .limit(limit + 1)
    )
    if cursor:
        query = query.where(ProductChange.version > int_cursor(cursor))
    rows = list((await session.execute(query)).scalars())
    page, nxt = next_cursor(rows, limit, lambda c: c.version)
    return Page[ChangeOut](items=[ChangeOut.model_validate(c) for c in page], next_cursor=nxt)


@router.get("/changes", response_model=Page[ChangeOut], response_model_by_alias=True)
async def change_feed(
    session: SessionDep,
    limit: Limit = 50,
    cursor: Cursor = None,
    order: Order = "asc",
    source: Annotated[Literal["POLLING", "WEBHOOK", "EXCEL"] | None, Query()] = None,
    since: Annotated[
        datetime | None, Query(description="Only changes recorded at or after this time")
    ] = None,
    partner_sku: Annotated[str | None, Query(alias="partnerSKU")] = None,
) -> Page[ChangeOut]:
    """Every stored change in order (`asc`: a consumer's feed; `desc`: newest first)."""
    query = select(ProductChange).limit(limit + 1)
    query = query.order_by(ProductChange.id.asc() if order == "asc" else ProductChange.id.desc())
    if cursor:
        after = int_cursor(cursor)
        query = query.where(ProductChange.id > after if order == "asc" else ProductChange.id < after)
    if source:
        query = query.where(ProductChange.source == source)
    if since:
        query = query.where(ProductChange.recorded_at >= since)
    if partner_sku:
        query = query.where(ProductChange.partner_sku == partner_sku)
    rows = list((await session.execute(query)).scalars())
    page, nxt = next_cursor(rows, limit, lambda c: c.id)
    return Page[ChangeOut](items=[ChangeOut.model_validate(c) for c in page], next_cursor=nxt)
