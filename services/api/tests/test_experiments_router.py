"""
Unit tests for the experiments router, via FastAPI's TestClient.

`app.execution`'s functions (run_grover, run_sat_grover, run_qpe,
run_vqe_sync) are monkeypatched to return canned results instantly. These
tests exercise the HTTP layer -- request validation, dispatch to the right
execution function, status codes, error handling, and the threadpool
offload for VQE -- in isolation from real quantum computation, which is
already covered by quantum_core's own demos and (for polling/backends)
unit tests. Monkeypatching `app.execution.run_grover` etc. works correctly
here specifically because routers/experiments.py calls these as
`execution.run_grover(...)` (an attribute lookup on the module object at
call time), not via a `from app.execution import run_grover` binding
captured at import time -- patching the module attribute is visible there.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import execution


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


def test_grover_dispatch(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    received_requests = []

    async def fake_run_grover(backend, request):
        received_requests.append(request)
        return {"counts": {"101": 1000}}

    monkeypatch.setattr(execution, "run_grover", fake_run_grover)

    response = client.post("/experiments", json={"algorithm": "grover", "marked_states": ["101"]})

    assert response.status_code == 200
    body = response.json()
    assert body["algorithm"] == "grover"
    assert body["status"] == "completed"
    assert body["result"] == {"counts": {"101": 1000}}
    assert body["error"] is None
    assert len(received_requests) == 1
    assert received_requests[0].marked_states == ["101"]


def test_sat_grover_dispatch(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_run_sat_grover(backend, request):
        return {"solutions": ["0110"], "expression": request.expression}

    monkeypatch.setattr(execution, "run_sat_grover", fake_run_sat_grover)

    response = client.post(
        "/experiments",
        json={"algorithm": "sat_grover", "variables": ["x0", "x1"], "expression": "x0 & x1"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["algorithm"] == "sat_grover"
    assert body["result"] == {"solutions": ["0110"], "expression": "x0 & x1"}


def test_qpe_dispatch(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_run_qpe(backend, request):
        return {"true_phi": request.phi, "resolution": 1 / (2**request.num_counting_qubits)}

    monkeypatch.setattr(execution, "run_qpe", fake_run_qpe)

    response = client.post("/experiments", json={"algorithm": "qpe", "phi": 0.625})

    assert response.status_code == 200
    body = response.json()
    assert body["algorithm"] == "qpe"
    assert body["result"]["true_phi"] == 0.625


def test_vqe_dispatch_via_threadpool(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """`run_vqe_sync` is a plain synchronous function -- the router must
    call it through `run_in_threadpool`, never `await` it directly (direct
    await would try to await a plain dict/value, not a coroutine, and
    FastAPI would raise immediately). This test's fake is intentionally
    also a plain sync `def`, matching the real function's signature: if the
    router regressed to something incompatible with `run_in_threadpool`
    (e.g. trying to `await execution.run_vqe_sync(...)` directly), this
    would surface as a clear failure here rather than passing silently.
    """
    received_requests = []

    def fake_run_vqe_sync(backend, request):
        received_requests.append(request)
        return {"total_energy": -1.14, "iterations_run": 5}

    monkeypatch.setattr(execution, "run_vqe_sync", fake_run_vqe_sync)

    response = client.post("/experiments", json={"algorithm": "vqe"})

    assert response.status_code == 200
    body = response.json()
    assert body["algorithm"] == "vqe"
    assert body["result"] == {"total_energy": -1.14, "iterations_run": 5}
    assert len(received_requests) == 1


def test_execution_error_becomes_failed_not_500(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def failing_run_grover(backend, request):
        raise RuntimeError("simulator exploded")

    monkeypatch.setattr(execution, "run_grover", failing_run_grover)

    response = client.post("/experiments", json={"algorithm": "grover", "marked_states": ["101"]})

    # A backend/circuit error becomes a FAILED experiment record, not a
    # raw 500 -- callers can always expect a well-formed ExperimentResponse
    # from this endpoint. See routers/experiments.py's broad except clause.
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "failed"
    assert body["error"] == "simulator exploded"
    assert body["result"] is None


def test_get_experiment_by_id(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_run_grover(backend, request):
        return {"counts": {"101": 1000}}

    monkeypatch.setattr(execution, "run_grover", fake_run_grover)

    submit_response = client.post(
        "/experiments", json={"algorithm": "grover", "marked_states": ["101"]}
    )
    experiment_id = submit_response.json()["id"]

    get_response = client.get(f"/experiments/{experiment_id}")

    assert get_response.status_code == 200
    assert get_response.json()["id"] == experiment_id


def test_get_experiment_not_found(client: TestClient) -> None:
    response = client.get("/experiments/does-not-exist")

    assert response.status_code == 404


def test_list_experiments(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_run_grover(backend, request):
        return {"counts": {}}

    monkeypatch.setattr(execution, "run_grover", fake_run_grover)

    client.post("/experiments", json={"algorithm": "grover", "marked_states": ["101"]})
    client.post("/experiments", json={"algorithm": "grover", "marked_states": ["110"]})

    response = client.get("/experiments")

    assert response.status_code == 200
    assert len(response.json()) == 2