"""
FastAPI application entry point.

Run with (from services/api/, and with RabbitMQ + Postgres running -- see
root docker-compose.yml, and docs/architecture/postgres.md for running
Alembic migrations first):
    uvicorn app.main:app --reload --port 8000

Then either use the interactive docs at http://localhost:8000/docs,
the experiments dashboard at http://localhost:8000/dashboard/, or:
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
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.db import close_db, init_db
from app.deps import close_rabbitmq, get_rabbitmq_channel, get_store, init_rabbitmq, utcnow
from app.routers import backends, experiments
from app.schemas.experiments import ExperimentStatus
from app.store.base import ExperimentStore
from quantum_core.tasks import RESULTS_QUEUE_NAME, ExperimentResultMessage

logger = logging.getLogger("api")

RABBITMQ_URL = os.environ.get("RABBITMQ_URL", "amqp://guest:guest@localhost/")
DATABASE_URL = os.environ.get("DATABASE_URL")

# Absolute path derived from this file's location, not a path relative to
# the process's current working directory -- this project has twice
# already hit bugs from assuming a particular CWD (editable installs
# resolving `-e ../quantum-core` relative to the pip invocation's CWD, and
# `alembic`/`python module.py` needing `-m` to get the right CWD on
# sys.path). `uvicorn app.main:app` is always launched from services/api/
# by convention here, but there's no reason to depend on that for
# something this cheap to make robust instead.
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


async def apply_result_message(result_msg: ExperimentResultMessage, store: ExperimentStore) -> None:
    """Updates `store` for a single result message -- pulled out of
    `consume_results` so it's testable directly (constructing a real
    aio-pika message/queue iterator in a unit test is not worth the
    trouble; this function has no aio-pika dependency at all).
    """
    existing = await store.get(result_msg.experiment_id)
    if existing is None:
        # Unknown id -- e.g. a result for an experiment submitted to a
        # *previous* instance of this API process while using the
        # in-memory store fallback (doesn't survive restarts; see
        # app/deps.py). With Postgres configured this shouldn't happen in
        # practice. Nothing to update either way.
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
    await store.save(updated)
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
                await apply_result_message(result_msg, store)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if DATABASE_URL:
        await init_db(DATABASE_URL)
    else:
        logger.warning(
            "DATABASE_URL not set -- falling back to in-memory experiment store "
            "(won't survive a restart; see app/deps.py)"
        )

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
        if DATABASE_URL:
            await close_db()


app = FastAPI(
    title="Quantum Platform API",
    description="Accepts quantum experiment requests (Grover, SAT-Grover, QPE, VQE), "
    "enqueues them to RabbitMQ, and returns immediately with status=queued. "
    "The orchestrator service consumes the queue, executes against a "
    "QuantumBackend, and publishes results back for this API to pick up. "
    "Experiment metadata is persisted to Postgres when DATABASE_URL is set.",
    version="0.3.0",
    lifespan=lifespan,
)

app.include_router(experiments.router)
app.include_router(backends.router)

# Mounted at /dashboard, not "/" -- StaticFiles(html=True) at the root
# would shadow every other route in this app (/experiments, /health,
# /docs). Serves static/dashboard/index.html for GET /dashboard/ (and any
# path under it) automatically.
app.mount(
    "/dashboard",
    StaticFiles(directory=STATIC_DIR / "dashboard", html=True),
    name="dashboard",
)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}