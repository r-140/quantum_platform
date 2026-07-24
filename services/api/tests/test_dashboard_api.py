"""
Tests for the dashboard-facing additions to the experiments router:
GET /experiments query params (algorithm/status/sort filters) and the new
GET /experiments/stats aggregate endpoint. Both go through the `store`
fixture from conftest.py directly (pre-populated via `_seed`, then queried
via the real HTTP client) -- neither of these endpoints touches
`publish_task`, so there's nothing to monkeypatch here unlike
test_experiments_router.py's POST-focused tests.

Test functions here are `async def`, not plain `def` -- deliberately, to
avoid depending on unconfirmed behavior around async fixtures being
resolved for plain sync test functions (a genuinely ambiguous area across
pytest-asyncio versions/modes that wasn't worth risking; see
docs/testing.md). Seeding the store is a plain async helper function
called explicitly via `await` inside each test body, not a fixture --
sidesteps the question entirely. `client.get(...)` remains a normal
(synchronous) call inside these async test bodies -- FastAPI's TestClient
wraps a sync httpx.Client, calling it doesn't need `await` regardless of
whether the enclosing test function is async.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.schemas.experiments import ExperimentResponse, ExperimentStatus
from app.store.in_memory import InMemoryExperimentStore


def _make_response(
    experiment_id: str,
    *,
    algorithm: str = "grover",
    status: ExperimentStatus = ExperimentStatus.COMPLETED,
    submitted_at: datetime,
) -> ExperimentResponse:
    return ExperimentResponse(
        id=experiment_id, algorithm=algorithm, status=status, submitted_at=submitted_at
    )


async def _seed(store: InMemoryExperimentStore) -> None:
    t0 = datetime.now(timezone.utc)
    await store.save(
        _make_response("a", algorithm="grover", status=ExperimentStatus.COMPLETED, submitted_at=t0)
    )
    await store.save(
        _make_response(
            "b",
            algorithm="vqe",
            status=ExperimentStatus.QUEUED,
            submitted_at=t0 + timedelta(seconds=10),
        )
    )
    await store.save(
        _make_response(
            "c",
            algorithm="grover",
            status=ExperimentStatus.FAILED,
            submitted_at=t0 + timedelta(seconds=20),
        )
    )


async def test_list_experiments_default_sort_is_newest_first(
    client: TestClient, store: InMemoryExperimentStore
) -> None:
    await _seed(store)

    response = client.get("/experiments")

    assert response.status_code == 200
    ids = [e["id"] for e in response.json()]
    assert ids == ["c", "b", "a"]


async def test_list_experiments_sort_asc(client: TestClient, store: InMemoryExperimentStore) -> None:
    await _seed(store)

    response = client.get("/experiments?sort=asc")

    assert response.status_code == 200
    ids = [e["id"] for e in response.json()]
    assert ids == ["a", "b", "c"]


async def test_list_experiments_invalid_sort_returns_422(
    client: TestClient, store: InMemoryExperimentStore
) -> None:
    await _seed(store)

    response = client.get("/experiments?sort=sideways")

    assert response.status_code == 422


async def test_list_experiments_filters_by_algorithm(
    client: TestClient, store: InMemoryExperimentStore
) -> None:
    await _seed(store)

    response = client.get("/experiments?algorithm=vqe")

    assert response.status_code == 200
    ids = [e["id"] for e in response.json()]
    assert ids == ["b"]


async def test_list_experiments_filters_by_status(
    client: TestClient, store: InMemoryExperimentStore
) -> None:
    await _seed(store)

    response = client.get("/experiments?status=failed")

    assert response.status_code == 200
    ids = [e["id"] for e in response.json()]
    assert ids == ["c"]


async def test_list_experiments_combines_filters(
    client: TestClient, store: InMemoryExperimentStore
) -> None:
    await _seed(store)

    response = client.get("/experiments?algorithm=grover&status=completed")

    assert response.status_code == 200
    ids = [e["id"] for e in response.json()]
    assert ids == ["a"]


async def test_experiments_stats(client: TestClient, store: InMemoryExperimentStore) -> None:
    await _seed(store)

    response = client.get("/experiments/stats")

    assert response.status_code == 200
    assert response.json() == [
        {"algorithm": "grover", "status": "completed", "count": 1},
        {"algorithm": "grover", "status": "failed", "count": 1},
        {"algorithm": "vqe", "status": "queued", "count": 1},
    ]


async def test_stats_route_not_shadowed_by_experiment_id_route(
    client: TestClient, store: InMemoryExperimentStore
) -> None:
    """Regression guard for the route-ordering pitfall noted in
    routers/experiments.py: if GET /{experiment_id} were registered before
    GET /stats, this request would resolve to the former with
    experiment_id="stats" and 404 instead of returning aggregate stats.
    """
    await _seed(store)

    response = client.get("/experiments/stats")

    assert response.status_code == 200
    assert isinstance(response.json(), list)