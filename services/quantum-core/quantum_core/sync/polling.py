"""
Synchronization mechanism for the hardware/software interaction loop.

Real quantum hardware is queued, slow, and occasionally flaky. This module
implements the policy for *how* software waits on hardware: adaptive
(exponential backoff) polling, a circuit breaker to stop hammering a backend
that is clearly unhealthy, and cooperative cancellation so a caller can
abandon a wait without leaking background tasks.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from quantum_core.backends.base import (
    ExperimentResult,
    JobHandle,
    JobStatus,
    QuantumBackend,
    TransientBackendError,
)

logger = logging.getLogger(__name__)


class CancellationToken:
    """Cooperative cancellation, checked by the polling loop between
    iterations. More explicit and easier to test than relying solely on
    asyncio task cancellation.
    """

    def __init__(self) -> None:
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    @property
    def cancelled(self) -> bool:
        return self._cancelled


@dataclass
class PollingConfig:
    initial_interval_s: float = 0.25
    max_interval_s: float = 5.0
    backoff_factor: float = 1.8
    timeout_s: float = 30.0
    max_retries_on_transient_error: int = 3


class CircuitBreaker:
    """Stops the loop from hammering a backend that is failing repeatedly.

    Trips open after `failure_threshold` consecutive failures; stays open
    for `reset_after_s` before allowing a single trial call through
    (half-open).
    """

    def __init__(self, failure_threshold: int = 5, reset_after_s: float = 15.0) -> None:
        self._failure_threshold = failure_threshold
        self._reset_after_s = reset_after_s
        self._consecutive_failures = 0
        self._opened_at: float | None = None

    def record_success(self) -> None:
        self._consecutive_failures = 0
        self._opened_at = None

    def record_failure(self) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= self._failure_threshold:
            self._opened_at = time.monotonic()

    @property
    def is_open(self) -> bool:
        if self._opened_at is None:
            return False
        if time.monotonic() - self._opened_at >= self._reset_after_s:
            # half-open: allow one attempt through to test recovery
            self._opened_at = None
            self._consecutive_failures = self._failure_threshold - 1
            return False
        return True


class BackendUnavailableError(Exception):
    """Raised when the circuit breaker is open."""


class PollingTimeoutError(Exception):
    """Raised when a job doesn't finish within `PollingConfig.timeout_s`."""


async def wait_for_result(
    backend: QuantumBackend,
    handle: JobHandle,
    *,
    config: PollingConfig | None = None,
    breaker: CircuitBreaker | None = None,
    cancellation: CancellationToken | None = None,
) -> ExperimentResult:
    """Poll `backend` until the job completes, fails permanently, is
    cancelled, or times out. This is the core hardware/software
    synchronization primitive used by every higher-level workflow
    (orchestrator tasks, VQE feedback loop, etc.).
    """
    config = config or PollingConfig()
    breaker = breaker or CircuitBreaker()
    cancellation = cancellation or CancellationToken()

    if breaker.is_open:
        raise BackendUnavailableError(f"circuit breaker open for backend={backend.name}")

    start = time.monotonic()
    interval = config.initial_interval_s
    transient_retries = 0

    while True:
        if cancellation.cancelled:
            await backend.cancel(handle)
            raise asyncio.CancelledError(f"job {handle.job_id} cancelled by caller")

        if time.monotonic() - start > config.timeout_s:
            await backend.cancel(handle)
            raise PollingTimeoutError(
                f"job {handle.job_id} did not finish within {config.timeout_s}s"
            )

        try:
            status = await backend.poll_status(handle)
        except Exception:
            breaker.record_failure()
            raise

        if status == JobStatus.COMPLETED:
            try:
                result = await backend.fetch_result(handle)
            except TransientBackendError as exc:
                transient_retries += 1
                if transient_retries > config.max_retries_on_transient_error:
                    breaker.record_failure()
                    raise
                logger.warning("transient error fetching result, retrying: %s", exc)
                await asyncio.sleep(interval)
                interval = min(interval * config.backoff_factor, config.max_interval_s)
                continue
            breaker.record_success()
            return result

        if status == JobStatus.FAILED:
            try:
                result = await backend.fetch_result(handle)
                breaker.record_success()  # backend responded correctly, just a bad run
                return result
            except TransientBackendError as exc:
                transient_retries += 1
                if transient_retries > config.max_retries_on_transient_error:
                    breaker.record_failure()
                    raise
                logger.warning(
                    "transient failure (%s), retry %d/%d",
                    exc,
                    transient_retries,
                    config.max_retries_on_transient_error,
                )
                await asyncio.sleep(interval)
                interval = min(interval * config.backoff_factor, config.max_interval_s)
                continue

        if status == JobStatus.CANCELLED:
            raise asyncio.CancelledError(f"job {handle.job_id} was cancelled")

        # still QUEUED or RUNNING -- wait and back off
        await asyncio.sleep(interval)
        interval = min(interval * config.backoff_factor, config.max_interval_s)
