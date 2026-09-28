"""Canonical product model (decision D2 in docs/assumptions.md).

Every source (a polling page item, a webhook `PRODUCT_UPSERTED` item or an Excel row) is normalized here into
one shape, so the same logical state always yields the same fingerprint whatever mechanism delivered it.

Rules:
- text: Unicode NFC, trimmed, `""` → `None` (Excel / other tools may send decomposed Vietnamese characters);
- booleans: `true/false/1/0/yes/no` (case-insensitive), blank → `None`;
- `units`: a set — list or comma-separated string, blanks and duplicates dropped, sorted;
- `categories`: a set — list of `{categoryCode, categoryName}` or `code:name;code:name`, sorted by code;
- unknown keys (transport metadata such as event id, timestamp, source) are dropped.

Keys stay camelCase as in Vietful `ProductDto`.
"""

import unicodedata
from collections.abc import Mapping
from typing import Any

PRODUCT_FIELDS = (
    "productId",
    "sku",
    "partnerSKU",
    "productName",
    "assetType",
    "hasSerial",
    "hasExpiration",
    "color",
    "size",
    "description",
    "isActive",
    "units",
    "categories",
)
_TEXT_FIELDS = ("sku", "partnerSKU", "productName", "assetType", "color", "size", "description")
_BOOL_FIELDS = ("hasSerial", "hasExpiration", "isActive")
_TRUE = frozenset({"true", "1", "yes"})
_FALSE = frozenset({"false", "0", "no"})

CanonicalProduct = dict[str, Any]


class InvalidProductError(ValueError):
    """The input cannot be mapped to a product (outcome INVALID)."""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(f"{field}: {message}")
        self.field = field
        self.message = message


def canonical_product(raw: Mapping[str, Any]) -> CanonicalProduct:
    """Normalize a ProductDto-like mapping. Raises `InvalidProductError` for unusable input."""
    product: CanonicalProduct = {"productId": _product_id(raw.get("productId"))}
    for field in _TEXT_FIELDS:
        product[field] = _text(raw.get(field), field)
    for field in _BOOL_FIELDS:
        product[field] = _bool(raw.get(field), field)
    product["units"] = _units(raw.get("units"))
    product["categories"] = _categories(raw.get("categories"))

    if product["partnerSKU"] is None:
        raise InvalidProductError("partnerSKU", "is required")
    return {field: product[field] for field in PRODUCT_FIELDS}


def _text(value: Any, field: str) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise InvalidProductError(field, "must be text")
    if isinstance(value, float) and value.is_integer():
        value = int(value)  # a numeric Excel cell such as 1001.0 means the code "1001"
    if isinstance(value, int | float):
        value = str(value)
    if not isinstance(value, str):
        raise InvalidProductError(field, "must be text")
    return unicodedata.normalize("NFC", value).strip() or None


def _bool(value: Any, field: str) -> bool | None:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if not text:
            return None
        if text in _TRUE:
            return True
        if text in _FALSE:
            return False
    raise InvalidProductError(field, f"not a boolean: {value!r}")


def _product_id(value: Any) -> int | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool):
        raise InvalidProductError("productId", "must be an integer")
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    raise InvalidProductError("productId", f"not an integer: {value!r}")


def _units(value: Any) -> list[str]:
    if value is None:
        return []
    items = value.split(",") if isinstance(value, str) else value
    if not isinstance(items, list | tuple):
        raise InvalidProductError("units", "must be a list or a comma-separated string")
    units = {unit for item in items if (unit := _text(item, "units")) is not None}
    return sorted(units)


def _categories(value: Any) -> list[dict[str, str | None]]:
    if value is None:
        return []
    if isinstance(value, str):
        # Blank parts (e.g. a trailing ";") are skipped; ":name" without a code is rejected below.
        pairs: list[tuple[Any, Any]] = [
            (code, name)
            for code, _, name in (part.partition(":") for part in value.split(";") if part.strip())
        ]
    elif isinstance(value, list | tuple):
        pairs = []
        for item in value:
            if not isinstance(item, Mapping):
                raise InvalidProductError("categories", "items must be objects")
            pairs.append((item.get("categoryCode"), item.get("categoryName")))
    else:
        raise InvalidProductError("categories", "must be a list or a 'code:name;code:name' string")

    categories: dict[tuple[str, str | None], dict[str, str | None]] = {}
    for raw_code, raw_name in pairs:
        code = _text(raw_code, "categories")
        if code is None:
            raise InvalidProductError("categories", "categoryCode is required")
        name = _text(raw_name, "categories")
        categories[(code, name)] = {"categoryCode": code, "categoryName": name}
    return [categories[key] for key in sorted(categories, key=lambda k: (k[0], k[1] or ""))]
