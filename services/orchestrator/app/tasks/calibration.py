"""
Calibration task: periodically runs a known circuit against the backend
and measures how far the result drifts from the ideal, exposing that as a
single `error_rate` metric.

This is the first piece of the "automation systems" / calibration idea
sketched in the very first architecture discussion for this project (and
the reason `mock_hw_backend.py` simulates a "calibration in progress"
failure state).

The circuit: a Bell pair (H + CX on 2 qubits, same construction as
demo_aer.py, already confirmed working end-to-end). An ideal Bell pair
measures '00' or '11' only -- never '01'/'10'. `error_rate` is the
fraction of shots that landed on '01'/'10', i.e. shots inconsistent with
perfect entanglement.

⚠️ Honest limitation: `AerBackend` (this project's only real backend so
far) is a *noiseless* simulator -- no noise model is configured, so
`error_rate` will read ~0.0 every time, with no drift to actually detect.
This module is still worth having: it's a real, working health-check
(confirms the backend is up, responsive, and produces circuits that behave
as expected), and it's the natural place to plug in an Aer noise model (or
eventually a real backend) to get meaningful drift signal later.

Results are published to the Kafka topic `calibration-results`, consumed
in real time by services/stream-analytics (rolling error-rate average,
alerting). This used to publish to a RabbitMQ queue as a temporary
stand-in -- see docs/architecture/kafka.md for the migration and why
`run_calibration()` itself didn't need to change, only the publish step.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

from aiokafka import AIOKafkaProducer
from qiskit import QuantumCircuit

from quantum_core.backends.base import Circuit, QuantumBackend
from quantum_core.sync.polling import PollingConfig, wait_for_result

logger = logging.getLogger("orchestrator.calibration")

CALIBRATION_TOPIC = "calibration-results"
DEFAULT_SHOTS = 1024


@dataclass(frozen=True)
class CalibrationResult:
    timestamp: str
    backend_name: str
    shots: int
    error_rate: float
    counts: dict[str, int]

    def to_json(self) -> str:
        import json

        return json.dumps(asdict(self))


def _build_bell_circuit() -> QuantumCircuit:
    qc = QuantumCircuit(2)
    qc.h(0)
    qc.cx(0, 1)
    qc.measure_all()
    return qc


async def run_calibration(backend: QuantumBackend, *, shots: int = DEFAULT_SHOTS) -> CalibrationResult:
    """Runs one calibration cycle: submit the Bell circuit, wait for the
    result via the standard hw/sw synchronization primitive (same
    `wait_for_result` used for every algorithm in this project -- a
    calibration run is just another job as far as the backend is
    concerned), and compute the error rate.
    """
    circuit = Circuit(name="calibration-bell", num_qubits=2, payload=_build_bell_circuit(), shots=shots)
    handle = await backend.submit(circuit)
    result = await wait_for_result(backend, handle, config=PollingConfig(timeout_s=30.0))

    counts = result.counts or {}
    total = sum(counts.values()) or 1  # avoid division by zero on a degenerate empty result
    inconsistent = counts.get("01", 0) + counts.get("10", 0)
    error_rate = inconsistent / total

    return CalibrationResult(
        timestamp=datetime.now(timezone.utc).isoformat(),
        backend_name=backend.name,
        shots=shots,
        error_rate=error_rate,
        counts=counts,
    )


async def publish_calibration_result(producer: AIOKafkaProducer, result: CalibrationResult) -> None:
    await producer.send_and_wait(CALIBRATION_TOPIC, result.to_json().encode())


async def run_calibration_loop(
    backend: QuantumBackend,
    producer: AIOKafkaProducer,
    *,
    interval_s: float = 300.0,
) -> None:
    """Runs `run_calibration` repeatedly forever, with `interval_s` between
    cycles (default 5 minutes). Meant to be launched as a background
    `asyncio.create_task` alongside the main task-consuming loop in
    worker.py -- it shares the same backend instance, but has its own
    Kafka producer (started/stopped independently of the RabbitMQ
    connection used for task processing).
    """
    while True:
        try:
            result = await run_calibration(backend)
            await publish_calibration_result(producer, result)
            logger.info(
                "calibration cycle: error_rate=%.4f shots=%d", result.error_rate, result.shots
            )
        except Exception:  # noqa: BLE001 -- a failed calibration cycle shouldn't crash the loop
            logger.exception("calibration cycle failed, will retry after interval")

        await asyncio.sleep(interval_s)


if __name__ == "__main__":
    # Manual one-off run: python3 -m app.tasks.calibration
    # Prints the result instead of publishing to Kafka -- useful for
    # checking the backend is healthy without needing a broker connection.
    from quantum_core.backends.aer_backend import AerBackend

    async def _main() -> None:
        backend = AerBackend()
        result = await run_calibration(backend)
        print(result.to_json())

    asyncio.run(_main())