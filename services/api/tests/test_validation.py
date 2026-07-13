"""
Validation edge cases for the request schemas -- exercised through the
HTTP layer (POST /experiments) rather than by constructing Pydantic models
directly, so these tests also confirm FastAPI surfaces validation errors as
422 responses rather than raw exceptions, and that the discriminated union
in `ExperimentRequest` correctly rejects malformed/mismatched payloads.

None of these hit `app.execution` -- validation happens before the router
body even runs, so there's nothing to monkeypatch here.
"""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_missing_required_field_returns_422(client: TestClient) -> None:
    response = client.post("/experiments", json={"algorithm": "grover"})  # missing marked_states

    assert response.status_code == 422


def test_unknown_algorithm_returns_422(client: TestClient) -> None:
    response = client.post("/experiments", json={"algorithm": "not_a_real_algorithm"})

    assert response.status_code == 422


def test_empty_marked_states_returns_422(client: TestClient) -> None:
    response = client.post("/experiments", json={"algorithm": "grover", "marked_states": []})

    assert response.status_code == 422


def test_shots_below_minimum_returns_422(client: TestClient) -> None:
    response = client.post(
        "/experiments",
        json={"algorithm": "grover", "marked_states": ["101"], "shots": 0},
    )

    assert response.status_code == 422


def test_shots_above_maximum_returns_422(client: TestClient) -> None:
    response = client.post(
        "/experiments",
        json={"algorithm": "grover", "marked_states": ["101"], "shots": 1_000_000},
    )

    assert response.status_code == 422


def test_qpe_phi_out_of_range_returns_422(client: TestClient) -> None:
    # QPERequest requires 0.0 <= phi < 1.0
    response = client.post("/experiments", json={"algorithm": "qpe", "phi": 1.5})

    assert response.status_code == 422


def test_qpe_phi_at_lower_bound_is_valid(client: TestClient, monkeypatch) -> None:
    from app import execution

    async def fake_run_qpe(backend, request):
        return {"true_phi": request.phi}

    monkeypatch.setattr(execution, "run_qpe", fake_run_qpe)

    response = client.post("/experiments", json={"algorithm": "qpe", "phi": 0.0})

    assert response.status_code == 200


def test_sat_grover_too_many_variables_returns_422(client: TestClient) -> None:
    # SatGroverRequest caps `variables` at 8 (keeps circuits small for this demo)
    variables = [f"x{i}" for i in range(9)]
    response = client.post(
        "/experiments",
        json={"algorithm": "sat_grover", "variables": variables, "expression": "x0"},
    )

    assert response.status_code == 422


def test_vqe_max_iterations_below_minimum_returns_422(client: TestClient) -> None:
    response = client.post("/experiments", json={"algorithm": "vqe", "max_iterations": 0})

    assert response.status_code == 422