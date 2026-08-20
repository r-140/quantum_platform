"""GET /backends -- informational only for now (single hardcoded backend);
will become meaningful once the API supports selecting mock vs. Aer vs.
real hardware per request.
"""

from __future__ import annotations

import os

from fastapi import APIRouter, HTTPException
from sqlalchemy import text

from app.db import get_sessionmaker

router = APIRouter(prefix="/backends", tags=["backends"])


@router.get("")
async def list_backends() -> list[dict]:
    return [
        {
            "name": "aer-simulator",
            "type": "simulator",
            "description": "Local Qiskit Aer simulator (quantum_core.backends.aer_backend.AerBackend)",
        }
    ]


@router.get("/{backend_name}/calibration")
async def get_backend_calibration(backend_name: str) -> dict:
    if not os.environ.get("DATABASE_URL"):
        raise HTTPException(503, "calibration state requires PostgreSQL")
    async with get_sessionmaker()() as session:
        result = await session.execute(
            text(
                """
                SELECT backend_name, observed_at, probe_type, shots,
                       error_rate, counts, updated_at
                FROM backend_calibration_state
                WHERE backend_name = :backend_name
                """
            ),
            {"backend_name": backend_name},
        )
        row = result.mappings().one_or_none()
        if row is None:
            raise HTTPException(404, "backend has no calibration observation")
        return dict(row)
