"""Cursor paging for list endpoints: `?limit=&cursor=` → `{items, nextCursor}` (docs/api.md conventions).

The cursor is the sort key of the last item returned (opaque to clients). One extra row is fetched to know
whether another page exists, so the last page says `nextCursor: null` without an extra request.
"""

from collections.abc import Callable, Sequence
from typing import Annotated, Any

from fastapi import Query

from cdms.api.errors import AppError

Limit = Annotated[int, Query(ge=1, le=500, description="Page size")]
Cursor = Annotated[str | None, Query(description="`nextCursor` of the previous page")]


def next_cursor[T](rows: Sequence[T], limit: int, key: Callable[[T], Any]) -> tuple[list[T], str | None]:
    """`rows` was fetched with `limit + 1`: returns the page and the cursor of the next one."""
    page = list(rows[:limit])
    return page, str(key(page[-1])) if len(rows) > limit else None


def int_cursor(cursor: str) -> int:
    """Cursor of an id-ordered list."""
    if not cursor.isdigit():
        raise AppError(422, "INVALID_CURSOR", "cursor must be the nextCursor of a previous page")
    return int(cursor)
