"""
Persists raw calibration events into TimescaleDB, so calibration history
survives a process restart -- unlike `RollingErrorRate`'s in-memory window
(see rolling.py's docstring, and docs/architecture/kafka.md's "Пока не
реализовано" for that limitation).

Uses `asyncpg` directly, not SQLAlchemy: this service's only database need
is a single append-only INSERT per calibration event into a hypertable --
a full ORM would be pure overhead here. Contrast with services/api, where
a proper storage abstraction with multiple swappable implementations
(in-memory/Postgres) justified SQLAlchemy's extra weight.

The table itself (`calibration_events`, converted to a TimescaleDB
hypertable) is created by init/001_create_hypertable.sql, which Postgres/
TimescaleDB's Docker image runs automatically on first container startup
-- no separate migration tool needed for a single table this simple.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import asyncpg

_INSERT_SQL = """
INSERT INTO calibration_events (time, backend_name, error_rate, shots)
VALUES ($1, $2, $3, $4)
"""


async def create_pool(dsn: str) -> asyncpg.Pool:
    return await asyncpg.create_pool(dsn)


async def insert_calibration_event(pool: asyncpg.Pool, payload: dict[str, Any]) -> None:
    """`payload` is the parsed JSON body of a calibration-results Kafka
    message -- see orchestrator/app/tasks/calibration.py's
    `CalibrationResult` for the exact shape (`timestamp`, `backend_name`,
    `error_rate`, `shots`, `counts`). `counts` is intentionally not stored
    here -- this table is for the aggregate metric time series, not raw
    per-shot histograms; `counts` stays in the Kafka log itself (and,
    later, wherever raw event replay might be needed) rather than being
    duplicated into this hypertable.
    """
    timestamp = datetime.fromisoformat(payload["timestamp"])
    await pool.execute(
        _INSERT_SQL,
        timestamp,
        payload["backend_name"],
        payload["error_rate"],
        payload["shots"],
    )