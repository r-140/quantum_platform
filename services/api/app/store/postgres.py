"""
Postgres-backed ExperimentStore, using SQLAlchemy 2.0's async ORM +
asyncpg driver.

`save()` uses Postgres's native `INSERT ... ON CONFLICT DO UPDATE`
(SQLAlchemy's `postgresql.insert(...).on_conflict_do_update(...)`) rather
than a separate "does it exist? update or insert" round trip -- one
statement, no race condition between the existence check and the write.
This mirrors the upsert logic already verified against sqlite as a
standalone sanity check before this file was written (sqlite's `ON
CONFLICT DO UPDATE` syntax is equivalent for this purpose, though sqlite
itself was only used to check the *logic*, not as a stand-in for
Postgres-specific behavior -- see docs/architecture/postgres.md).
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.schemas.experiments import ExperimentResponse
from app.store.base import ExperimentStore
from app.store.models import ExperimentRow


def _row_to_response(row: ExperimentRow) -> ExperimentResponse:
    return ExperimentResponse(
        id=row.id,
        algorithm=row.algorithm,
        status=row.status,
        submitted_at=row.submitted_at,
        completed_at=row.completed_at,
        result=row.result,
        error=row.error,
    )


class PostgresExperimentStore(ExperimentStore):
    """Holds a reference to a shared `async_sessionmaker` (created once at
    app startup, see app/db.py) rather than a single long-lived session --
    each method opens a short-lived session for its own operation, per
    SQLAlchemy's recommended "one session per unit of work" pattern.
    """

    def __init__(self, sessionmaker: async_sessionmaker) -> None:
        self._sessionmaker = sessionmaker

    async def save(self, experiment: ExperimentResponse) -> None:
        values = {
            "id": experiment.id,
            "algorithm": experiment.algorithm,
            "status": experiment.status,
            "submitted_at": experiment.submitted_at,
            "completed_at": experiment.completed_at,
            "result": experiment.result,
            "error": experiment.error,
        }
        stmt = pg_insert(ExperimentRow).values(**values)
        stmt = stmt.on_conflict_do_update(
            index_elements=["id"],
            set_={k: v for k, v in values.items() if k != "id"},
        )
        async with self._sessionmaker() as session:
            await session.execute(stmt)
            await session.commit()

    async def get(self, experiment_id: str) -> ExperimentResponse | None:
        async with self._sessionmaker() as session:
            row = await session.get(ExperimentRow, experiment_id)
            return _row_to_response(row) if row is not None else None

    async def list_all(self) -> list[ExperimentResponse]:
        async with self._sessionmaker() as session:
            result = await session.execute(
                select(ExperimentRow).order_by(ExperimentRow.submitted_at)
            )
            return [_row_to_response(row) for row in result.scalars().all()]
