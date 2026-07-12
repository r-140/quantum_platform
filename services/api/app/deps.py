"""
FastAPI dependencies: a shared QuantumBackend instance and an in-memory
experiment store.

The in-memory store is a deliberate, temporary simplification -- it won't
survive a process restart and won't work correctly if the API is ever run
with multiple worker processes (each would have its own store). This is
exactly the gap Postgres (for experiment metadata) is meant to fill once
the storage layer is added; nothing here pretends otherwise.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone

from quantum_core.backends.aer_backend import AerBackend
from quantum_core.backends.base import QuantumBackend

from app.schemas.experiments import ExperimentResponse

_backend: QuantumBackend | None = None


def get_backend() -> QuantumBackend:
    """A single shared AerBackend instance for the process lifetime.

    Kept as a plain module-level singleton rather than FastAPI's
    `lifespan`-managed state for now, to keep this first version simple --
    revisit if/when the API needs to support multiple backend types
    selectable per request (mock vs. Aer vs., eventually, real hardware).
    """
    global _backend
    if _backend is None:
        _backend = AerBackend()
    return _backend


class ExperimentStore:
    """Thread-safe in-memory store, keyed by experiment id.

    Thread safety matters here specifically because of VQE: its endpoint
    runs in a threadpool worker thread (see routers/experiments.py), not
    on the main event loop thread, so store access from that path is
    genuinely concurrent with the main thread, unlike the async endpoints.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: dict[str, ExperimentResponse] = {}

    def save(self, experiment: ExperimentResponse) -> None:
        with self._lock:
            self._data[experiment.id] = experiment

    def get(self, experiment_id: str) -> ExperimentResponse | None:
        with self._lock:
            return self._data.get(experiment_id)

    def list_all(self) -> list[ExperimentResponse]:
        with self._lock:
            return list(self._data.values())


_store = ExperimentStore()


def get_store() -> ExperimentStore:
    return _store


def utcnow() -> datetime:
    return datetime.now(timezone.utc)