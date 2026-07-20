"""
In-memory ExperimentStore implementation. Used as the default when
DATABASE_URL isn't configured, and in tests (via FastAPI's
`app.dependency_overrides`, see tests/conftest.py) -- no external
dependency, no setup required.

Doesn't survive a process restart, and won't behave correctly if the API
is ever run with multiple worker processes (each gets its own instance) --
exactly the gap `PostgresExperimentStore` closes.
"""

from __future__ import annotations

import asyncio

from app.schemas.experiments import ExperimentResponse
from app.store.base import ExperimentStore


class InMemoryExperimentStore(ExperimentStore):
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._data: dict[str, ExperimentResponse] = {}

    async def save(self, experiment: ExperimentResponse) -> None:
        async with self._lock:
            self._data[experiment.id] = experiment

    async def get(self, experiment_id: str) -> ExperimentResponse | None:
        async with self._lock:
            return self._data.get(experiment_id)

    async def list_all(self) -> list[ExperimentResponse]:
        async with self._lock:
            return sorted(self._data.values(), key=lambda e: e.submitted_at)
