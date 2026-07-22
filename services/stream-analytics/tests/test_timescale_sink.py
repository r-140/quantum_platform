"""
Tests for `app.sinks.timescale_sink.insert_calibration_event`, using a
hand-written fake standing in for `asyncpg.Pool` -- no real TimescaleDB
connection needed to verify the SQL/parameter-binding logic. Consistent
with this project's general approach of not reaching for a mocking
framework when a small explicit fake makes the exact behavior visible.
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.sinks.timescale_sink import insert_calibration_event


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
