"""
Publishes per-COBYLA-iteration VQE metrics (quantum vs. classical time
split, retry counts, circuit-breaker trips, and the energy value itself --
the convergence curve) to Kafka, for TimescaleDB persistence and Grafana
visualization.

This is the "VQE metrics for the hw/sw interaction loop" item from
docs/tech-debt.md -- a natural extension of the calibration pipeline
(calibration.py) applied to VQE's own repeated hw/sw round trips.

quantum_core.loops.vqe_loop already collects this data per iteration
(VQEIterationLog, via the PollingMetrics/VQEIterationMetrics
instrumentation layer added to polling.py/vqe_loop.py) -- quantum_core
itself stays framework/broker-agnostic (same principle as everywhere
else in this project, e.g. execution.py/tasks.py), so the actual
publishing happens here in orchestrator, reusing the same shared
AIOKafkaProducer instance calibration.py already uses (passed in, not
created fresh per call -- a new producer per VQE run would mean a
broker handshake on every single experiment).

Published *after* the full VQE run completes, not truly live during it.
run_vqe_sync() is synchronous and runs inside a background thread (via
run_in_executor, see app/tasks/run_experiment.py), and AIOKafkaProducer
isn't safe to drive from a thread other than the one its event loop
belongs to. True per-iteration live streaming would need a thread-safe
bridge back to the main event loop (e.g. asyncio.run_coroutine_threadsafe)
-- not implemented here: a full VQE run is only ~1 minute total (see
services/quantum-core/README.md), so publishing the whole history right
after completion is a reasonable scope trade-off, not a fundamental
limitation. Worth revisiting if VQE runs grow long enough that seeing the
convergence curve *during* the run (not just after) becomes valuable.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

from aiokafka import AIOKafkaProducer

logger = logging.getLogger("orchestrator.vqe_metrics")

VQE_METRICS_TOPIC = "vqe-iteration-metrics"


@dataclass(frozen=True)
class VQEIterationMetricsMessage:
    experiment_id: str
    iteration: int
    params: list[float]
    energy: float
    quantum_time_s: float
    classical_time_s: float
    retry_count: int
    circuit_breaker_trips: int
    timestamp: str

    def to_json(self) -> str:
        return json.dumps(asdict(self))


async def publish_vqe_history(
    producer: AIOKafkaProducer, experiment_id: str, history: list[dict]
) -> None:
    """Sends one Kafka message per COBYLA iteration in `history` -- the
    `history` field of run_vqe_sync()'s result dict (see
    quantum_core.execution.run_vqe_sync and quantum_core.loops.vqe_loop
    for where each entry's fields come from).

    Best-effort: a publish failure here doesn't fail the whole experiment
    or get re-raised -- the experiment's own result has already been
    computed successfully by this point, and only the metrics
    visualization would be incomplete, not the experiment itself. Stops
    publishing the rest of this run's history on the first failure
    (rather than retrying per-message) since a failure here almost always
    means the broker itself is unreachable, not a one-off bad message --
    retrying each of up to ~80 messages individually against a down
    broker would only slow down task completion for no benefit.
    """
    timestamp = datetime.now(timezone.utc).isoformat()
    for entry in history:
        message = VQEIterationMetricsMessage(
            experiment_id=experiment_id,
            iteration=entry["iteration"],
            params=entry["params"],
            energy=entry["energy"],
            quantum_time_s=entry["quantum_time_s"],
            classical_time_s=entry["classical_time_s"],
            retry_count=entry["retry_count"],
            circuit_breaker_trips=entry["circuit_breaker_trips"],
            timestamp=timestamp,
        )
        try:
            await producer.send_and_wait(VQE_METRICS_TOPIC, message.to_json().encode())
        except Exception:
            logger.exception(
                "failed to publish vqe iteration metrics, experiment_id=%s iteration=%s "
                "-- broker likely unreachable, not retrying remaining iterations for this run",
                experiment_id,
                entry.get("iteration"),
            )
            return
