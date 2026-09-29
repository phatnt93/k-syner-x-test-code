"""HTTP routes: Vietful-compatible `/api/v1/...` (docs/reference/vietful-api-notes.md) and `/_admin`."""

import asyncio
import hmac
import random
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Response
from fastapi.responses import RedirectResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from cdms.config import get_settings
from cdms.db.session import get_session
from cdms.emulator import callbacks, catalog
from cdms.emulator.errors import VietfulError
from cdms.emulator.models import VietfulCallback, VietfulMutation, VietfulProduct, VietfulSettings
from cdms.emulator.schemas import (
    Callback,
    CallbackPage,
    CallbackSettings,
    CreateProductsRequest,
    Faults,
    MutateRequest,
    Mutation,
    MutationPage,
    MutationsResponse,
    ProductDto,
    SeedRequest,
    SeedResponse,
    Subscriber,
    WebhookSubscriber,
    product_dto,
)

SessionDep = Annotated[AsyncSession, Depends(get_session)]

MAX_PAGE_SIZE = 1_000  # Vietful documents no maximum; the emulator caps it (extension)
_fault_rng = random.Random()


async def apply_faults(session: SessionDep) -> None:
    """Emulated outages (PUT /_admin/faults); runs before auth, like an unreachable service would."""
    faults = await catalog.get_faults(session)
    await session.rollback()  # end the read transaction; handlers start their own
    if faults.mode == "down":
        raise VietfulError(503, "SERVICE_UNAVAILABLE", "Inventory service unavailable (emulated fault)")
    if faults.mode in ("slow", "flaky") and faults.latencyMs:
        await asyncio.sleep(faults.latencyMs / 1000)
    if faults.mode == "flaky" and _fault_rng.random() < faults.errorRate:
        raise VietfulError(500, "INTERNAL_ERROR", "Transient failure (emulated fault)")


# auto_error=False: keep Vietful's error body (VietfulError) instead of FastAPI's default 403 / 401
_bearer = HTTPBearer(auto_error=False, description="Static token = INVENTORY_API_TOKEN (extension E1)")


