"""Response / request bodies of the CDMS query and config API (docs/api.md).

JSON is camelCase; product fields keep Vietful's names (`partnerSKU`, `productId`) so a product looks the same
in Vietful's API, the webhook payload and CDMS.
"""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic.alias_generators import to_camel

_VIETFUL_NAMES = {"partner_sku": "partnerSKU"}


def _alias(name: str) -> str:
    return _VIETFUL_NAMES.get(name) or to_camel(name)


class ApiModel(BaseModel):
    model_config = ConfigDict(alias_generator=_alias, populate_by_name=True, from_attributes=True)


def _hex(value: Any) -> Any:
    return value.hex() if isinstance(value, bytes) else value


class Page[T](ApiModel):
    items: list[T]
    next_cursor: str | None = Field(description="Pass as `cursor` to get the next page; null = last page")


# --- products and changes -----------------------------------------------------------------------------------


class ProductOut(ApiModel):
    partner_sku: str
    product_id: int | None
    sku: str | None
    product_name: str | None
    asset_type: str | None
    has_serial: bool | None
    has_expiration: bool | None
    color: str | None
    size: str | None
    description: str | None
    is_active: bool | None
    units: list[str]
    categories: list[dict[str, Any]]
    version: int
    fingerprint: str  # hex
    observed_at: datetime
    created_at: datetime
    updated_at: datetime

    _fingerprint_hex = field_validator("fingerprint", mode="before")(_hex)


class ChangeOut(ApiModel):
    id: int
    partner_sku: str
    version: int
    change_type: Literal["CREATED", "UPDATED"]
    source: Literal["POLLING", "WEBHOOK", "EXCEL"]
    source_ref: str
    diff: dict[str, Any]  # {field: [old, new]}
    data: dict[str, Any]  # full state after the change
    prev_fingerprint: str | None  # hex
    fingerprint: str  # hex
    observed_at: datetime
    recorded_at: datetime

    _fingerprints_hex = field_validator("fingerprint", "prev_fingerprint", mode="before")(_hex)


# --- webhook inbox and poll runs ----------------------------------------------------------------------------


class InboxEventOut(ApiModel):
    id: int
    event_id: str
    event_type: str
    status: Literal["PENDING", "PROCESSED", "IGNORED", "DEAD"]
    items: int
    outcome: dict[str, int] | None
    error: str | None
    event_ts: datetime
    received_at: datetime
    processed_at: datetime | None


class InboxEventDetail(InboxEventOut):
    payload: dict[str, Any]


class PollRunOut(ApiModel):
    id: int
    trigger: Literal["SCHEDULE", "MANUAL"]
    status: Literal["RUNNING", "SUCCEEDED", "FAILED", "ABORTED"]
    pages: int
    items: int
    created: int
    updated: int
    unchanged: int
    stale: int
    invalid: int
    error: str | None
    started_at: datetime
    finished_at: datetime | None


# --- stats and config ---------------------------------------------------------------------------------------


class Stats(ApiModel):
    products: int
    changes_by_source: dict[str, int]
    changes_by_type: dict[str, int]
    inbox_by_status: dict[str, int]
    jobs_by_status: dict[str, int]
    job_backlog: int = Field(description="PENDING + RUNNING jobs")
    oldest_pending_job_age_seconds: float | None
    last_poll_run: PollRunOut | None


class SyncConfigOut(ApiModel):
    poll_enabled: bool
    poll_interval_seconds: int
    poll_page_size: int
    http_connect_timeout_ms: int
    http_read_timeout_ms: int
    http_max_attempts: int
    job_max_attempts: int
    updated_at: datetime


class SyncConfigUpdate(ApiModel):
    """Only the fields sent are changed; ranges match the table's CHECK constraints."""

    poll_enabled: bool | None = None
    poll_interval_seconds: int | None = Field(default=None, ge=5, le=86_400)
    poll_page_size: int | None = Field(default=None, ge=1, le=500)
    http_connect_timeout_ms: int | None = Field(default=None, ge=100, le=60_000)
    http_read_timeout_ms: int | None = Field(default=None, ge=100, le=300_000)
    http_max_attempts: int | None = Field(default=None, ge=1, le=10)
    job_max_attempts: int | None = Field(default=None, ge=1, le=100)
