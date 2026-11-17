import asyncio
from logging.config import fileConfig

import sqlalchemy as sa
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

from app.core.config import get_settings
from app.core.database import Base
from app.models import *  # noqa: F401,F403
# Agents module models (always loaded so migrations stay in sync; the
# feature itself is gated at runtime by AGENTS_ENABLED).
from app.agents.models import *  # noqa: F401,F403

config = context.config
settings = get_settings()

config.set_main_option("sqlalchemy.url", settings.database_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


#: How this fork once numbered its migrations, before they got a branch of
#: their own: (old stamp, upstream revision that ran, fork revision that ran,
#: what upstream's own migration of the old number adds, as (table, column)).
_FORK_OLD_STAMPS = {
    "099": ("098", "f01", ("accounts", "loan_kind")),
    "100": ("098", "f02", ("accounts", "loan_kind")),
    "109": ("108", "f01", ("users", "theme")),
    "110": ("108", "f02", ("users", "theme")),
}


def _restamp_fork_branch(connection: Connection) -> None:
    """Split a database stamped with this fork's old numbers into two heads.

    The receipts migrations used to sit at the end of upstream's line, as
    099/100 and later 109/110, numbers upstream then gave to migrations of
    its own. Such a database holds one stamp, e.g. "110", while upstream's
    110 never ran, so alembic would skip it. Its mark: the receipts table
    is there and the column upstream's migration of that number adds is
    not. It is restamped as the upstream revision that really ran plus the
    fork's own head, and `upgrade heads` carries on from both.
    """
    inspector = sa.inspect(connection)
    if not inspector.has_table("alembic_version") or not inspector.has_table("receipts"):
        return
    current = connection.execute(sa.text("SELECT version_num FROM alembic_version")).scalars().all()
    if len(current) != 1 or current[0] not in _FORK_OLD_STAMPS:
        return
    upstream, fork, (table, column) = _FORK_OLD_STAMPS[current[0]]
    if any(c["name"] == column for c in inspector.get_columns(table)):
        return  # upstream's own migration of that number ran: the stamp is upstream's
    connection.execute(sa.text("DELETE FROM alembic_version"))
    connection.execute(
        sa.text("INSERT INTO alembic_version (version_num) VALUES (:upstream), (:fork)"),
        {"upstream": upstream, "fork": fork},
    )


def do_run_migrations(connection: Connection) -> None:
    _restamp_fork_branch(connection)
    # The check above began a transaction; alembic leaves one it did not
    # begin uncommitted, which would roll every migration back on close.
    connection.commit()
    context.configure(connection=connection, target_metadata=target_metadata)

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
