"""GET /backends -- informational only for now (single hardcoded backend);
will become meaningful once the API supports selecting mock vs. Aer vs.
real hardware per request.
"""

from __future__ import annotations

from fastapi import APIRouter

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