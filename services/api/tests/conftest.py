"""
Shared fixtures for the API test suite.

Uses FastAPI's `app.dependency_overrides` to swap in a fresh
`InMemoryExperimentStore` per test, rather than monkeypatching a module-level
singleton (the approach used before the storage abstraction existed) --
this is the idiomatic FastAPI pattern for swapping out a `Depends(...)`
dependency in tests, and it means the test suite never touches
`app.deps.get_store`'s real branching logic (Postgres vs. in-memory) at all.

Note on qiskit: importing `app.main` here does NOT require qiskit/qiskit-aer
to be importable. That wasn't always true -- an earlier version of this
service had a `routers/experiments.py` that called straight into
`quantum_core.algorithms.*` to execute circuits in-process. Since execution
moved to the orchestrator (see docs/architecture/orchestration.md), nothing
on the API's import path touches qiskit at module level; `app.deps.get_backend`
lazily imports `AerBackend` only if called, and nothing in this test suite
calls it.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.deps import get_store
from app.main import app
from app.store.in_memory import InMemoryExperimentStore


@pytest.fixture
def store() -> InMemoryExperimentStore:
    return InMemoryExperimentStore()


@pytest.fixture(autouse=True)
def override_store(store: InMemoryExperimentStore):
    """Overrides the `get_store` dependency for every test in this suite,
    so each test gets its own fresh, empty store -- without this, experiments
    saved by one test would leak into the next (FastAPI's dependency
    overrides are otherwise shared across the whole `app` object, same as
    the store singleton this replaced).
    """
    app.dependency_overrides[get_store] = lambda: store
    yield
    app.dependency_overrides.clear()


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)