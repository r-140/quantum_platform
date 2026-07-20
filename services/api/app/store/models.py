"""
SQLAlchemy declarative model for the `experiments` table.

`id` is a plain `String`, not a native Postgres `UUID` column type -- the
rest of the codebase already works with `str(uuid.uuid4())` everywhere
(see routers/experiments.py), and using `String` here avoids a type
conversion at every store boundary for no real benefit at this project's
scale. Revisit if UUID-specific query performance ever becomes a measured
concern.

`result` uses JSONB (not plain JSON) for Postgres-native indexing/query
support on the result payload, even though nothing in this project queries
*into* it yet -- cheap to have, and the standard choice for this kind of
column on Postgres.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class ExperimentRow(Base):
    __tablename__ = "experiments"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    algorithm: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    # DateTime(timezone=True) must be explicit here -- SQLAlchemy's default
    # mapping for a bare `Mapped[datetime]` annotation is a *naive*
    # DateTime column, which doesn't match the TIMESTAMPTZ column the
    # migration actually creates (`sa.DateTime(timezone=True)` in
    # migrations/versions/0001_...). Without this, SQLAlchemy compiles
    # INSERT/UPDATE statements that cast bind parameters to `TIMESTAMP
    # WITHOUT TIME ZONE`, and asyncpg then fails to encode Python's
    # timezone-aware `datetime.now(timezone.utc)` values against that cast
    # (`TypeError: can't subtract offset-naive and offset-aware datetimes`)
    # -- discovered by actually running this against Postgres, not caught
    # by the earlier sqlite-based logic check (sqlite has no timezone-aware
    # timestamp type at all, so this mismatch had no way to surface there).
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    result: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)