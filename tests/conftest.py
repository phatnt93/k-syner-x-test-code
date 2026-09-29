import asyncio
import os
import sys
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from cdms.config import get_settings

ROOT = Path(__file__).resolve().parents[1]

# Tests never depend on the developer's real token; set before any module reads the settings.
os.environ["INVENTORY_API_TOKEN"] = "test-emulator-token"

if sys.platform == "win32":
    # Async psycopg needs a selector loop on Windows (see cdms.loop).
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


@pytest.fixture(scope="session")
def test_database_url() -> str:
    settings = get_settings()
    if settings.db_test_name == settings.db_name:
        pytest.exit("DB_TEST_NAME must differ from DB_NAME: integration tests wipe the test database")
    return settings.test_database_url


@pytest.fixture(scope="session")
def alembic_cfg(test_database_url: str) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.attributes["configure_logger"] = False
    cfg.attributes["url"] = test_database_url
    return cfg


@pytest.fixture(scope="session")
def migrated_db(test_database_url: str, alembic_cfg: Config) -> Iterator[str]:
    """Fresh schema for the test session: downgrade to base, then upgrade to head."""
    command.downgrade(alembic_cfg, "base")
    command.upgrade(alembic_cfg, "head")
    yield test_database_url


@pytest.fixture(scope="session")
async def db_engine(migrated_db: str) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(migrated_db)
    yield engine
    await engine.dispose()
