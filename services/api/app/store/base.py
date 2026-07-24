"""
Storage abstraction boundary for experiment metadata -- mirrors
`quantum_core.backends.base.QuantumBackend` (submit/poll/fetch abstracted
from any specific simulator/hardware) in spirit: `ExperimentStore`
abstracts "persist and retrieve an experiment record" from any specific
storage technology.

Two implementations: `InMemoryExperimentStore` (in `in_memory.py`, used by
default and in tests -- no external dependency, doesn't survive a
restart) and `PostgresExperimentStore` (in `postgres.py`, real
persistence). Routers depend on this abstract type via FastAPI's
`Depends(get_store)`, never on a concrete implementation directly -- see
app/deps.py.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from app.schemas.experiments import ExperimentResponse


class ExperimentStore(ABC):
    @abstractmethod
    async def save(self, experiment: ExperimentResponse) -> None:
        """Insert a new experiment record, or update it if `experiment.id`
        already exists (upsert semantics) -- callers rely on this to both
        create a QUEUED record and later update it to
        COMPLETED/FAILED without needing separate insert/update methods.
        """

    @abstractmethod
    async def get(self, experiment_id: str) -> ExperimentResponse | None:
        """Returns None if no experiment with this id exists -- callers
        should not need to catch an exception for the "not found" case.
        """

    @abstractmethod
    async def list_all(
        self,
        *,
        algorithm: str | None = None,
        status: str | None = None,
        sort_desc: bool = True,
    ) -> list[ExperimentResponse]:
        """Returns known experiments, optionally filtered by exact
        `algorithm`/`status` match, sorted by `submitted_at` (newest first
        by default -- `sort_desc=False` for oldest first). Filtering is
        pushed down to the storage layer (SQL WHERE for Postgres, a plain
        loop for in-memory) rather than left to callers to filter a full
        list themselves -- the dashboard frontend relies on this to avoid
        pulling every experiment over the wire just to show one algorithm.
        """

    @abstractmethod
    async def stats(self) -> list[dict[str, str | int]]:
        """Returns per (algorithm, status) counts, e.g.
        `[{"algorithm": "grover", "status": "completed", "count": 12}, ...]`
        -- powers the dashboard's summary header without requiring the
        frontend to fetch and locally tally the full experiment list.
        """