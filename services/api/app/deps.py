"""
FastAPI dependencies: a shared QuantumBackend instance, an in-memory
experiment store, and the RabbitMQ connection used to enqueue experiments
for the orchestrator.

The in-memory store is a deliberate, temporary simplification -- it won't
survive a process restart and won't work correctly if the API is ever run
with multiple worker processes (each would have its own store). This is
exactly the gap Postgres (for experiment metadata) is meant to fill once
the storage layer is added; nothing here pretends otherwise.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone

from quantum_core.backends.base import QuantumBackend
from quantum_core.tasks import RESULTS_QUEUE_NAME, TASK_QUEUE_NAME, ExperimentTask

from app.schemas.experiments import ExperimentResponse

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
    including tests that only care about `ExperimentStore`'s pure-Python
    logic -- transitively requires qiskit/qiskit-aer to be importable.
    """
    global _backend
    if _backend is None:
        from quantum_core.backends.aer_backend import AerBackend

        _backend = AerBackend()
    return _backend


class ExperimentStore:
    """Thread-safe in-memory store, keyed by experiment id.

    The lock is cheap insurance: the results-queue consumer (see
    app/main.py's `consume_results`) writes to this store from a background
    asyncio task, and nothing rules out a future sync/threaded write path
    reappearing (as VQE's did, briefly, before execution moved entirely to
    the orchestrator).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: dict[str, ExperimentResponse] = {}

    def save(self, experiment: ExperimentResponse) -> None:
        with self._lock:
            self._data[experiment.id] = experiment

    def get(self, experiment_id: str) -> ExperimentResponse | None:
        with self._lock:
            return self._data.get(experiment_id)

    def list_all(self) -> list[ExperimentResponse]:
        with self._lock:
            return list(self._data.values())


_store = ExperimentStore()


def get_store() -> ExperimentStore:
    return _store


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