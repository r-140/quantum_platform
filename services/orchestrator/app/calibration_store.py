"""PostgreSQL materialized snapshot of the latest probe per backend."""

from __future__ import annotations

import json
from datetime import datetime

from app.calibration_policy import CalibrationObservation


class CalibrationStateStore:
    def __init__(self, pool) -> None:
        self._pool = pool

    async def save(self, result) -> None:
        await self._pool.execute(
            """
            INSERT INTO backend_calibration_state
                (backend_name, observed_at, probe_type, shots, error_rate, counts)
            VALUES ($1, $2, 'bell_z_parity', $3, $4, $5::jsonb)
            ON CONFLICT (backend_name) DO UPDATE SET
                observed_at = EXCLUDED.observed_at,
                probe_type = EXCLUDED.probe_type,
                shots = EXCLUDED.shots,
                error_rate = EXCLUDED.error_rate,
                counts = EXCLUDED.counts,
                updated_at = now()
            """,
            result.backend_name,
            datetime.fromisoformat(result.timestamp),
            result.shots,
            result.error_rate,
            json.dumps(result.counts, sort_keys=True),
        )

    async def get(self, backend_name: str) -> CalibrationObservation | None:
        row = await self._pool.fetchrow(
            """
            SELECT backend_name, observed_at, error_rate, shots
            FROM backend_calibration_state
            WHERE backend_name = $1
            """,
            backend_name,
        )
        if row is None:
            return None
        return CalibrationObservation(
            backend_name=row["backend_name"],
            observed_at=row["observed_at"],
            error_rate=row["error_rate"],
            shots=row["shots"],
        )
