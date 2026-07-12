"""
Simulated hardware backend that deliberately reproduces the inconvenient
properties of real quantum hardware: queueing delay, non-trivial run time,
and occasional transient failures (e.g. calibration drift, connection
hiccups). This exists so the synchronization/polling logic can be developed
and exercised without needing real QPU access.
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass

from .base import (
    Circuit,
    ExperimentResult,
    JobHandle,
    JobStatus,
    QuantumBackend,
    TransientBackendError,
)


@dataclass
class _JobRecord:
    circuit: Circuit
    status: JobStatus
    result: ExperimentResult | None = None


class MockHardwareBackend(QuantumBackend):
    """In-memory fake backend with randomized timing and failure injection.

    Parameters let tests/demos tune how "unreliable" the hardware is, which
    is exactly what you want when exercising retry/backoff logic.
    """

    name = "mock-hw"

    def __init__(
        self,
        *,
        min_queue_s: float = 0.3,
        max_queue_s: float = 1.5,
        min_run_s: float = 0.1,
        max_run_s: float = 0.6,
        transient_failure_rate: float = 0.2,
        hard_failure_rate: float = 0.03,
        seed: int | None = None,
    ) -> None:
        self._jobs: dict[str, _JobRecord] = {}
        self._min_queue_s = min_queue_s
        self._max_queue_s = max_queue_s
        self._min_run_s = min_run_s
        self._max_run_s = max_run_s
        self._transient_failure_rate = transient_failure_rate
        self._hard_failure_rate = hard_failure_rate
        self._rng = random.Random(seed)

    async def submit(self, circuit: Circuit) -> JobHandle:
        handle = JobHandle.new(self.name)
        self._jobs[handle.job_id] = _JobRecord(circuit=circuit, status=JobStatus.QUEUED)
        # Fire-and-forget task that "runs" the job in the background,
        # mirroring how a real backend processes asynchronously relative to
        # the caller.
        asyncio.create_task(self._run_job(handle.job_id))
        return handle

    async def _run_job(self, job_id: str) -> None:
        record = self._jobs[job_id]

        await asyncio.sleep(self._rng.uniform(self._min_queue_s, self._max_queue_s))
        if record.status == JobStatus.CANCELLED:
            return
        record.status = JobStatus.RUNNING

        await asyncio.sleep(self._rng.uniform(self._min_run_s, self._max_run_s))
        if record.status == JobStatus.CANCELLED:
            return

        roll = self._rng.random()
        if roll < self._hard_failure_rate:
            record.status = JobStatus.FAILED
            record.result = ExperimentResult(
                job_id=job_id,
                status=JobStatus.FAILED,
                error="hardware fault: qubit readout error (non-retryable)",
            )
            return

        if roll < self._hard_failure_rate + self._transient_failure_rate:
            record.status = JobStatus.FAILED
            record.result = ExperimentResult(
                job_id=job_id,
                status=JobStatus.FAILED,
                error="transient: calibration cycle in progress",
                metadata={"retryable": True},
            )
            return

        record.status = JobStatus.COMPLETED
        record.result = ExperimentResult(
            job_id=job_id,
            status=JobStatus.COMPLETED,
            counts=self._fake_counts(record.circuit),
            metadata={"backend": self.name, "shots": record.circuit.shots},
        )

    def _fake_counts(self, circuit: Circuit) -> dict[str, int]:
        """Produce a plausible-looking measurement histogram. Not physically
        meaningful -- purely so downstream code has something to consume
        while we're still at the infrastructure-plumbing stage.
        """
        n = circuit.num_qubits
        outcomes = sorted({format(self._rng.getrandbits(n), f"0{n}b") for _ in range(4)})
        shots_left = circuit.shots
        counts: dict[str, int] = {}
        for i, outcome in enumerate(outcomes):
            remaining_buckets = len(outcomes) - i
            share = shots_left // remaining_buckets
            counts[outcome] = share
            shots_left -= share
        return counts

    async def poll_status(self, handle: JobHandle) -> JobStatus:
        record = self._jobs.get(handle.job_id)
        if record is None:
            raise KeyError(f"unknown job_id={handle.job_id}")
        return record.status

    async def fetch_result(self, handle: JobHandle) -> ExperimentResult:
        record = self._jobs.get(handle.job_id)
        if record is None:
            raise KeyError(f"unknown job_id={handle.job_id}")
        if record.status not in (JobStatus.COMPLETED, JobStatus.FAILED):
            raise RuntimeError(
                f"job {handle.job_id} not finished yet (status={record.status})"
            )
        assert record.result is not None
        if record.result.status == JobStatus.FAILED and record.result.metadata.get("retryable"):
            raise TransientBackendError(record.result.error)
        return record.result

    async def cancel(self, handle: JobHandle) -> None:
        record = self._jobs.get(handle.job_id)
        if record and record.status in (JobStatus.QUEUED, JobStatus.RUNNING):
            record.status = JobStatus.CANCELLED
