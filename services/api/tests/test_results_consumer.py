"""
Tests for `app.main.apply_result_message` -- the store-update logic behind
the results-queue consumer, tested directly without any aio-pika involvement
(constructing a real queue/message for this would be an integration test,
not a unit test; see docs/testing.md).
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.main import apply_result_message
from app.schemas.experiments import ExperimentResponse, ExperimentStatus
from app.store.in_memory import InMemoryExperimentStore
from quantum_core.tasks import ExperimentResultMessage


def _queued_experiment(experiment_id: str) -> ExperimentResponse:
    return ExperimentResponse(
        id=experiment_id,
        algorithm="grover",
        status=ExperimentStatus.QUEUED,
        submitted_at=datetime.now(timezone.utc),
    )


async def test_completed_result_updates_store() -> None:
    store = InMemoryExperimentStore()
    await store.save(_queued_experiment("abc"))

    result_msg = ExperimentResultMessage(
        experiment_id="abc", status="completed", result={"counts": {"101": 970}}
    )
    await apply_result_message(result_msg, store)

    updated = await store.get("abc")
    assert updated.status == ExperimentStatus.COMPLETED
    assert updated.result == {"counts": {"101": 970}}
    assert updated.error is None
    assert updated.completed_at is not None


async def test_failed_result_updates_store() -> None:
    store = InMemoryExperimentStore()
    await store.save(_queued_experiment("abc"))

    result_msg = ExperimentResultMessage(experiment_id="abc", status="failed", error="circuit error")
    await apply_result_message(result_msg, store)

    updated = await store.get("abc")
    assert updated.status == ExperimentStatus.FAILED
    assert updated.error == "circuit error"
    assert updated.result is None


async def test_unknown_experiment_id_is_ignored_not_raised() -> None:
    """A result for an id the store doesn't know about (e.g. from an
    experiment submitted to a previous API process instance while running
    with the in-memory store fallback) should be silently ignored -- not
    raise, which would crash the whole results-consumer loop over one stale
    message.
    """
    store = InMemoryExperimentStore()

    result_msg = ExperimentResultMessage(experiment_id="does-not-exist", status="completed", result={})

    await apply_result_message(result_msg, store)  # should not raise

    assert await store.get("does-not-exist") is None


async def test_original_submitted_at_is_preserved() -> None:
    store = InMemoryExperimentStore()
    original = _queued_experiment("abc")
    await store.save(original)

    await apply_result_message(
        ExperimentResultMessage(experiment_id="abc", status="completed", result={}), store
    )

    updated = await store.get("abc")
    assert updated.submitted_at == original.submitted_at