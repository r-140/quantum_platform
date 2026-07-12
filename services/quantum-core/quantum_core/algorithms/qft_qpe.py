"""
Hardware/software abstraction boundary for quantum backends.

This module defines the contract that every backend (simulator, mock
hardware, real QPU) must satisfy. The abstraction is deliberately
asynchronous and job-based: real quantum hardware does not return results
synchronously. Instead, a circuit is submitted, queued, executed, and polled
for status until a result (or failure) is available.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any
import uuid


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class Circuit:
    """Minimal circuit representation, backend-agnostic.

    In a real implementation this would wrap a Qiskit/Cirq circuit object.
    Kept generic here so the interaction-loop code doesn't depend on any
    specific SDK.
    """

    name: str
    num_qubits: int
    payload: Any  # opaque to this layer; the backend knows how to interpret it
    shots: int = 1024


@dataclass(frozen=True)
class JobHandle:
    """Opaque reference to a submitted job. Backends may attach their own
    provider-specific id internally, but callers only ever need `job_id`.
    """

    job_id: str
    backend_name: str

    @staticmethod
    def new(backend_name: str) -> "JobHandle":
        return JobHandle(job_id=str(uuid.uuid4()), backend_name=backend_name)


@dataclass(frozen=True)
class ExperimentResult:
    job_id: str
    status: JobStatus
    counts: dict[str, int] | None = None  # measurement histogram
    metadata: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


class TransientBackendError(Exception):
    """Raised for errors that are expected to be retryable (queue timeout,
    calibration in progress, transient connection issues). Distinguishing
    this from a hard failure is what allows the polling loop to retry
    intelligently instead of failing the whole experiment.
    """


class QuantumBackend(ABC):
    """Abstract hardware/software boundary.

    Every method is async because real backends involve network I/O and
    non-trivial wait times. Implementations must not block the event loop.
    """

    name: str

    @abstractmethod
    async def submit(self, circuit: Circuit) -> JobHandle:
        """Submit a circuit for execution. Returns immediately with a
        handle; does not wait for completion.
        """

    @abstractmethod
    async def poll_status(self, handle: JobHandle) -> JobStatus:
        """Return the current status of a job. Must be cheap/fast to call
        repeatedly -- this is invoked by the polling loop.
        """

    @abstractmethod
    async def fetch_result(self, handle: JobHandle) -> ExperimentResult:
        """Fetch the result of a completed job. Should only be called once
        `poll_status` reports COMPLETED or FAILED.
        """

    @abstractmethod
    async def cancel(self, handle: JobHandle) -> None:
        """Best-effort cancellation of a queued/running job."""