async def require_token(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> None:
    """Static bearer token (extension E1) instead of Vietful's OAuth2 client credentials."""
    expected = get_settings().inventory_api_token
    if expected is None:
        raise VietfulError(500, "NOT_CONFIGURED", "INVENTORY_API_TOKEN is not set")
    if credentials is None or not hmac.compare_digest(
        credentials.credentials.encode(), expected.get_secret_value().encode()
    ):
        raise VietfulError(
            401, "UNAUTHORIZED", "Missing or invalid bearer token", headers={"WWW-Authenticate": "Bearer"}
        )


vietful = APIRouter(
    prefix="/api/v1", tags=["vietful"], dependencies=[Depends(apply_faults), Depends(require_token)]
)
admin = APIRouter(prefix="/_admin", tags=["admin"])
ops = APIRouter(tags=["ops"])


def _csv(value: str | None) -> list[str]:
    return [part.strip() for part in (value or "").split(",") if part.strip()]


# --- Vietful-compatible -------------------------------------------------------------------------------------


@vietful.get("/Products", response_model=list[ProductDto])
async def list_products(
    session: SessionDep,
    keyword: Annotated[str | None, Query(alias="Keyword", description="SKU or product name")] = None,
    partner_skus: Annotated[str | None, Query(alias="PartnerSKUs", description="Comma-separated")] = None,
    skus: Annotated[str | None, Query(alias="SKUs", description="Comma-separated")] = None,
    page_index: Annotated[int, Query(alias="PageIndex", ge=0)] = 0,
    page_size: Annotated[int, Query(alias="PageSize", ge=1, le=MAX_PAGE_SIZE)] = 10,
) -> list[dict[str, object]]:
    """Get product list. Ordered by product identity number. Plain JSON array, no paging envelope."""
    query = select(VietfulProduct)
    if keyword and keyword.strip():
        term = keyword.strip()
        query = query.where(
            VietfulProduct.sku.icontains(term, autoescape=True)
            | VietfulProduct.product_name.icontains(term, autoescape=True)
        )
    if partner_list := _csv(partner_skus):
        query = query.where(VietfulProduct.partner_sku.in_(partner_list))
    if sku_list := _csv(skus):
        query = query.where(VietfulProduct.sku.in_(sku_list))
    query = query.order_by(VietfulProduct.product_id).offset(page_index * page_size).limit(page_size)
    return [product_dto(product) for product in (await session.execute(query)).scalars()]


@vietful.get("/Products/{partnerSKU}", response_model=ProductDto)
async def get_product(session: SessionDep, partnerSKU: str) -> dict[str, object]:  # Vietful's param name
    """Get product details (the `ProductDto` subset of Vietful's `ProductFullDetailDto`)."""
    product = (
        await session.execute(select(VietfulProduct).where(VietfulProduct.partner_sku == partnerSKU))
    ).scalar_one_or_none()
    if product is None:
        # Vietful documents only 400 + ExceptionErrorModel for this route; the code is the emulator's.
        raise VietfulError(400, "PRODUCT_NOT_FOUND", f"Product {partnerSKU!r} not found")
    return product_dto(product)


@vietful.post("/WebhookSubscribers", status_code=204)
async def subscribe(session: SessionDep, body: WebhookSubscriber) -> Response:
    """Create webhook subscribers: every event is POSTed to `endpoint` (delivery: C-03b)."""
    await catalog.put_settings(session, webhook_endpoint=body.endpoint)
    await session.commit()
    return Response(status_code=204)


# --- admin (emulator-only, no auth: local prototype, D11) ---------------------------------------------------


@admin.post("/seed", response_model=SeedResponse)
async def seed(session: SessionDep, body: SeedRequest) -> SeedResponse:
    """Replace the catalogue with `count` Faker products (deterministic per seed); clears the mutation log."""
    await catalog.seed(session, body.count, body.seed)
    await session.commit()
    return SeedResponse(count=body.count, seed=body.seed)


@admin.post("/products", response_model=MutationsResponse, status_code=201)
async def create_products(session: SessionDep, body: CreateProductsRequest) -> MutationsResponse:
    try:
        mutations = await catalog.create(session, body.count, body.items, body.seed)
        await _notify(session, mutations, body.notify)
        await session.commit()
    except IntegrityError as exc:
        raise VietfulError(409, "PRODUCT_EXISTS", "A product with this partnerSKU already exists") from exc
    return MutationsResponse(mutations=[_mutation(m) for m in mutations])


@admin.post("/mutate", response_model=MutationsResponse)
async def mutate(session: SessionDep, body: MutateRequest) -> MutationsResponse:
    """Change 1-2 fields of `count` random products (all products if there are fewer). `notify: "webhook"`
    queues one `PRODUCT_UPSERTED` callback per change, in the same transaction."""
    mutations = await catalog.mutate(session, body.count, body.fields, body.seed)
    await _notify(session, mutations, body.notify)
    await session.commit()
    return MutationsResponse(mutations=[_mutation(m) for m in mutations])


@admin.get("/mutations", response_model=MutationPage)
async def mutations(
    session: SessionDep,
    cursor: Annotated[str | None, Query(pattern=r"^\d+$")] = None,
    limit: Annotated[int, Query(ge=1, le=1_000)] = 100,
) -> MutationPage:
    """The mutation log in order (ground truth for CDMS's change history)."""
    page = await catalog.mutations_page(session, int(cursor or 0), limit)
    return MutationPage(
        items=[_mutation(m) for m in page], nextCursor=str(page[-1].id) if len(page) == limit else None
    )


@admin.get("/subscriber", response_model=Subscriber)
async def get_subscriber(session: SessionDep) -> Subscriber:
    endpoint = (await session.execute(select(VietfulSettings.webhook_endpoint))).scalar_one_or_none()
    return Subscriber(endpoint=endpoint)


@admin.put("/subscriber", response_model=Subscriber)
async def put_subscriber(session: SessionDep, body: Subscriber) -> Subscriber:
    """Same effect as Vietful's `POST /api/v1/WebhookSubscribers` without the bearer token (for the /ui
    console); `endpoint: null` unsubscribes."""
    await catalog.put_settings(session, webhook_endpoint=body.endpoint)
    await session.commit()
    return body


@admin.get("/callback", response_model=CallbackSettings)
async def get_callback(session: SessionDep) -> CallbackSettings:
    return await catalog.get_callback_settings(session)


@admin.put("/callback", response_model=CallbackSettings)
async def put_callback(session: SessionDep, body: CallbackSettings) -> CallbackSettings:
    await catalog.put_settings(
        session,
        callback_duplicate_rate=body.duplicateRate,
        callback_max_retries=body.maxRetries,
        callback_retry_delay_ms=body.retryDelayMs,
        callback_concurrency=body.concurrency,
    )
    await session.commit()
    return body


@admin.get("/callbacks", response_model=CallbackPage)
async def list_callbacks(
    session: SessionDep,
    status: Annotated[Literal["PENDING", "DELIVERED", "FAILED"] | None, Query()] = None,
    cursor: Annotated[str | None, Query(pattern=r"^\d+$")] = None,
    limit: Annotated[int, Query(ge=1, le=1_000)] = 100,
) -> CallbackPage:
    """The callback outbox: what was sent, how often, and what the receiver answered."""
    page = await catalog.callbacks_page(session, int(cursor or 0), limit, status)
    return CallbackPage(
        items=[_callback(c) for c in page], nextCursor=str(page[-1].id) if len(page) == limit else None
    )


@admin.get("/faults", response_model=Faults)
async def get_faults(session: SessionDep) -> Faults:
    return await catalog.get_faults(session)


@admin.put("/faults", response_model=Faults)
async def put_faults(session: SessionDep, body: Faults) -> Faults:
    await catalog.put_settings(
        session, fault_mode=body.mode, fault_latency_ms=body.latencyMs, fault_error_rate=body.errorRate
    )
    await session.commit()
    return body


@ops.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@ops.get("/", include_in_schema=False)
async def root() -> RedirectResponse:
    """The emulator has no UI of its own; land on Swagger."""
    return RedirectResponse("/docs")


async def _notify(session: AsyncSession, mutations: list[VietfulMutation], notify: str) -> None:
    if notify != "webhook":
        return
    try:
        await callbacks.enqueue(session, mutations)
    except callbacks.NoSubscriber as exc:
        raise VietfulError(409, "NO_SUBSCRIBER", str(exc)) from exc


def _callback(c: VietfulCallback) -> Callback:
    return Callback(
        id=c.id,
        eventId=c.event_id,
        eventType=c.event_type,
        mutationId=c.mutation_id,
        status=c.status,  # type: ignore[arg-type]
        attempts=c.attempts,
        deliveries=c.deliveries,
        lastStatus=c.last_status,
        lastError=c.last_error,
        createdAt=c.created_at,
        deliveredAt=c.delivered_at,
    )


def _mutation(m: VietfulMutation) -> Mutation:
    return Mutation(
        id=m.id,
        kind=m.kind,  # type: ignore[arg-type]
        productId=m.product_id,
        partnerSKU=m.partner_sku,
        changedFields=m.changed_fields,
        data=m.data,
        createdAt=m.created_at,
    )
