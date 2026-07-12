"""
FastAPI application entry point.

Run with (from services/api/):
    uvicorn app.main:app --reload --port 8000

Then either use the interactive docs at http://localhost:8000/docs, or:
    curl -X POST http://localhost:8000/experiments \\
        -H "Content-Type: application/json" \\
        -d '{"algorithm": "grover", "marked_states": ["101"]}'
"""

from __future__ import annotations

from fastapi import FastAPI

from app.routers import backends, experiments

app = FastAPI(
    title="Quantum Platform API",
    description="Accepts quantum experiment requests (Grover, SAT-Grover, QPE, VQE) "
    "and runs them against a QuantumBackend. Currently executes synchronously "
    "in-process; a queue-based orchestrator is the next piece of this project.",
    version="0.1.0",
)

app.include_router(experiments.router)
app.include_router(backends.router)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}