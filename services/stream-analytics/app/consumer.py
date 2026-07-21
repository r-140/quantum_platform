"""
Consumes the `calibration-results` Kafka topic (published by
orchestrator/app/tasks/calibration.py) and computes a rolling average
error_rate per backend, logging an alert if it exceeds a threshold.

This is the "stream-analytics" piece sketched in the very first
architecture discussion for this project -- the real-time-aggregation
counterpart to the task-queue side of the system (RabbitMQ/orchestrator).
See docs/architecture/kafka.md for why Kafka (not RabbitMQ) is the right
tool for this specific job, and the deliberate choice of a hand-rolled
consumer loop over Kafka Streams/Faust at this project's current scale
(see rolling.py's docstring).

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

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("stream-analytics")

KAFKA_BOOTSTRAP_SERVERS = os.environ.get("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
CALIBRATION_TOPIC = "calibration-results"

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
        bootstrap_servers=KAFKA_BOOTSTRAP_SERVERS,
        group_id="stream-analytics",
        auto_offset_reset="latest",
    )
    rolling = RollingErrorRate()

    await consumer.start()
    logger.info("stream-analytics started, consuming %r", CALIBRATION_TOPIC)
    try:
        async for message in consumer:
            payload = json.loads(message.value.decode())
            backend_name = payload["backend_name"]
            error_rate = payload["error_rate"]

            rolling_avg = rolling.add_sample(backend_name, error_rate)
            logger.info(
                "backend=%s error_rate=%.4f rolling_avg(n=%d)=%.4f",
                backend_name,
                error_rate,
                rolling.sample_count(backend_name),
                rolling_avg,
            )

            if rolling_avg > ALERT_THRESHOLD:
                logger.warning(
                    "ALERT: backend=%s rolling average error_rate=%.4f exceeds threshold %.4f",
                    backend_name,
                    rolling_avg,
                    ALERT_THRESHOLD,
                )
    finally:
        await consumer.stop()


if __name__ == "__main__":
    asyncio.run(consume_calibration_results())
