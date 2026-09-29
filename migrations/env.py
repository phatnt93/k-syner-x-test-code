"""Alembic environment - synchronous psycopg connection (no event loop needed, works on Windows).

URL precedence: `config.attributes["url"]` (tests) > `-x url=...` > `DATABASE_URL` / `.env`
(cdms.config.Settings).
"""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

import cdms.db.models  # registers every CDMS model on Base.metadata
import cdms.emulator.models  # noqa: F401  # emulator tables (schema `vietful`), same database (D12)
from cdms.config import get_settings
from cdms.db.base import Base

config = context.config
if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

# Schemas owned by this project: default (None = public) and the emulator's; others are never compared.
SCHEMAS = {None, "vietful"}


def _include_name(name: str | None, type_: str, _parent_names: object) -> bool:
    return name in SCHEMAS if type_ == "schema" else True


def _url() -> str:
    url: str | None = config.attributes.get("url") or context.get_x_argument(as_dictionary=True).get("url")
    return url or get_settings().database_url


def run_migrations_offline() -> None:
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        compare_server_default=True,
        include_schemas=True,
        include_name=_include_name,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_server_default=True,
            include_schemas=True,
            include_name=_include_name,
        )
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
