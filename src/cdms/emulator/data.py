"""Fake product data (Faker, requirements section 3.2) and random mutations of it.

Deterministic for a given seed, so a seeded catalogue and a seeded mutation run can be reproduced exactly.
Values keep Vietful `ProductDto` types; field names here are the snake_case columns of `VietfulProduct`.
"""

import random
from collections.abc import Callable
from typing import Any

from faker import Faker

UNITS = ("PCS", "BOX", "PACK", "SET", "CAI")
CATEGORIES = (
    ("C01", "Điện thoại"),
    ("C02", "Phụ kiện"),
    ("C03", "Gia dụng"),
    ("C04", "Vật tư"),
    ("C05", "Văn phòng phẩm"),
)
# Vietnamese names on purpose: CDMS must handle diacritics (NFC normalization, D2).
NOUNS = (
    "Kẽm",
    "Ốc vít",
    "Bàn phím",
    "Tai nghe",
    "Ổ cắm",
    "Bóng đèn",
    "Dây cáp",
    "Bình nước",
    "Sổ tay",
    "Ba lô",
)
SIZES = ("S", "M", "L", "XL", "XXL")
ASSET_TYPE = "Single"  # the only value seen in Vietful's sample response

# ProductDto field (camelCase) → column. partnerSKU is the key and never mutated.
MUTABLE_FIELDS = {
    "sku": "sku",
    "productName": "product_name",
    "hasSerial": "has_serial",
    "hasExpiration": "has_expiration",
    "color": "color",
    "size": "size",
    "description": "description",
    "isActive": "is_active",
    "units": "units",
    "categories": "categories",
}


def partner_sku(number: int) -> str:
    return f"P-{number:05d}"


class ProductFaker:
    def __init__(self, seed: int | None = None) -> None:
        self.rng = random.Random(seed)
        self.fake = Faker("en_US")
        if seed is not None:
            self.fake.seed_instance(seed)

    def product(self, number: int) -> dict[str, Any]:
        """Column values of a new product; `number` makes partnerSKU / sku unique."""
        return {
            "sku": f"SKU-{number:05d}",
            "partner_sku": partner_sku(number),
            "product_name": self._name(),
            "asset_type": ASSET_TYPE,
            "has_serial": self.rng.random() < 0.3,
            "has_expiration": self.rng.random() < 0.2,
            "color": self.fake.color_name(),
            "size": self.rng.choice(SIZES),
            "description": self.fake.sentence(nb_words=8),
            "is_active": self.rng.random() < 0.9,
            "units": self._units(),
            "categories": self._categories(),
        }

    def mutate(self, current: dict[str, Any], fields: list[str]) -> dict[str, Any]:
        """New values for 1-2 of `fields` (camelCase), each guaranteed to differ from `current` (columns)."""
        chosen = self.rng.sample(fields, k=min(len(fields), self.rng.choice((1, 1, 2))))
        changes: dict[str, Any] = {}
        for field in chosen:
            column = MUTABLE_FIELDS[field]
            changes[column] = self._different(current[column], self._generator(field))
        return changes

    def _generator(self, field: str) -> Callable[[], Any]:
        generators: dict[str, Callable[[], Any]] = {
            "sku": lambda: f"SKU-{self.rng.randint(10_000, 99_999)}",
            "productName": self._name,
            "hasSerial": lambda: self.rng.random() < 0.5,
            "hasExpiration": lambda: self.rng.random() < 0.5,
            "color": self.fake.color_name,
            "size": lambda: self.rng.choice(SIZES),
            "description": lambda: self.fake.sentence(nb_words=8),
            "isActive": lambda: self.rng.random() < 0.5,
            "units": self._units,
            "categories": self._categories,
        }
        return generators[field]

    def _different(self, current: Any, generate: Callable[[], Any]) -> Any:
        for _ in range(20):
            value = generate()
            if not _same(value, current):
                return value
        if isinstance(current, bool):
            return not current
        raise RuntimeError("could not generate a different value")  # pragma: no cover - 20 misses in a row

    def _name(self) -> str:
        return f"{self.rng.choice(NOUNS)} {self.fake.word().capitalize()} {self.rng.randint(1, 999)}"

    def _units(self) -> list[str]:
        # Unsorted on purpose: order is not meaningful and CDMS must not see a reorder as a change.
        return self.rng.sample(UNITS, k=self.rng.randint(1, 3))

    def _categories(self) -> list[dict[str, str]]:
        picked = self.rng.sample(CATEGORIES, k=self.rng.randint(1, 2))
        return [{"categoryCode": code, "categoryName": name} for code, name in picked]


def _same(a: Any, b: Any) -> bool:
    """Equality with list order ignored (units / categories are sets)."""
    if isinstance(a, list) and isinstance(b, list):
        return sorted(map(repr, a)) == sorted(map(repr, b))
    return bool(a == b)
