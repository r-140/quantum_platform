"""
Orchestrator worker: consumes ExperimentTask messages from RabbitMQ,
executes them via app.tasks.run_experiment (which calls quantum_core.execution
-- the same functions the API used to call directly before this queue
existed), and publishes an ExperimentResultMessage back so the API can
update its store. Also launches a periodic calibration cycle
(app.tasks.calibration) as a background task alongside task processing.

This module itself is deliberately thin -- just RabbitMQ connection setup
and the consume loop. Dispatch logic lives in app/tasks/run_experiment.py,
retry/dead-letter policy lives in app/retry_policy.py, and calibration
lives in app/tasks/calibration.py -- each independently testable without
needing a real RabbitMQ connection.

Three distinct failure modes, handled differently -- see retry_policy.py
for the third:

1. **Malformed message** (bad JSON, can't even parse as an ExperimentTask)
   -- not retryable, since retrying the exact same bytes would fail the
   same way forever. Sent straight to the dead-letter queue
   (`retry_policy.send_to_dead_letter_queue`) and acked off the main queue.
2. **Algorithm/backend execution failure** (circuit error, backend
   timeout) -- a *definitive* answer, not a crash: captured as a FAILED
   ExperimentResultMessage and the task is acked normally. This is not
   retried by this worker at all; from the queue's perspective the task
   was handled successfully (we produced a result, even though that result
   is "it failed").
3. **Worker-level crash** (connection dropped, unhandled exception before
   reaching ack/reject) -- the one case where RabbitMQ's own redelivery
   kicks in automatically. Without a policy, a message that reliably
   crashes the worker would be redelivered *forever*, monopolizing the
   queue. `retry_policy.handle_redelivery` caps this at a bounded number of
   retries (with backoff) before routing to the dead-letter queue too --
   see that module's docstring for why this is a genuinely different
   concern from `quantum_core.sync.polling`'s backend-level retry/backoff.

Run with (from services/orchestrator/):
    python3 -m app.worker

Not `python3 app/worker.py` -- this module uses absolute imports (`from
app import retry_policy`, `from app.tasks import ...`), which require
`app` to be importable as a package. Running as `python3 -m app.worker`
puts `services/orchestrator/` (the parent of `app/`) on sys.path
automatically; running the file directly only puts `app/` itself there, so
`import app` fails with `ModuleNotFoundError: No module named 'app'`.
"""

from __future__ import annotations

import asyncio
import logging
import os

import aio_pika
from aio_pika.abc import AbstractIncomingMessage

from quantum_core.backends.aer_backend import AerBackend
from quantum_core.backends.base import QuantumBackend
from quantum_core.tasks import RESULTS_QUEUE_NAME, TASK_QUEUE_NAME, ExperimentResultMessage, ExperimentTask

from app import retry_policy
from app.tasks.calibration import run_calibration_loop
from app.tasks.run_experiment import execute_task

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("orchestrator")

# Qiskit's transpiler logs one INFO line per optimization pass (very
# verbose -- dozens of lines per circuit) at the same log level as our own
# operational logs. Silencing it to WARNING keeps `processing
# experiment_id=...` / `calibration cycle: ...` visible in the log instead
# of buried under transpiler internals; doesn't affect Qiskit's actual
# behavior, only how much it prints.
logging.getLogger("qiskit").setLevel(logging.WARNING)

RABBITMQ_URL = os.environ.get("RABBITMQ_URL", "amqp://guest:guest@localhost/")
CALIBRATION_INTERVAL_S = float(os.environ.get("CALIBRATION_INTERVAL_S", "300"))


async def handle_message(
    message: AbstractIncomingMessage,
    backend: QuantumBackend,
    channel: aio_pika.abc.AbstractChannel,
) -> None:
    try:
        task = ExperimentTask.from_json(message.body.decode())
    except Exception as exc:  # noqa: BLE001 -- malformed message, not retryable
        logger.error("malformed task message, sending to dead-letter queue: %s", exc)
        await retry_policy.send_to_dead_letter_queue(channel, message)
        await message.ack()
        return

    logger.info("processing experiment_id=%s algorithm=%s", task.experiment_id, task.algorithm)

    try:
        result = await execute_task(backend, task)
        result_message = ExperimentResultMessage(
            experiment_id=task.experiment_id, status="completed", result=result
        )
    except Exception as exc:  # noqa: BLE001 -- a definitive (non-retryable) application-level failure
        logger.exception("experiment_id=%s failed", task.experiment_id)
        result_message = ExperimentResultMessage(
            experiment_id=task.experiment_id, status="failed", error=str(exc)
        )

    await channel.default_exchange.publish(
        aio_pika.Message(
            body=result_message.to_json().encode(),
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
        ),
        routing_key=RESULTS_QUEUE_NAME,
    )
    await message.ack()
    logger.info("experiment_id=%s -> %s", task.experiment_id, result_message.status)


async def main() -> None:
    backend = AerBackend()

    connection = await aio_pika.connect_robust(RABBITMQ_URL)
    async with connection:
        channel = await connection.channel()
        # One task in flight at a time per worker -- VQE tasks can take
        # ~1 minute, and processing tasks strictly one-at-a-time keeps this
        # first version simple and predictable. Run multiple `worker.py`
        # processes for concurrency rather than raising this, at least
        # until there's a reason (measured, not assumed) to do otherwise.
        await channel.set_qos(prefetch_count=1)

        task_queue = await channel.declare_queue(TASK_QUEUE_NAME, durable=True)
        # No explicit bind needed (or allowed): RabbitMQ's default exchange
        # automatically routes to any queue using the queue's own name as
        # the routing key. Declaring the queue is enough; an explicit
        # `queue.bind(channel.default_exchange, ...)` call would actually
        # fail with ACCESS_REFUSED (binding to the default exchange is
        # reserved/automatic, not something a client is allowed to do).
        await channel.declare_queue(RESULTS_QUEUE_NAME, durable=True)

        calibration_task = asyncio.create_task(
            run_calibration_loop(backend, channel, interval_s=CALIBRATION_INTERVAL_S)
        )

        logger.info("orchestrator started, waiting for tasks on %r", TASK_QUEUE_NAME)

        try:
            async with task_queue.iterator() as queue_iter:
                async for message in queue_iter:
                    should_process = await retry_policy.handle_redelivery(
                        channel, message, TASK_QUEUE_NAME
                    )
                    if not should_process:
                        # retry_policy already either republished a retry
                        # copy or routed this to the dead-letter queue --
                        # remove the original from the main queue either way.
                        await message.ack()
                        continue

                    await handle_message(message, backend, channel)
        finally:
            calibration_task.cancel()
            try:
                await calibration_task
            except asyncio.CancelledError:
                pass


if __name__ == "__main__":
    asyncio.run(main())