"""
Direct tests for `ExperimentStore` -- no HTTP layer, no FastAPI app import.

Thanks to `app.deps.get_backend`'s lazy import (see that function's
docstring), importing `app.deps` here does NOT require qiskit/qiskit-aer to
be installed -- only pydantic (for `ExperimentResponse`, which the store
holds). If this file starts failing to import with a qiskit-related error,
that's a sign the lazy-import discipline in deps.py has regressed.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone

from app.deps import ExperimentStore
from app.schemas.experiments import ExperimentResponse, ExperimentStatus


def _make_response(experiment_id: str) -> ExperimentResponse:
    return ExperimentResponse(
        id=experiment_id,
        algorithm="grover",
        status=ExperimentStatus.COMPLETED,
        submitted_at=datetime.now(timezone.utc),
    )


def test_save_and_get() -> None:
    store = ExperimentStore()
    response = _make_response("abc")

    store.save(response)

    assert store.get("abc") == response


def test_get_missing_returns_none() -> None:
    store = ExperimentStore()
    assert store.get("does-not-exist") is None


def test_list_all() -> None:
    store = ExperimentStore()
    store.save(_make_response("a"))
    store.save(_make_response("b"))

    ids = {experiment.id for experiment in store.list_all()}

    assert ids == {"a", "b"}


def test_save_overwrites_existing_id() -> None:
    store = ExperimentStore()
    store.save(_make_response("abc"))
    updated = _make_response("abc")

    store.save(updated)

    assert store.get("abc") == updated
    assert len(store.list_all()) == 1


def test_concurrent_saves_are_thread_safe() -> None:
    """Hammers the store from many threads at once. This specifically
    matters because the VQE endpoint writes to the store from a threadpool
    worker thread, not the main event-loop thread (see
    routers/experiments.py and its use of `run_in_threadpool`) -- so store
    access is genuinely concurrent in a way it wouldn't be for a purely
    async-only service. A missing or incorrect lock would show up here as
    lost writes (final count < n), not as an obvious crash.
    """
    store = ExperimentStore()
    n = 200

    def worker(i: int) -> None:
        store.save(_make_response(str(i)))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(store.list_all()) == n