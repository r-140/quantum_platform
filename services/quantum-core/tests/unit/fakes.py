"""
Test doubles for exercising `quantum_core.sync.polling` without a real
backend. Hand-written rather than `unittest.mock.AsyncMock` -- explicit
fakes are easier to read in a portfolio project and make the exact
scripted behavior (which status appears when, what fetch_result does)
visible at the call site of each test, rather than buried in mock
configuration.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from quantum_core.backends.base import (
    Circuit,
    ExperimentResult,
    JobHandle,
    JobStatus,
    QuantumBackend,
)


class ScriptedBackend(QuantumBackend):
    """Replays a fixed sequence of statuses from `poll_status()` (holding
    the last one once exhausted), and delegates `fetch_result()` to a
    caller-supplied async function -- so tests can make it raise
    `TransientBackendError`, return a result, or fail on the Nth call.
    """

    name = "scripted"

    def __init__(
        self,
        statuses: list[JobStatus],
        fetch_result_fn: Callable[[], Awaitable[ExperimentResult]],
    ) -> None:
        self._statuses = list(statuses)
        self._idx = 0
        self._fetch_result_fn = fetch_result_fn
        self.cancel_called = False
        self.poll_call_count = 0

    async def submit(self, circuit: Circuit) -> JobHandle:
        return JobHandle.new(self.name)

    async def poll_status(self, handle: JobHandle) -> JobStatus:
        self.poll_call_count += 1
        idx = min(self._idx, len(self._statuses) - 1)
        status = self._statuses[idx]
        self._idx += 1
        return status

    async def fetch_result(self, handle: JobHandle) -> ExperimentResult:
        return await self._fetch_result_fn()

    async def cancel(self, handle: JobHandle) -> None:
        self.cancel_called = True


async def ok_result(
    job_id: str = "test-job", counts: dict[str, int] | None = None
) -> ExperimentResult:
    """Default success `fetch_result_fn` for tests that don't care about
    the specific counts.
    """
    return ExperimentResult(
        job_id=job_id, status=JobStatus.COMPLETED, counts=counts or {"00": 100}
    )