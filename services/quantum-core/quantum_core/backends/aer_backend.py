"""
Backend implementation backed by Qiskit Aer (local simulator).

Unlike MockHardwareBackend, this runs actual quantum circuits -- but AerSimulator's
`run()` API is synchronous and can be non-trivially slow for larger circuits.
To keep this backend honest with the async `QuantumBackend` contract (and to
avoid blocking the event loop that the polling/orchestrator code relies on),
the actual simulation is offloaded to a thread pool via `run_in_executor` and
tracked the same way MockHardwareBackend tracks its fake jobs: submit()
returns immediately with a handle, and a background task advances the job
through QUEUED -> RUNNING -> COMPLETED/FAILED.

This is still "simulation", not real QPU hardware -- but it is the first
backend in this project that actually executes a quantum circuit rather than
faking a result.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from qiskit import QuantumCircuit, transpile
from qiskit_aer import AerSimulator

from .base import (
    Circuit,
    ExperimentResult,
    JobHandle,
    JobStatus,
    QuantumBackend,
)


@dataclass
class _JobRecord:
    circuit: Circuit
    status: JobStatus
    result: ExperimentResult | None = None


class AerBackend(QuantumBackend):
    """Local Qiskit Aer simulator, wrapped to satisfy the async job-based
    `QuantumBackend` contract.

    `circuit.payload` is expected to be a `qiskit.QuantumCircuit` that
    already includes measurements (e.g. via `.measure_all()`). This backend
    does not add measurements on the caller's behalf, since algorithms
    (Grover, QFT/QPE, VQE) have different needs around what/when to measure.
    """

    name = "aer-simulator"

    def __init__(self, *, method: str = "automatic", seed_simulator: int | None = None) -> None:
        self._sim = AerSimulator(method=method)
        self._seed_simulator = seed_simulator
        self._jobs: dict[str, _JobRecord] = {}

    async def submit(self, circuit: Circuit) -> JobHandle:
        if not isinstance(circuit.payload, QuantumCircuit):
            raise TypeError(
                "AerBackend requires circuit.payload to be a qiskit.QuantumCircuit "
                f"with measurements included, got {type(circuit.payload)!r}"
            )

        handle = JobHandle.new(self.name)
        self._jobs[handle.job_id] = _JobRecord(circuit=circuit, status=JobStatus.QUEUED)
        asyncio.create_task(self._run_job(handle.job_id))
        return handle

    async def _run_job(self, job_id: str) -> None:
        record = self._jobs[job_id]
        record.status = JobStatus.RUNNING

        loop = asyncio.get_running_loop()
        try:
            counts = await loop.run_in_executor(None, self._simulate_sync, record.circuit)
        except Exception as exc:  # noqa: BLE001 - surface any Aer/transpile error as a failed job
            record.status = JobStatus.FAILED
            record.result = ExperimentResult(
                job_id=job_id,
                status=JobStatus.FAILED,
                error=f"simulation error: {exc!r}",
            )
            return

        record.status = JobStatus.COMPLETED
        record.result = ExperimentResult(
            job_id=job_id,
            status=JobStatus.COMPLETED,
            counts=counts,
            metadata={
                "backend": self.name,
                "shots": record.circuit.shots,
                "method": self._sim.options.method,
            },
        )

    def _simulate_sync(self, circuit: Circuit) -> dict[str, int]:
        """Runs on a worker thread -- must not touch asyncio state."""
        transpiled = transpile(circuit.payload, self._sim)
        run_kwargs = {"shots": circuit.shots}
        if self._seed_simulator is not None:
            run_kwargs["seed_simulator"] = self._seed_simulator
        job = self._sim.run(transpiled, **run_kwargs)
        result = job.result()
        return dict(result.get_counts(transpiled))

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
        return record.result

    async def cancel(self, handle: JobHandle) -> None:
        # Aer does not support cancelling an in-flight run() call once it has
        # started on the worker thread. Best-effort: if it hasn't started
        # running yet, mark it cancelled so _run_job's result is ignored by
        # any caller that already gave up (this mirrors real hardware, where
        # "cancel" often just means "we won't wait for the result").
        record = self._jobs.get(handle.job_id)
        if record and record.status == JobStatus.QUEUED:
            record.status = JobStatus.CANCELLED
