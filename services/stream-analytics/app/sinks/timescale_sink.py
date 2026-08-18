"""
Persists raw calibration events into TimescaleDB, so calibration history
survives a process restart -- unlike `RollingErrorRate`'s in-memory window
(see rolling.py's docstring, and docs/architecture/kafka.md's "Пока не
реализовано" for that limitation).

Also persists per-COBYLA-iteration VQE metrics (see
`insert_vqe_iteration_metric` below) -- the "VQE metrics for the hw/sw
interaction loop" item from docs/tech-debt.md, published by orchestrator
to the `vqe-iteration-metrics` Kafka topic and consumed here the same way
calibration events are.

Uses `asyncpg` directly, not SQLAlchemy: this service's only database need
is a single append-only INSERT per event into a hypertable -- a full ORM
would be pure overhead here. Contrast with services/api, where a proper
storage abstraction with multiple swappable implementations
(in-memory/Postgres) justified SQLAlchemy's extra weight.

Both tables (`calibration_events`, `vqe_iteration_metrics`, each converted
to a TimescaleDB hypertable) are created by init/*.sql, which Postgres/
TimescaleDB's Docker image runs automatically on first container startup
-- no separate migration tool needed for tables this simple.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import asyncpg

_INSERT_CALIBRATION_SQL = """
INSERT INTO calibration_events (time, backend_name, error_rate, shots)
VALUES ($1, $2, $3, $4)
"""

_INSERT_VQE_METRIC_SQL = """
INSERT INTO vqe_iteration_metrics
    (time, experiment_id, iteration, params, energy,
     quantum_time_s, classical_time_s, retry_count, circuit_breaker_trips)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
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
        _INSERT_CALIBRATION_SQL,
        timestamp,
        payload["backend_name"],
        payload["error_rate"],
        payload["shots"],
    )


async def insert_vqe_iteration_metric(pool: asyncpg.Pool, payload: dict[str, Any]) -> None:
    """`payload` is the parsed JSON body of a vqe-iteration-metrics Kafka
    message -- see orchestrator/app/tasks/vqe_metrics.py's
    `VQEIterationMetricsMessage` for the exact shape. `params` is stored
    as JSONB (a small list of floats, the ansatz parameters for this
    iteration) rather than flattened into separate columns -- the ansatz
    parameter count isn't fixed forever (see the LiH/BeH2 item in
    docs/tech-debt.md, which would need more parameters than H2's
    current 4), and JSONB avoids a schema migration if/when that happens.
    """
    timestamp = datetime.fromisoformat(payload["timestamp"])
    await pool.execute(
        _INSERT_VQE_METRIC_SQL,
        timestamp,
        payload["experiment_id"],
        payload["iteration"],
        json.dumps(payload["params"]),
        payload["energy"],
        payload["quantum_time_s"],
        payload["classical_time_s"],
        payload["retry_count"],
        payload["circuit_breaker_trips"],
    )