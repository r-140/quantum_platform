"""
Message schemas for the task queue (RabbitMQ) connecting the API service
(producer) and the orchestrator (consumer), plus the results queue flowing
back the other way.

Plain dataclasses, not Pydantic models: quantum_core has no HTTP-framework
dependency today (see execution.py's docstring for the same reasoning
applied to execution functions), and a queue message is just JSON on the
wire regardless -- a dataclass + a small to_json/from_json pair is enough,
without adding pydantic as a quantum_core dependency for something
json.dumps/loads already does fine.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any

TASK_QUEUE_NAME = "experiments"
RESULTS_QUEUE_NAME = "experiment-results"


@dataclass(frozen=True)
class ExperimentTask:
    """Published to the `experiments` queue by the API, consumed by the
    orchestrator.

    `params` holds algorithm-specific fields as a plain dict (e.g.
    `{"marked_states": ["101"], "shots": 1024}` for grover) rather than one
    dataclass per algorithm -- this module has no business validating the
    *shape* of params, since that already happened in the API's Pydantic
    layer before publishing. This is purely a transport envelope.
    """

    experiment_id: str
    algorithm: str
    params: dict[str, Any]

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @staticmethod
    def from_json(data: str) -> ExperimentTask:
        payload = json.loads(data)
        return ExperimentTask(**payload)


@dataclass(frozen=True)
class ExperimentResultMessage:
    """Published to the `experiment-results` queue by the orchestrator,
    consumed by the API to update its store once a queued experiment
    finishes (successfully or not).
    """

    experiment_id: str
    status: str  # "completed" | "failed"
    result: dict[str, Any] | None = None
    error: str | None = None

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @staticmethod
    def from_json(data: str) -> ExperimentResultMessage:
        payload = json.loads(data)
        return ExperimentResultMessage(**payload)