"""Request / response bodies. Field names are Vietful's JSON names (camelCase, `partnerSKU`)."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from cdms.emulator.models import VietfulProduct

MutableField = Literal[
    "sku",
    "productName",
    "hasSerial",
    "hasExpiration",
    "color",
    "size",
    "description",
    "isActive",
    "units",
    "categories",
]
FaultMode = Literal["ok", "down", "slow", "flaky"]


# --- Vietful-compatible -------------------------------------------------------------------------------------


class Category(BaseModel):
    categoryCode: str
    categoryName: str | None = None


class ProductDto(BaseModel):
    """Vietful `ProductDto` (GET /api/v1/Products item)."""

    productId: int
    sku: str | None
    partnerSKU: str
    productName: str | None
    assetType: str | None
    hasSerial: bool | None
    hasExpiration: bool | None
    color: str | None
    size: str | None
    description: str | None
    isActive: bool | None
    units: list[str]
    categories: list[Category]


class WebhookSubscriber(BaseModel):
    """Vietful `UpdateAllEventSubscriberDto`."""

    endpoint: str = Field(min_length=1, max_length=2048)


class Subscriber(BaseModel):
    """Admin view of the subscription (`endpoint` null = none)."""

    endpoint: str | None = Field(default=None, max_length=2048)


def product_dto(product: VietfulProduct) -> dict[str, Any]:
    return {
        "productId": product.product_id,
        "sku": product.sku,
        "partnerSKU": product.partner_sku,
        "productName": product.product_name,
        "assetType": product.asset_type,
        "hasSerial": product.has_serial,
        "hasExpiration": product.has_expiration,
        "color": product.color,
        "size": product.size,
        "description": product.description,
        "isActive": product.is_active,
        "units": product.units,
        "categories": product.categories,
    }


# --- admin (emulator-only) ----------------------------------------------------------------------------------


class SeedRequest(BaseModel):
    count: int = Field(ge=0, le=100_000)
    seed: int = 42


class SeedResponse(BaseModel):
    count: int
    seed: int


class ProductInput(BaseModel):
    """An explicit product for POST /_admin/products; `sku` defaults to `partnerSKU` as in Vietful."""

    partnerSKU: str = Field(min_length=1)
    sku: str | None = None
    productName: str | None = None
    assetType: str | None = "Single"
    hasSerial: bool | None = False
    hasExpiration: bool | None = False
    color: str | None = None
    size: str | None = None
    description: str | None = None
    isActive: bool | None = True
    units: list[str] = []
    categories: list[Category] = []


Notify = Literal["none", "webhook"]  # webhook: one PRODUCT_UPSERTED callback per created / changed product


class CreateProductsRequest(BaseModel):
    count: int = Field(default=0, ge=0, le=10_000)  # Faker-generated products
    items: list[ProductInput] = Field(default=[], max_length=10_000)  # explicit products
    seed: int | None = None
    notify: Notify = "none"


class MutateRequest(BaseModel):
    count: int = Field(ge=1, le=10_000)
    fields: list[MutableField] | None = Field(default=None, min_length=1)  # default: any mutable field
    seed: int | None = None
    notify: Notify = "none"


class Mutation(BaseModel):
    id: int
    kind: Literal["CREATED", "UPDATED"]
    productId: int
    partnerSKU: str
    changedFields: list[str]
    data: dict[str, Any]
    createdAt: datetime


class MutationsResponse(BaseModel):
    mutations: list[Mutation]


class MutationPage(BaseModel):
    items: list[Mutation]
    nextCursor: str | None


class Faults(BaseModel):
    """`slow`: every Vietful call waits `latencyMs`. `flaky`: waits `latencyMs`, then fails with 500 at
    `errorRate`. `down`: every Vietful call fails with 503."""

    mode: FaultMode = "ok"
    latencyMs: int = Field(default=0, ge=0, le=60_000)
    errorRate: float = Field(default=0.0, ge=0.0, le=1.0)


class CallbackSettings(BaseModel):
    """How webhook callbacks are (re)delivered.

    Vietful retries after 15 minutes; `retryDelayMs` is the emulator's fast retry (extension E3).
    `duplicateRate`: share of delivered events that are sent a second time.
    """

    duplicateRate: float = Field(default=0.0, ge=0.0, le=1.0)
    maxRetries: int = Field(default=5, ge=0, le=100)
    retryDelayMs: int = Field(default=2000, ge=0, le=3_600_000)
    concurrency: int = Field(default=4, ge=1, le=100)


class Callback(BaseModel):
    id: int
    eventId: str
    eventType: str
    mutationId: int | None
    status: Literal["PENDING", "DELIVERED", "FAILED"]
    attempts: int
    deliveries: int
    lastStatus: int | None
    lastError: str | None
    createdAt: datetime
    deliveredAt: datetime | None


class CallbackPage(BaseModel):
    items: list[Callback]
    nextCursor: str | None
