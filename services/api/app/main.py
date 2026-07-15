"""
FastAPI application entry point.

Run with (from services/api/, and with RabbitMQ running -- see root
docker-compose.yml):
    uvicorn app.main:app --reload --port 8000

Then either use the interactive docs at http://localhost:8000/docs, or:
    curl -X POST http://localhost:8000/experiments \\
        -H "Content-Type: application/json" \\
        -d '{"algorithm": "grover", "marked_states": ["101"]}'
    curl http://localhost:8000/experiments/<id-from-above>
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.deps import ExperimentStore, close_rabbitmq, get_rabbitmq_channel, get_store, init_rabbitmq, utcnow
from app.routers import backends, experiments
from app.schemas.experiments import ExperimentStatus
from quantum_core.tasks import RESULTS_QUEUE_NAME, ExperimentResultMessage

logger = logging.getLogger("api")

RABBITMQ_URL = os.environ.get("RABBITMQ_URL", "amqp://guest:guest@localhost/")


def apply_result_message(result_msg: ExperimentResultMessage, store: ExperimentStore) -> None:
    """Updates `store` for a single result message -- pulled out of
    `consume_results` so it's testable directly (constructing a real
    aio-pika message/queue iterator in a unit test is not worth the
    trouble; this function has no aio-pika dependency at all).
    """
    existing = store.get(result_msg.experiment_id)
    if existing is None:
        # Unknown id -- e.g. a result for an experiment submitted to a
        # *previous* instance of this API process (the in-memory store
        # doesn't survive restarts; see app/deps.py). Nothing to update.
        logger.warning("received result for unknown experiment_id=%s", result_msg.experiment_id)
        return

    updated = existing.model_copy(
        update={
            "status": ExperimentStatus.COMPLETED
            if result_msg.status == "completed"
            else ExperimentStatus.FAILED,
            "completed_at": utcnow(),
            "result": result_msg.result,
            "error": result_msg.error,
        }
    )
    store.save(updated)
    logger.info("experiment_id=%s updated to status=%s", result_msg.experiment_id, updated.status)


async def consume_results() -> None:
    """Background task: listens on the results queue and applies each
    message via `apply_result_message`. This is what lets
    GET /experiments/{id} eventually report status=completed/failed for a
    request that POST /experiments returned as status=queued.

    Runs for the lifetime of the app (started in `lifespan`, cancelled on
    shutdown) rather than per-request -- there's exactly one of these per
    API process, consuming continuously, independent of how many HTTP
    requests are in flight.
    """
    channel = get_rabbitmq_channel()
    results_queue = await channel.declare_queue(RESULTS_QUEUE_NAME, durable=True)
    store = get_store()

    async with results_queue.iterator() as queue_iter:
        async for message in queue_iter:
            async with message.process():
                result_msg = ExperimentResultMessage.from_json(message.body.decode())
                apply_result_message(result_msg, store)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_rabbitmq(RABBITMQ_URL)
    consumer_task = asyncio.create_task(consume_results())
    try:
        yield
    finally:
        consumer_task.cancel()
        try:
            await consumer_task
        except asyncio.CancelledError:
            pass
        await close_rabbitmq()


app = FastAPI(
    title="Quantum Platform API",
    description="Accepts quantum experiment requests (Grover, SAT-Grover, QPE, VQE), "
    "enqueues them to RabbitMQ, and returns immediately with status=queued. "
    "The orchestrator service consumes the queue, executes against a "
    "QuantumBackend, and publishes results back for this API to pick up.",
    version="0.2.0",
    lifespan=lifespan,
)

app.include_router(experiments.router)
app.include_router(backends.router)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}