"""Kafka publisher for durable, replayable experiment-completion events."""

from __future__ import annotations

import json
import os

from aiokafka import AIOKafkaProducer

COMPLETED_EXPERIMENTS_TOPIC = "experiment-completed"
KAFKA_BOOTSTRAP_SERVERS = os.environ.get("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")

_producer: AIOKafkaProducer | None = None


async def init_kafka() -> None:
    global _producer
    _producer = AIOKafkaProducer(bootstrap_servers=KAFKA_BOOTSTRAP_SERVERS)
    await _producer.start()


async def close_kafka() -> None:
    global _producer
    if _producer is not None:
        await _producer.stop()
        _producer = None


async def publish_completed_experiment(payload: dict) -> None:
    if _producer is None:
        raise RuntimeError("Kafka producer is not initialized")
    experiment_id = str(payload["id"])
    await _producer.send_and_wait(
        COMPLETED_EXPERIMENTS_TOPIC,
        json.dumps(payload, separators=(",", ":")).encode(),
        key=experiment_id.encode(),
    )
