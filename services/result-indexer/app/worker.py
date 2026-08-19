"""Consumes completed experiments, embeds their canonical text, and indexes it."""

from __future__ import annotations

import asyncio
import json
import logging
import os

import asyncpg
from aiokafka import AIOKafkaConsumer
from sentence_transformers import SentenceTransformer

from app.canonical import canonical_experiment_text
from app.repository import upsert_embedding

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("result-indexer")

TOPIC = "experiment-completed"
MODEL_NAME = os.environ.get("EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2")
KAFKA_BOOTSTRAP_SERVERS = os.environ.get("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
DATABASE_DSN = os.environ.get(
    "INDEX_DATABASE_DSN", "postgresql://quantum:quantum@localhost:5432/quantum_platform"
)


async def run() -> None:
    model = await asyncio.to_thread(SentenceTransformer, MODEL_NAME)
    pool = await asyncpg.create_pool(DATABASE_DSN)
    consumer = AIOKafkaConsumer(
        TOPIC,
        bootstrap_servers=KAFKA_BOOTSTRAP_SERVERS,
        group_id="result-indexer",
        auto_offset_reset="earliest",
        enable_auto_commit=False,
    )
    await consumer.start()
    logger.info("indexing '%s' with %s", TOPIC, MODEL_NAME)
    try:
        async for message in consumer:
            experiment = json.loads(message.value)
            content = canonical_experiment_text(experiment)
            embedding = await asyncio.to_thread(
                model.encode, content, normalize_embeddings=True
            )
            await upsert_embedding(
                pool,
                experiment=experiment,
                content=content,
                embedding=embedding,
                model_name=MODEL_NAME,
            )
            await consumer.commit()
            logger.info("indexed experiment_id=%s", experiment["id"])
    finally:
        await consumer.stop()
        await pool.close()


if __name__ == "__main__":
    asyncio.run(run())
