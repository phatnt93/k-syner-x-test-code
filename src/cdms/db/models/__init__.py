"""ORM models.

Import every model here so `Base.metadata` is complete for Alembic autogenerate.
"""

from cdms.db.models.product import Product
from cdms.db.models.product_change import ProductChange

__all__ = ["Product", "ProductChange"]
