"""
FastAPI dependencies: a shared QuantumBackend instance, the experiment
store (Postgres-backed if DATABASE_URL is set, in-memory otherwise), and
the RabbitMQ connection used to enqueue experiments for the orchestrator.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

from quantum_core.backends.base import QuantumBackend
from quantum_core.tasks import RESULTS_QUEUE_NAME, TASK_QUEUE_NAME, ExperimentTask

from app.store.base import ExperimentStore
from app.store.in_memory import InMemoryExperimentStore

_backend: QuantumBackend | None = None


def get_backend() -> QuantumBackend:
    """A single shared AerBackend instance for the process lifetime.

    Currently unused by the experiments router itself -- execution moved to
    the orchestrator once the RabbitMQ queue was introduced (see
    routers/experiments.py) -- but kept available for anything that might
    want direct in-process execution later (a debug/sync-mode endpoint,
    tests, etc.).

    The import is deliberately local to this function, not at module level:
    `app.deps` is imported by nearly everything in this service (routers,
    tests), and an eager `from quantum_core.backends.aer_backend import
    AerBackend` at module level would mean *anything* touching `app.deps` --
    including tests that only care about pure-Python store logic --
    transitively requires qiskit/qiskit-aer to be importable.
    """
    global _backend
    if _backend is None:
        from quantum_core.backends.aer_backend import AerBackend

        _backend = AerBackend()
    return _backend


# --- Experiment store ---------------------------------------------------
#
# Postgres-backed if DATABASE_URL is set (the normal case, once Postgres is
# running -- see app/main.py's lifespan, which calls init_db before this is
# ever used for real requests); falls back to an in-memory store otherwise,
# which is what keeps `pytest tests/` fast and dependency-free -- tests
# override this dependency entirely via FastAPI's `app.dependency_overrides`
# (see tests/conftest.py) rather than relying on this fallback, but it's
# also what a bare `uvicorn app.main:app` run would silently use if
# DATABASE_URL were unset and init_db were skipped -- a state worth being
# loud about, not falling into quietly (see main.py's lifespan, which logs
# a warning in that case).

_fallback_store: ExperimentStore | None = None


def get_store() -> ExperimentStore:
    database_url = os.environ.get("DATABASE_URL")
    if database_url:
        from app.db import get_sessionmaker
        from app.store.postgres import PostgresExperimentStore

        return PostgresExperimentStore(get_sessionmaker())

    global _fallback_store
    if _fallback_store is None:
        _fallback_store = InMemoryExperimentStore()
    return _fallback_store


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --- RabbitMQ ---------------------------------------------------------
#
# Connection/channel are process-lifetime singletons, set up once via
# `init_rabbitmq` (called from app/main.py's lifespan on startup) and torn
# down via `close_rabbitmq` on shutdown. `app.deps` stays the single place
# that owns this state -- routers only ever call `publish_task`, never
# touch aio-pika directly.

_rabbitmq_connection = None  # aio_pika.abc.AbstractRobustConnection | None
_rabbitmq_channel = None  # aio_pika.abc.AbstractChannel | None


async def init_rabbitmq(url: str) -> None:
    global _rabbitmq_connection, _rabbitmq_channel
    import aio_pika

    _rabbitmq_connection = await aio_pika.connect_robust(url)
    _rabbitmq_channel = await _rabbitmq_connection.channel()
    # Declared here (not left solely to the orchestrator) so the API can
    # publish successfully even if the orchestrator hasn't started yet --
    # whichever side starts first creates the queue.
    await _rabbitmq_channel.declare_queue(TASK_QUEUE_NAME, durable=True)
    await _rabbitmq_channel.declare_queue(RESULTS_QUEUE_NAME, durable=True)


async def close_rabbitmq() -> None:
    global _rabbitmq_connection
    if _rabbitmq_connection is not None:
        await _rabbitmq_connection.close()
        _rabbitmq_connection = None


def get_rabbitmq_channel():
    if _rabbitmq_channel is None:
        raise RuntimeError(
            "RabbitMQ channel not initialized -- init_rabbitmq() must run first "
            "(normally via app.main's lifespan on startup)"
        )
    return _rabbitmq_channel


async def publish_task(task: ExperimentTask) -> None:
    import aio_pika

    channel = get_rabbitmq_channel()
    await channel.default_exchange.publish(
        aio_pika.Message(
            body=task.to_json().encode(),
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
        ),
        routing_key=TASK_QUEUE_NAME,
    )