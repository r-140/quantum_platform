"""
Request/response schemas for the experiments API.

Each algorithm has its own request model; `ExperimentRequest` is a
discriminated union on the `algorithm` field, so FastAPI/Pydantic validates
the right shape automatically based on what the client sends (and rejects,
with a clear error, a request that mixes fields from the wrong algorithm --
e.g. sending `marked_states` for a `vqe` request).
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field


class Algorithm(str, Enum):
    GROVER = "grover"
    SAT_GROVER = "sat_grover"
    QPE = "qpe"
    VQE = "vqe"


class GroverRequest(BaseModel):
    """'Hello world' Grover -- the target is provided directly, not derived
    from a criterion. See quantum_core/algorithms/grover.py.
    """

    algorithm: Literal["grover"] = "grover"
    marked_states: list[str] = Field(
        ..., min_length=1, description="Target bitstrings, e.g. ['101']. All must be equal length."
    )
    shots: int = Field(default=1024, gt=0, le=100_000)


class SatGroverRequest(BaseModel):
    """Real Grover use case: search over a boolean-expression criterion via
    PhaseOracleGate. See quantum_core/algorithms/sat_search.py.
    """

    algorithm: Literal["sat_grover"] = "sat_grover"
    variables: list[str] = Field(..., min_length=1, max_length=8)
    expression: str = Field(
        ..., description="Boolean expression using &, |, ~, ^ over `variables`, e.g. '(x0 | x1) & ~x2'"
    )
    shots: int = Field(default=1024, gt=0, le=100_000)


class QPERequest(BaseModel):
    """Estimates the eigenphase of a phase gate P(2*pi*phi) applied to its
    |1> eigenstate. See quantum_core/algorithms/qpe.py.
    """

    algorithm: Literal["qpe"] = "qpe"
    phi: float = Field(..., ge=0.0, lt=1.0, description="True phase to estimate, in [0, 1)")
    num_counting_qubits: int = Field(default=3, ge=1, le=10)
    shots: int = Field(default=1024, gt=0, le=100_000)


class VQERequest(BaseModel):
    """Finds the H2 ground-state energy via the classical-quantum feedback
    loop. See quantum_core/loops/vqe_loop.py.

    Runs synchronously inside a threadpool (see app/execution.py) -- this
    is the slowest of the four algorithms by a wide margin (many circuit
    submissions per optimizer iteration).
    """

    algorithm: Literal["vqe"] = "vqe"
    shots: int = Field(default=8192, gt=0, le=100_000)
    max_iterations: int = Field(default=80, gt=0, le=1000)


ExperimentRequest = Annotated[
    Union[GroverRequest, SatGroverRequest, QPERequest, VQERequest],
    Field(discriminator="algorithm"),
]


class ExperimentStatus(str, Enum):
    COMPLETED = "completed"
    FAILED = "failed"


class ExperimentResponse(BaseModel):
    id: str
    algorithm: Algorithm
    status: ExperimentStatus
    submitted_at: datetime
    completed_at: datetime | None = None
    result: dict | None = Field(
        default=None, description="Algorithm-specific result payload, e.g. counts or energy."
    )
    error: str | None = None