"""
Direct tests for `InMemoryExperimentStore` -- no HTTP layer, no FastAPI app
import needed (though `conftest.py`'s autouse fixture will still import it;
these tests just don't use `client`).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from app.schemas.experiments import ExperimentResponse, ExperimentStatus
from app.store.in_memory import InMemoryExperimentStore


def _make_response(
    experiment_id: str,
    *,
    algorithm: str = "grover",
    status: ExperimentStatus = ExperimentStatus.COMPLETED,
    submitted_at: datetime | None = None,
) -> ExperimentResponse:
    return ExperimentResponse(
        id=experiment_id,
        algorithm=algorithm,
        status=status,
        submitted_at=submitted_at or datetime.now(timezone.utc),
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


async def test_list_all_sorts_newest_first_by_default() -> None:
    store = InMemoryExperimentStore()
    t0 = datetime.now(timezone.utc)
    await store.save(_make_response("older", submitted_at=t0))
    await store.save(_make_response("newer", submitted_at=t0 + timedelta(seconds=10)))

    result = await store.list_all()

    assert [e.id for e in result] == ["newer", "older"]


async def test_list_all_sort_desc_false_returns_oldest_first() -> None:
    store = InMemoryExperimentStore()
    t0 = datetime.now(timezone.utc)
    await store.save(_make_response("older", submitted_at=t0))
    await store.save(_make_response("newer", submitted_at=t0 + timedelta(seconds=10)))

    result = await store.list_all(sort_desc=False)

    assert [e.id for e in result] == ["older", "newer"]


async def test_list_all_filters_by_algorithm() -> None:
    store = InMemoryExperimentStore()
    await store.save(_make_response("a", algorithm="grover"))
    await store.save(_make_response("b", algorithm="vqe"))

    result = await store.list_all(algorithm="vqe")

    assert [e.id for e in result] == ["b"]


async def test_list_all_filters_by_status() -> None:
    store = InMemoryExperimentStore()
    await store.save(_make_response("a", status=ExperimentStatus.QUEUED))
    await store.save(_make_response("b", status=ExperimentStatus.COMPLETED))

    result = await store.list_all(status=ExperimentStatus.COMPLETED)

    assert [e.id for e in result] == ["b"]


async def test_list_all_combines_algorithm_and_status_filters() -> None:
    store = InMemoryExperimentStore()
    await store.save(_make_response("a", algorithm="grover", status=ExperimentStatus.COMPLETED))
    await store.save(_make_response("b", algorithm="grover", status=ExperimentStatus.FAILED))
    await store.save(_make_response("c", algorithm="vqe", status=ExperimentStatus.COMPLETED))

    result = await store.list_all(algorithm="grover", status=ExperimentStatus.COMPLETED)

    assert [e.id for e in result] == ["a"]


async def test_stats_groups_by_algorithm_and_status() -> None:
    store = InMemoryExperimentStore()
    await store.save(_make_response("a", algorithm="grover", status=ExperimentStatus.COMPLETED))
    await store.save(_make_response("b", algorithm="grover", status=ExperimentStatus.COMPLETED))
    await store.save(_make_response("c", algorithm="grover", status=ExperimentStatus.FAILED))
    await store.save(_make_response("d", algorithm="vqe", status=ExperimentStatus.QUEUED))

    stats = await store.stats()

    assert stats == [
        {"algorithm": "grover", "status": "completed", "count": 2},
        {"algorithm": "grover", "status": "failed", "count": 1},
        {"algorithm": "vqe", "status": "queued", "count": 1},
    ]


async def test_stats_on_empty_store_returns_empty_list() -> None:
    store = InMemoryExperimentStore()
    assert await store.stats() == []


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