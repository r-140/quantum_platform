"""
Direct tests for `InMemoryExperimentStore` -- no HTTP layer, no FastAPI app
import needed (though `conftest.py`'s autouse fixture will still import it;
these tests just don't use `client`).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from app.schemas.experiments import ExperimentResponse, ExperimentStatus
from app.store.in_memory import InMemoryExperimentStore


def _make_response(experiment_id: str) -> ExperimentResponse:
    return ExperimentResponse(
        id=experiment_id,
        algorithm="grover",
        status=ExperimentStatus.COMPLETED,
        submitted_at=datetime.now(timezone.utc),
    )


async def test_save_and_get() -> None:
    store = InMemoryExperimentStore()
    response = _make_response("abc")

    await store.save(response)

    assert await store.get("abc") == response


async def test_get_missing_returns_none() -> None:
    store = InMemoryExperimentStore()
    assert await store.get("does-not-exist") is None


async def test_list_all() -> None:
    store = InMemoryExperimentStore()
    await store.save(_make_response("a"))
    await store.save(_make_response("b"))

    ids = {experiment.id for experiment in await store.list_all()}

    assert ids == {"a", "b"}


async def test_save_overwrites_existing_id() -> None:
    store = InMemoryExperimentStore()
    await store.save(_make_response("abc"))
    updated = _make_response("abc")

    await store.save(updated)

    assert await store.get("abc") == updated
    assert len(await store.list_all()) == 1


async def test_concurrent_saves_do_not_lose_writes() -> None:
    """Hammers the store from many concurrent coroutines at once. Uses
    `asyncio.gather` (concurrent tasks on the same event loop), not threads
    -- consistent with the store now being asyncio.Lock-based rather than
    threading.Lock-based (see in_memory.py: nothing writes to this store
    from a different OS thread anymore, now that VQE execution moved
    entirely to the orchestrator and no longer needs `run_in_threadpool`
    on the API side). A missing/incorrect lock would show up here as lost
    writes (final count < n).
    """
    store = InMemoryExperimentStore()
    n = 200

    await asyncio.gather(*(store.save(_make_response(str(i))) for i in range(n)))

    assert len(await store.list_all()) == n