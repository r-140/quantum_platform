"""
Tests for `app.sinks.timescale_sink.insert_calibration_event` and
`insert_vqe_iteration_metric`, using a hand-written fake standing in for
`asyncpg.Pool` -- no real TimescaleDB connection needed to verify the
SQL/parameter-binding logic. Consistent with this project's general
approach of not reaching for a mocking framework when a small explicit
fake makes the exact behavior visible.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from app.sinks.timescale_sink import insert_calibration_event, insert_vqe_iteration_metric


class FakePool:
    """Records every `execute()` call instead of touching a real database."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def execute(self, query: str, *args) -> None:
        self.calls.append((query, args))


async def test_insert_calibration_event_parses_timestamp_and_binds_params() -> None:
    pool = FakePool()
    payload = {
        "timestamp": "2026-07-21T18:30:42.924140+00:00",
        "backend_name": "aer-simulator",
        "error_rate": 0.0,
        "shots": 1024,
        "counts": {"00": 512, "11": 512},  # deliberately NOT expected in the bound params
    }

    await insert_calibration_event(pool, payload)

    assert len(pool.calls) == 1
    query, args = pool.calls[0]
    assert "INSERT INTO calibration_events" in query
    assert args == (
        datetime(2026, 7, 21, 18, 30, 42, 924140, tzinfo=timezone.utc),
        "aer-simulator",
        0.0,
        1024,
    )


async def test_insert_calibration_event_does_not_forward_counts() -> None:
    """`counts` (the raw per-shot histogram) is intentionally not part of
    the hypertable schema -- see timescale_sink.py's docstring for why.
    This test would catch an accidental future change that starts passing
    a 5th bound parameter derived from `counts`.
    """
    pool = FakePool()
    payload = {
        "timestamp": "2026-01-01T00:00:00+00:00",
        "backend_name": "x",
        "error_rate": 0.1,
        "shots": 100,
        "counts": {"anything": 1},
    }

    await insert_calibration_event(pool, payload)

    _, args = pool.calls[0]
    assert len(args) == 4


async def test_insert_vqe_iteration_metric_binds_all_params() -> None:
    pool = FakePool()
    payload = {
        "timestamp": "2026-08-16T12:00:00+00:00",
        "experiment_id": "exp-123",
        "iteration": 5,
        "params": [0.1, 0.2, 0.3, 0.4],
        "energy": -1.14,
        "quantum_time_s": 0.5,
        "classical_time_s": 0.01,
        "retry_count": 2,
        "circuit_breaker_trips": 0,
    }

    await insert_vqe_iteration_metric(pool, payload)

    assert len(pool.calls) == 1
    query, args = pool.calls[0]
    assert "INSERT INTO vqe_iteration_metrics" in query
    assert args[0] == datetime(2026, 8, 16, 12, 0, 0, tzinfo=timezone.utc)
    assert args[1] == "exp-123"
    assert args[2] == 5
    assert json.loads(args[3]) == [0.1, 0.2, 0.3, 0.4]
    assert args[4] == -1.14
    assert args[5] == 0.5
    assert args[6] == 0.01
    assert args[7] == 2
    assert args[8] == 0
    assert len(args) == 9


async def test_insert_vqe_iteration_metric_serializes_params_as_json() -> None:
    """`params` is stored as JSONB, not flattened into separate columns --
    see timescale_sink.py's docstring for why (the ansatz parameter count
    isn't fixed forever). This test would catch an accidental future
    change that stops JSON-encoding it before binding.
    """
    pool = FakePool()
    payload = {
        "timestamp": "2026-01-01T00:00:00+00:00",
        "experiment_id": "x",
        "iteration": 1,
        "params": [1.0, 2.0],
        "energy": 0.0,
        "quantum_time_s": 0.0,
        "classical_time_s": 0.0,
        "retry_count": 0,
        "circuit_breaker_trips": 0,
    }

    await insert_vqe_iteration_metric(pool, payload)

    _, args = pool.calls[0]
    assert isinstance(args[3], str)  # JSON-encoded, not a raw Python list
    assert json.loads(args[3]) == [1.0, 2.0]
