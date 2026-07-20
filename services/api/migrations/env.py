"""
Alembic migration environment, adapted for SQLAlchemy's async engine.

Alembic's default generated env.py assumes a synchronous engine
(`engine_from_config` + a plain `Connection`) -- that doesn't work with
asyncpg. This follows SQLAlchemy's documented async migration pattern: the
actual migration work (`context.run_migrations()`) is still synchronous
code, but it's invoked via `AsyncConnection.run_sync(...)` from inside an
async engine context, bridging the two.

DATABASE_URL is read from the environment (not from alembic.ini) so the
same setup works in any environment without editing/committing a
connection string -- see alembic.ini's comment.
"""

from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.store.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError(
        "DATABASE_URL environment variable must be set to run migrations "
        "(e.g. postgresql+asyncpg://quantum:quantum@localhost:5432/quantum_platform)"
    )


def run_migrations_offline() -> None:
    """Generates SQL without a live DB connection (`alembic upgrade head --sql`)."""
    context.configure(
        url=DATABASE_URL,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    connectable: AsyncEngine = create_async_engine(DATABASE_URL)

    async with connectable.connect() as connection:
        await connection.run_sync(_do_run_migrations)

    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())