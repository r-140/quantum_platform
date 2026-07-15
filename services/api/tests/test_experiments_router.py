"""
Unit tests for the experiments router, via FastAPI's TestClient.

POST /experiments now enqueues a task and returns immediately with
status=queued -- it no longer executes anything in-process (see
routers/experiments.py's docstring for why). These tests monkeypatch
`app.deps.publish_task` to a no-op/recording fake, so they never need a
real RabbitMQ connection -- they verify the API's side of the contract
(what gets published, what the immediate response looks like), not the
orchestrator's execution or the full round trip through a real broker
(that's an integration-level concern; see docs/testing.md).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import deps


def test_health(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_list_backends(client: TestClient) -> None:
    response = client.get("/backends")

    assert response.status_code == 200
    backends = response.json()
    assert len(backends) == 1
    assert backends[0]["name"] == "aer-simulator"


def test_grover_submit_enqueues_and_returns_queued(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    published_tasks = []

    async def fake_publish_task(task):
        published_tasks.append(task)

    monkeypatch.setattr(deps, "publish_task", fake_publish_task)

    response = client.post("/experiments", json={"algorithm": "grover", "marked_states": ["101"]})

    assert response.status_code == 202
    body = response.json()
    assert body["algorithm"] == "grover"
    assert body["status"] == "queued"
    assert body["result"] is None
    assert body["completed_at"] is None

    assert len(published_tasks) == 1
    task = published_tasks[0]
    assert task.experiment_id == body["id"]
    assert task.algorithm == "grover"
    assert task.params == {"marked_states": ["101"], "shots": 1024}


def test_sat_grover_submit_params_shape(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    published_tasks = []

    async def fake_publish_task(task):
        published_tasks.append(task)

    monkeypatch.setattr(deps, "publish_task", fake_publish_task)

    response = client.post(
        "/experiments",
        json={"algorithm": "sat_grover", "variables": ["x0", "x1"], "expression": "x0 & x1"},
    )

    assert response.status_code == 202
    assert published_tasks[0].params == {
        "variables": ["x0", "x1"],
        "expression": "x0 & x1",
        "shots": 1024,
    }


def test_vqe_submit_params_shape(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """VQE no longer needs any special handling on the API side (no
    threadpool offload) -- it's enqueued exactly like every other
    algorithm. That asymmetry now lives entirely in the orchestrator (see
    services/orchestrator/app/worker.py's use of run_in_executor).
    """
    published_tasks = []

    async def fake_publish_task(task):
        published_tasks.append(task)

    monkeypatch.setattr(deps, "publish_task", fake_publish_task)

    response = client.post("/experiments", json={"algorithm": "vqe"})

    assert response.status_code == 202
    assert response.json()["status"] == "queued"
    assert published_tasks[0].params == {"shots": 8192, "max_iterations": 80}


def test_enqueue_failure_becomes_failed_not_500(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def failing_publish_task(task):
        raise ConnectionError("RabbitMQ unreachable")

    monkeypatch.setattr(deps, "publish_task", failing_publish_task)

    response = client.post("/experiments", json={"algorithm": "grover", "marked_states": ["101"]})

    # Not a 500 -- if we can't even enqueue, that's captured as a FAILED
    # experiment record, consistent with how execution failures are
    # reported once an experiment *does* reach the orchestrator.
    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "failed"
    assert "RabbitMQ unreachable" in body["error"]


def test_get_experiment_by_id(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_publish_task(task):
        pass

    monkeypatch.setattr(deps, "publish_task", fake_publish_task)

    submit_response = client.post(
        "/experiments", json={"algorithm": "grover", "marked_states": ["101"]}
    )
    experiment_id = submit_response.json()["id"]

    get_response = client.get(f"/experiments/{experiment_id}")

    assert get_response.status_code == 200
    assert get_response.json()["id"] == experiment_id
    assert get_response.json()["status"] == "queued"


def test_get_experiment_not_found(client: TestClient) -> None:
    response = client.get("/experiments/does-not-exist")

    assert response.status_code == 404


def test_list_experiments(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_publish_task(task):
        pass

    monkeypatch.setattr(deps, "publish_task", fake_publish_task)

    client.post("/experiments", json={"algorithm": "grover", "marked_states": ["101"]})
    client.post("/experiments", json={"algorithm": "grover", "marked_states": ["110"]})

    response = client.get("/experiments")

    assert response.status_code == 200
    assert len(response.json()) == 2