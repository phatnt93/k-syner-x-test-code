"""Fingerprint of a canonical product (decision D2): two states are equal iff their fingerprints are equal."""

import hashlib
import json

from cdms.core.canonical import PRODUCT_FIELDS, CanonicalProduct

# productId is Vietful's internal id, not business data: a new id alone is not a change.
FINGERPRINT_FIELDS = tuple(field for field in PRODUCT_FIELDS if field != "productId")


def canonical_json(product: CanonicalProduct) -> bytes:
    """Deterministic JSON of the business fields: keys sorted, no whitespace, UTF-8."""
    business = {field: product[field] for field in FINGERPRINT_FIELDS}
    return json.dumps(business, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def fingerprint(product: CanonicalProduct) -> bytes:
    """SHA-256 (32 bytes) of `canonical_json`."""
    return hashlib.sha256(canonical_json(product)).digest()
