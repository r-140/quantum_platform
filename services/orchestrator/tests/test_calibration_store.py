from datetime import datetime, timezone

from app.calibration_store import CalibrationStateStore
from app.tasks.calibration import CalibrationResult


class FakePool:
    def __init__(self, row=None) -> None:
        self.row = row
        self.calls = []

    async def execute(self, query, *args):
        self.calls.append((query, args))

    async def fetchrow(self, query, *args):
        self.calls.append((query, args))
        return self.row


async def test_save_upserts_latest_observation() -> None:
    pool = FakePool()
    store = CalibrationStateStore(pool)
    result = CalibrationResult(
        timestamp="2026-08-19T20:00:00+00:00",
        backend_name="aer-simulator",
        shots=1024,
        error_rate=0.02,
        counts={"00": 500, "11": 504, "01": 10, "10": 10},
    )
    await store.save(result)
    query, args = pool.calls[0]
    assert "ON CONFLICT (backend_name) DO UPDATE" in query
    assert args[:4] == (
        "aer-simulator",
        datetime(2026, 8, 19, 20, 0, tzinfo=timezone.utc),
        1024,
        0.02,
    )


async def test_get_maps_database_row() -> None:
    observed_at = datetime(2026, 8, 19, 20, 0, tzinfo=timezone.utc)
    store = CalibrationStateStore(
        FakePool(
            {
                "backend_name": "aer-simulator",
                "observed_at": observed_at,
                "error_rate": 0.01,
                "shots": 1024,
            }
        )
    )
    observation = await store.get("aer-simulator")
    assert observation is not None
    assert observation.observed_at == observed_at
    assert observation.error_rate == 0.01
