"""
Shared fixtures for the API test suite.

Importing `app.main` (needed for TestClient) pulls in the full router ->
execution -> quantum_core.algorithms chain, which requires qiskit/qiskit-aer
to be installed -- even though the tests here monkeypatch execution
functions to avoid ever *running* a real circuit. That's an acceptable
trade-off: qiskit is already a mandatory dependency of this service (see
requirements.txt), so requiring it to be importable for the test suite
doesn't add a new constraint, and the alternative (lazily importing inside
every execution.py function) would meaningfully hurt readability of the
actual business logic for a benefit that only matters for test isolation.
Contrast this with `app.deps.get_backend`, where the lazy-import fix *was*
worth it -- see that module's docstring.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import app.deps as deps_module
from app.deps import ExperimentStore
from app.main import app


@pytest.fixture(autouse=True)
def fresh_store(monkeypatch: pytest.MonkeyPatch) -> ExperimentStore:
    """Replaces the module-level `_store` singleton with a fresh, empty
    instance for every test. Without this, experiments saved by one test
    would leak into the next (the store is a plain module global, shared
    across the whole process) -- `test_list_experiments` would then have a
    count that depends on what ran before it, which is exactly the kind of
    order-dependent flakiness a test suite shouldn't have.
    """
    fresh = ExperimentStore()
    monkeypatch.setattr(deps_module, "_store", fresh)
    return fresh


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)