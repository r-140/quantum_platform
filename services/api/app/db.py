"""
Postgres engine/session lifecycle. `init_db`/`close_db` are called from
app/main.py's lifespan, mirroring app/deps.py's `init_rabbitmq`/
`close_rabbitmq` pattern -- both are process-lifetime singletons set up
once at startup.

`DATABASE_URL` uses the `postgresql+asyncpg://` scheme, not plain
`postgresql://` -- SQLAlchemy needs the driver named explicitly to pick
the async asyncpg dialect over the default sync psycopg one.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


async def init_db(database_url: str) -> None:
    global _engine, _sessionmaker
    _engine = create_async_engine(database_url, pool_pre_ping=True)
    _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False)


async def close_db() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _sessionmaker = None


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    if _sessionmaker is None:
        raise RuntimeError(
            "database not initialized -- init_db() must run first "
            "(normally via app.main's lifespan on startup)"
        )
    return _sessionmaker