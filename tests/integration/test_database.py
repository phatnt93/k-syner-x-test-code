"""Smoke test: the test database is reachable and migrated (via the `migrated_db` fixture)."""

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from cdms.config import get_settings


async def test_connected_to_test_database(db_engine: AsyncEngine) -> None:
    async with db_engine.connect() as conn:
        name = (await conn.execute(text("SELECT current_database()"))).scalar_one()
    assert name == get_settings().db_test_name
    assert name != get_settings().db_name


async def test_schema_is_at_head(db_engine: AsyncEngine, alembic_cfg: Config) -> None:
    head = ScriptDirectory.from_config(alembic_cfg).get_current_head()
    async with db_engine.connect() as conn:
        has_version_table = (
            await conn.execute(text("SELECT to_regclass('public.alembic_version') IS NOT NULL"))
        ).scalar_one()
        current = (
            (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar_one_or_none()
            if has_version_table
            else None
        )
    assert current == head  # both None while there are no migrations yet
