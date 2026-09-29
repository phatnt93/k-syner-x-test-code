"""Webhook signature and envelope checks (no database)."""

import base64
import hashlib
import hmac
import json

import pytest

from cdms.ingestion.webhook import MAX_ITEMS, WebhookRejected, content_hash, parse, sign, verify_signature

SECRET = "s3cret"
BODY = b'{"id":"evt-1","timestamp":1790000000,"event":"PRODUCT_UPSERTED","items":[{"partnerSKU":"P-1"}]}'


def test_signature_is_base64_hmac_sha256_of_the_raw_body() -> None:
    expected = base64.b64encode(hmac.new(SECRET.encode(), BODY, hashlib.sha256).digest()).decode()
    assert sign(BODY, SECRET) == expected
    verify_signature(BODY, expected, SECRET)


@pytest.mark.parametrize(
    "signature", [None, "", "abc", sign(BODY, "other secret"), sign(BODY + b" ", SECRET)]
)
def test_bad_signature_is_rejected(signature: str | None) -> None:
    with pytest.raises(WebhookRejected) as exc:
        verify_signature(BODY, signature, SECRET)
    assert (exc.value.status, exc.value.code) == (401, "INVALID_SIGNATURE")


def test_valid_envelope_keeps_extra_fields() -> None:
    body = json.dumps({"id": "e", "timestamp": 1, "event": "INV_CHANGED", "warehouseCode": "W1"}).encode()
    envelope, payload = parse(body)
    assert (envelope.id, envelope.event, envelope.items) == ("e", "INV_CHANGED", None)
    assert payload["warehouseCode"] == "W1"


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b"[]",
        b'{"timestamp": 1, "event": "X"}',
        b'{"id": "", "timestamp": 1, "event": "X"}',
        b'{"id": "e", "timestamp": 0, "event": "X"}',
        b'{"id": "e", "timestamp": "soon", "event": "X"}',
        b'{"id": "e", "timestamp": 1, "event": "PRODUCT_UPSERTED"}',
        b'{"id": "e", "timestamp": 1, "event": "PRODUCT_UPSERTED", "items": []}',
        b'{"id": "e", "timestamp": 1, "event": "PRODUCT_UPSERTED", "items": [1]}',
        json.dumps(
            {"id": "e", "timestamp": 1, "event": "PRODUCT_UPSERTED", "items": [{}] * (MAX_ITEMS + 1)}
        ).encode(),
    ],
    ids=["not-json", "array", "no-id", "empty-id", "zero-ts", "text-ts", "no-items", "empty-items",
         "item-not-object", "too-many-items"],
)  # fmt: skip
def test_invalid_envelope_is_422(body: bytes) -> None:
    with pytest.raises(WebhookRejected) as exc:
        parse(body)
    assert (exc.value.status, exc.value.code) == (422, "INVALID_PAYLOAD")


def test_oversized_body_is_413() -> None:
    with pytest.raises(WebhookRejected) as exc:
        parse(b" " * 1_000_001)
    assert exc.value.status == 413


def test_content_hash_ignores_key_order_and_spacing() -> None:
    a = json.loads('{"id": "e", "timestamp": 1, "event": "X", "items": [{"a": 1, "b": 2}]}')
    b = json.loads('{"items":[{"b":2,"a":1}],"event":"X","timestamp":1,"id":"e"}')
    c = json.loads('{"id": "e", "timestamp": 1, "event": "X", "items": [{"a": 1, "b": 3}]}')
    assert content_hash(a) == content_hash(b) != content_hash(c)
