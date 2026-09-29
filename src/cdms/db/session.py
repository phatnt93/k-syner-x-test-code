from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from cdms.config import get_settings

_settings = get_settings()

engine: AsyncEngine = create_async_engine(
    _settings.database_url,
    pool_size=_settings.db_pool_size,
    max_overflow=_settings.db_max_overflow,
    pool_pre_ping=True,
    echo=_settings.db_echo,
    # Fail fast when PostgreSQL is down: the API answers 503 (the sender retries) instead of hanging.
    connect_args={"connect_timeout": _settings.db_connect_timeout_s},
)

SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: one session per request. Commit explicitly (`async with session.begin():`)."""
    async with SessionLocal() as session:
        yield session
