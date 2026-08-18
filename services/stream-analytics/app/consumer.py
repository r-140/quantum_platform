"""
Consumes the `calibration-results` Kafka topic (published by
orchestrator/app/tasks/calibration.py), computes a rolling average
error_rate per backend (logging an alert if it exceeds a threshold), and
persists each raw event into TimescaleDB via app.sinks.timescale_sink --
so calibration history survives a process restart, unlike the in-memory
rolling window (see rolling.py's docstring).

Also consumes `vqe-iteration-metrics` (published by
orchestrator/app/tasks/vqe_metrics.py) and persists each entry into
TimescaleDB's `vqe_iteration_metrics` hypertable -- the "VQE metrics for
the hw/sw interaction loop" item from docs/tech-debt.md. One
AIOKafkaConsumer subscribed to both topics (rather than a second consumer
process) since both are small-volume, low-effort to dispatch on
`message.topic`, and share the same TimescaleDB connection pool and
consumer-group lifecycle.

This is the "stream-analytics" piece sketched in the very first
architecture discussion for this project -- the real-time-aggregation
counterpart to the task-queue side of the system (RabbitMQ/orchestrator).
See docs/architecture/kafka.md for why Kafka (not RabbitMQ) is the right
tool for this specific job, and the deliberate choice of a hand-rolled
consumer loop over Kafka Streams/Faust at this project's current scale
(see rolling.py's docstring) -- for the Faust-based alternative that
additionally does alerting and drift detection, see app/faust_app.py and
docs/architecture/stream-analytics-dashboard.md.

Run with (from services/stream-analytics/):
    python3 -m app.consumer
"""

from __future__ import annotations

import asyncio
import json
import logging
import os

from aiokafka import AIOKafkaConsumer

from app.rolling import RollingErrorRate
from app.sinks.timescale_sink import create_pool, insert_calibration_event, insert_vqe_iteration_metric, insert_vqe_window_metric

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("stream-analytics")

KAFKA_BOOTSTRAP_SERVERS = os.environ.get("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
CALIBRATION_TOPIC = "calibration-results"
VQE_METRICS_TOPIC = "vqe-iteration-metrics"
VQE_WINDOW_METRICS_TOPIC = "vqe-window-metrics"
TIMESCALE_DSN = os.environ.get(
    "TIMESCALE_DSN", "postgresql://quantum:quantum@localhost:5433/telemetry"
)

# Rolling average above this triggers an ALERT log line. 5% is a somewhat
# arbitrary placeholder -- there's no real drift signal to calibrate this
# threshold against yet, since AerBackend is noiseless (see
# calibration.py's "Honest limitation"). Revisit once there's a noise
# model or real hardware producing a meaningful error_rate distribution to
# tune this against.
ALERT_THRESHOLD = 0.05


async def consume_calibration_results() -> None:
    consumer = AIOKafkaConsumer(
        CALIBRATION_TOPIC,
        VQE_METRICS_TOPIC,
        VQE_WINDOW_METRICS_TOPIC,
        bootstrap_servers=KAFKA_BOOTSTRAP_SERVERS,
        group_id="stream-analytics",
        auto_offset_reset="latest",
    )

    rolling = RollingErrorRate()
    timescale_pool = await create_pool(TIMESCALE_DSN)

    await consumer.start()

    logger.info(
        "stream-analytics started, consuming %r, %r and %r",
        CALIBRATION_TOPIC,
        VQE_METRICS_TOPIC,
        VQE_WINDOW_METRICS_TOPIC,
    )

    try:
        async for message in consumer:
            payload = json.loads(message.value.decode())

            if message.topic == VQE_METRICS_TOPIC:
                logger.info(
                    "vqe iteration experiment_id=%s iteration=%d energy=%.6f "
                    "quantum_time=%.3fs classical_time=%.3fs "
                    "retries=%d breaker_trips=%d",
                    payload["experiment_id"],
                    payload["iteration"],
                    payload["energy"],
                    payload["quantum_time_s"],
                    payload["classical_time_s"],
                    payload["retry_count"],
                    payload["circuit_breaker_trips"],
                )

                try:
                    await insert_vqe_iteration_metric(
                        timescale_pool,
                        payload,
                    )
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "failed to persist vqe iteration metric to TimescaleDB"
                    )

                continue

            if message.topic == VQE_WINDOW_METRICS_TOPIC:
                logger.info(
                    "vqe window experiment_id=%s window=%ss iterations=%d "
                    "avg_energy=%.6f best_energy=%.6f "
                    "quantum_time=%.3fs classical_time=%.3fs ratio=%.2f "
                    "retries=%d breaker_trips=%d",
                    payload["experiment_id"],
                    payload["window_size_s"],
                    payload["iteration_count"],
                    payload["avg_energy"],
                    payload["best_energy"],
                    payload["avg_quantum_time_s"],
                    payload["avg_classical_time_s"],
                    payload["quantum_classical_ratio"],
                    payload["retry_count"],
                    payload["circuit_breaker_trips"],
                )

                try:
                    await insert_vqe_window_metric(
                        timescale_pool,
                        payload,
                    )
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "failed to persist vqe window metric to TimescaleDB"
                    )

                continue

            # message.topic == CALIBRATION_TOPIC
            backend_name = payload["backend_name"]
            error_rate = payload["error_rate"]

            rolling_avg = rolling.add_sample(
                backend_name,
                error_rate,
            )

            logger.info(
                "backend=%s error_rate=%.4f rolling_avg(n=%d)=%.4f",
                backend_name,
                error_rate,
                rolling.sample_count(backend_name),
                rolling_avg,
            )

            if rolling_avg > ALERT_THRESHOLD:
                logger.warning(
                    "ALERT: backend=%s rolling average error_rate=%.4f "
                    "exceeds threshold %.4f",
                    backend_name,
                    rolling_avg,
                    ALERT_THRESHOLD,
                )

            try:
                await insert_calibration_event(
                    timescale_pool,
                    payload,
                )
            except Exception:  # noqa: BLE001
                logger.exception(
                    "failed to persist calibration event to TimescaleDB"
                )

    finally:
        await consumer.stop()
        await timescale_pool.close()


if __name__ == "__main__":
    asyncio.run(consume_calibration_results())