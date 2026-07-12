"""
Unit tests for `quantum_core.sync.polling.wait_for_result` -- the core
hardware/software synchronization primitive used throughout this project.

Every scenario here was first exercised as a standalone asyncio script (no
pytest, no mocking framework) to confirm the `ScriptedBackend`/`fake_clock`
approach actually produced the expected behavior before being transcribed
into pytest form. That mattered because this environment has no network
access to install pytest and run these directly -- see docs/testing.md.
"""

from __future__ import annotations

import asyncio

import pytest

from quantum_core.backends.base import (
    ExperimentResult,
    JobHandle,
    JobStatus,
    TransientBackendError,
)
from quantum_core.sync.polling import (
    BackendUnavailableError,
    CancellationToken,
    CircuitBreaker,
    PollingConfig,
    PollingTimeoutError,
    wait_for_result,
)

from .fakes import ScriptedBackend, ok_result


async def test_immediate_success() -> None:
    backend = ScriptedBackend([JobStatus.COMPLETED], fetch_result_fn=ok_result)
    handle = JobHandle.new("scripted")

    result = await wait_for_result(backend, handle)

    assert result.status == JobStatus.COMPLETED
    assert result.counts == {"00": 100}


async def test_backoff_grows_and_stops_polling_once_completed(fake_clock) -> None:
    backend = ScriptedBackend(
        [JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.RUNNING, JobStatus.COMPLETED],
        fetch_result_fn=ok_result,
    )
    handle = JobHandle.new("scripted")
    config = PollingConfig(initial_interval_s=0.1, backoff_factor=2.0, timeout_s=10.0)

    result = await wait_for_result(backend, handle, config=config)

    assert result.status == JobStatus.COMPLETED
    assert backend.poll_call_count == 4
    # Three waits happened before the COMPLETED status was seen: 0.1, 0.2, 0.4
    assert fake_clock.time() == pytest.approx(0.7)


async def test_transient_error_retries_then_succeeds(fake_clock) -> None:
    calls = {"n": 0}

    async def flaky_fetch() -> ExperimentResult:
        calls["n"] += 1
        if calls["n"] <= 2:
            raise TransientBackendError("calibration in progress")
        return ExperimentResult(job_id="x", status=JobStatus.COMPLETED, counts={"11": 50})

    backend = ScriptedBackend([JobStatus.COMPLETED], fetch_result_fn=flaky_fetch)
    handle = JobHandle.new("scripted")
    config = PollingConfig(initial_interval_s=0.1, max_retries_on_transient_error=3)

    result = await wait_for_result(backend, handle, config=config)

    assert result.status == JobStatus.COMPLETED
    assert calls["n"] == 3  # 2 failures + 1 success


async def test_transient_error_exhausted_raises(fake_clock) -> None:
    async def always_transient() -> ExperimentResult:
        raise TransientBackendError("calibration in progress")

    backend = ScriptedBackend([JobStatus.COMPLETED], fetch_result_fn=always_transient)
    handle = JobHandle.new("scripted")
    config = PollingConfig(initial_interval_s=0.1, max_retries_on_transient_error=2)
    # High threshold so the breaker itself doesn't trip mid-test and mask
    # the exception we're actually checking for.
    breaker = CircuitBreaker(failure_threshold=100)

    with pytest.raises(TransientBackendError):
        await wait_for_result(backend, handle, config=config, breaker=breaker)


async def test_timeout_raises_and_cancels(fake_clock) -> None:
    backend = ScriptedBackend([JobStatus.QUEUED], fetch_result_fn=ok_result)  # never completes
    handle = JobHandle.new("scripted")
    config = PollingConfig(initial_interval_s=1.0, backoff_factor=1.0, timeout_s=5.0)

    with pytest.raises(PollingTimeoutError):
        await wait_for_result(backend, handle, config=config)

    assert backend.cancel_called, "timing out should cancel the job on the backend"


async def test_cancellation_token_raises_and_cancels() -> None:
    backend = ScriptedBackend(
        [JobStatus.QUEUED, JobStatus.QUEUED], fetch_result_fn=ok_result
    )
    handle = JobHandle.new("scripted")
    token = CancellationToken()
    token.cancel()

    with pytest.raises(asyncio.CancelledError):
        await wait_for_result(backend, handle, cancellation=token)

    assert backend.cancel_called


async def test_hard_failure_returns_normally_not_raised() -> None:
    """A FAILED job whose fetch_result succeeds (i.e. the backend correctly
    reported a non-retryable failure, like a hardware fault) should be
    returned as a normal FAILED ExperimentResult -- not raised as an
    exception. Only TransientBackendError triggers retry/raise behavior;
    the circuit breaker should record this as a *success* too, since the
    backend responded correctly.
    """

    async def hard_fail() -> ExperimentResult:
        return ExperimentResult(job_id="x", status=JobStatus.FAILED, error="readout error")

    backend = ScriptedBackend([JobStatus.FAILED], fetch_result_fn=hard_fail)
    handle = JobHandle.new("scripted")

    result = await wait_for_result(backend, handle)

    assert result.status == JobStatus.FAILED
    assert result.error == "readout error"


async def test_open_circuit_breaker_blocks_without_calling_backend() -> None:
    backend = ScriptedBackend([JobStatus.COMPLETED], fetch_result_fn=ok_result)
    handle = JobHandle.new("scripted")
    breaker = CircuitBreaker(failure_threshold=1)
    breaker.record_failure()  # opens immediately, threshold=1
    assert breaker.is_open

    with pytest.raises(BackendUnavailableError):
        await wait_for_result(backend, handle, breaker=breaker)

    assert backend.poll_call_count == 0, "an open breaker should short-circuit before any backend call"