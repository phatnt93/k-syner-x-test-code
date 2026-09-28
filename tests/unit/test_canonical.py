"""One test per normalization rule of decision D2 (cdms.core.canonical)."""

import unicodedata
from typing import Any

import pytest

from cdms.core.canonical import PRODUCT_FIELDS, InvalidProductError, canonical_product
from cdms.core.fingerprint import canonical_json, fingerprint


def product(**overrides: Any) -> dict[str, Any]:
    """A Vietful ProductDto as in docs/project-info/products_res.txt."""
    base: dict[str, Any] = {
        "productId": 123,
        "sku": "SKU-1",
        "partnerSKU": "P-1001",
        "productName": "Kẽm",
        "assetType": "Single",
        "hasSerial": True,
        "hasExpiration": False,
        "color": "Red",
        "size": "XXL",
        "description": "Galvanized",
        "isActive": True,
        "units": ["PCS", "BOX"],
        "categories": [
            {"categoryCode": "C02", "categoryName": "Metal"},
            {"categoryCode": "C01", "categoryName": "Raw"},
        ],
    }
    return base | overrides


def test_output_has_exactly_the_product_fields() -> None:
    result = canonical_product(product(eventId="evt-1", timestamp="2026-09-28T10:00:00Z", source="WEBHOOK"))
    assert tuple(result) == PRODUCT_FIELDS


def test_text_is_trimmed() -> None:
    assert canonical_product(product(productName="  Kẽm \t"))["productName"] == "Kẽm"


@pytest.mark.parametrize("blank", ["", "   ", None])
def test_blank_text_is_null(blank: str | None) -> None:
    assert canonical_product(product(color=blank))["color"] is None


def test_missing_optional_field_is_null() -> None:
    raw = product()
    del raw["size"]
    assert canonical_product(raw)["size"] is None


def test_text_is_nfc_normalized() -> None:
    decomposed = unicodedata.normalize("NFD", "Kẽm")
    assert decomposed != "Kẽm"
    assert canonical_product(product(productName=decomposed))["productName"] == "Kẽm"


def test_numeric_text_cell_becomes_code() -> None:
    assert canonical_product(product(partnerSKU=1001.0, sku=42))["partnerSKU"] == "1001"
    assert canonical_product(product(sku=42))["sku"] == "42"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (True, True), (False, False), (1, True), (0, False),
        ("true", True), ("TRUE", True), (" yes ", True), ("1", True),
        ("false", False), ("No", False), ("0", False),
        ("", None), (None, None),
    ],
)  # fmt: skip
def test_boolean_parsing(raw: Any, expected: bool | None) -> None:
    assert canonical_product(product(isActive=raw))["isActive"] is expected


@pytest.mark.parametrize("raw", ["maybe", 2, 1.5, "y"])
def test_invalid_boolean_is_rejected(raw: Any) -> None:
    with pytest.raises(InvalidProductError) as exc:
        canonical_product(product(hasSerial=raw))
    assert exc.value.field == "hasSerial"


@pytest.mark.parametrize(("raw", "expected"), [(123, 123), ("123", 123), (" 7 ", 7), (9.0, 9), ("", None)])
def test_product_id_parsing(raw: Any, expected: int | None) -> None:
    assert canonical_product(product(productId=raw))["productId"] == expected


@pytest.mark.parametrize("raw", ["abc", True, 1.5])
def test_invalid_product_id_is_rejected(raw: Any) -> None:
    with pytest.raises(InvalidProductError):
        canonical_product(product(productId=raw))


@pytest.mark.parametrize("sku", [None, "", "   "])
def test_partner_sku_is_required(sku: str | None) -> None:
    with pytest.raises(InvalidProductError) as exc:
        canonical_product(product(partnerSKU=sku))
    assert exc.value.field == "partnerSKU"


def test_units_are_a_sorted_set() -> None:
    assert canonical_product(product(units=[" PCS", "BOX", "PCS", ""]))["units"] == ["BOX", "PCS"]


def test_units_from_comma_separated_string() -> None:
    assert canonical_product(product(units="PCS, BOX,,"))["units"] == ["BOX", "PCS"]


@pytest.mark.parametrize("empty", [None, [], "", " , "])
def test_empty_units(empty: Any) -> None:
    assert canonical_product(product(units=empty))["units"] == []


def test_categories_are_sorted_by_code_and_deduplicated() -> None:
    raw = [
        {"categoryCode": "C02", "categoryName": " Metal "},
        {"categoryCode": "C01", "categoryName": "Raw"},
        {"categoryCode": "C02", "categoryName": "Metal"},
    ]
    assert canonical_product(product(categories=raw))["categories"] == [
        {"categoryCode": "C01", "categoryName": "Raw"},
        {"categoryCode": "C02", "categoryName": "Metal"},
    ]


def test_categories_from_excel_string() -> None:
    assert canonical_product(product(categories="C02:Metal; C01:Raw;C03:"))["categories"] == [
        {"categoryCode": "C01", "categoryName": "Raw"},
        {"categoryCode": "C02", "categoryName": "Metal"},
        {"categoryCode": "C03", "categoryName": None},
    ]


@pytest.mark.parametrize("raw", [[{"categoryCode": " ", "categoryName": "X"}], "C01:Raw; :X"])
def test_category_without_code_is_rejected(raw: Any) -> None:
    with pytest.raises(InvalidProductError) as exc:
        canonical_product(product(categories=raw))
    assert exc.value.field == "categories"


def test_same_state_from_poll_webhook_and_excel_has_one_fingerprint() -> None:
    polled = canonical_product(product())
    webhook = canonical_product(product(units=["BOX", "PCS"], eventId="evt-9", productName=" Kẽm "))
    excel = canonical_product(
        product(productId=None, units="BOX,PCS", categories="C01:Raw;C02:Metal", hasSerial="yes", isActive=1,
                hasExpiration="0")
    )  # fmt: skip
    assert fingerprint(polled) == fingerprint(webhook) == fingerprint(excel)


def test_product_id_is_not_part_of_the_fingerprint() -> None:
    assert fingerprint(canonical_product(product(productId=1))) == fingerprint(
        canonical_product(product(productId=2))
    )


@pytest.mark.parametrize(
    "change",
    [{"productName": "Kem"}, {"isActive": False}, {"units": ["PCS"]}, {"color": None}, {"sku": "SKU-2"}],
)
def test_business_change_changes_the_fingerprint(change: dict[str, Any]) -> None:
    assert fingerprint(canonical_product(product())) != fingerprint(canonical_product(product(**change)))


def test_canonical_json_is_compact_sorted_utf8() -> None:
    raw = canonical_json(canonical_product(product()))
    assert raw.startswith(b'{"assetType":"Single","categories":[{"categoryCode":"C01"')
    assert b", " not in raw and b'": ' not in raw
    assert "Kẽm".encode() in raw
    assert len(fingerprint(canonical_product(product()))) == 32
