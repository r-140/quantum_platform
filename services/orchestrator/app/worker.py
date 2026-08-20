"""
Orchestrator worker: consumes ExperimentTask messages from RabbitMQ,
executes them via app.tasks.run_experiment (which calls quantum_core.execution
-- the same functions the API used to call directly before this queue
existed), and publishes an ExperimentResultMessage back so the API can
update its store. Also launches a periodic calibration cycle
(app.tasks.calibration), which publishes to Kafka rather than RabbitMQ --
see docs/architecture/kafka.md.

This module itself is deliberately thin -- just RabbitMQ/Kafka connection
setup and the consume loop. Dispatch logic lives in
app/tasks/run_experiment.py, retry/dead-letter policy lives in
app/retry_policy.py, and calibration lives in app/tasks/calibration.py --
each independently testable without needing a real broker connection.

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
from datetime import timedelta

import aio_pika
import asyncpg
from aio_pika.abc import AbstractIncomingMessage
from aiokafka import AIOKafkaProducer

from quantum_core.backends.aer_backend import AerBackend
from quantum_core.backends.base import QuantumBackend
from quantum_core.tasks import RESULTS_QUEUE_NAME, TASK_QUEUE_NAME, ExperimentResultMessage, ExperimentTask

from app import retry_policy
from app.calibration_policy import CalibrationDecision, CalibrationPolicy
from app.calibration_store import CalibrationStateStore
from app.calibration_wait import (
    DEFAULT_MAX_ATTEMPTS,
    declare_wait_queue,
    reschedule,
    wait_count,
)
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
KAFKA_BOOTSTRAP_SERVERS = os.environ.get("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
CALIBRATION_INTERVAL_S = float(os.environ.get("CALIBRATION_INTERVAL_S", "300"))
CALIBRATION_FRESHNESS_S = float(os.environ.get("CALIBRATION_FRESHNESS_S", "600"))
CALIBRATION_REJECT_ERROR_RATE = float(
    os.environ.get("CALIBRATION_REJECT_ERROR_RATE", "0.10")
)
CALIBRATION_WAIT_DELAY_S = float(os.environ.get("CALIBRATION_WAIT_DELAY_S", "5"))
CALIBRATION_MAX_WAIT_ATTEMPTS = int(
    os.environ.get("CALIBRATION_MAX_WAIT_ATTEMPTS", str(DEFAULT_MAX_ATTEMPTS))
)
DATABASE_DSN = os.environ.get(
    "ORCHESTRATOR_DATABASE_DSN",
    "postgresql://quantum:quantum@localhost:5432/quantum_platform",
)


async def publish_result_message(channel, result_message: ExperimentResultMessage) -> None:
    await channel.default_exchange.publish(
        aio_pika.Message(
            body=result_message.to_json().encode(),
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
        ),
        routing_key=RESULTS_QUEUE_NAME,
    )


async def handle_message(
    message: AbstractIncomingMessage,
    backend: QuantumBackend,
    channel: aio_pika.abc.AbstractChannel,
    kafka_producer: AIOKafkaProducer,
    calibration_store: CalibrationStateStore,
    calibration_policy: CalibrationPolicy,
    calibration_trigger: asyncio.Event,
) -> None:
    try:
        task = ExperimentTask.from_json(message.body.decode())
    except Exception as exc:  # noqa: BLE001 -- malformed message, not retryable
        logger.error("malformed task message, sending to dead-letter queue: %s", exc)
        await retry_policy.send_to_dead_letter_queue(channel, message)
        await message.ack()
        return

    logger.info("processing experiment_id=%s algorithm=%s", task.experiment_id, task.algorithm)

    if task.algorithm == "vqe":
        observation = await calibration_store.get(backend.name)
        decision = calibration_policy.evaluate(observation)
        if decision is CalibrationDecision.REJECT:
            error_rate = observation.error_rate if observation is not None else None
            result_message = ExperimentResultMessage(
                experiment_id=task.experiment_id,
                status="failed",
                error=(
                    "backend calibration rejected execution: "
                    f"bell parity error_rate={error_rate:.4f}"
                ),
            )
            await publish_result_message(channel, result_message)
            await message.ack()
            logger.warning("experiment_id=%s rejected by calibration policy", task.experiment_id)
            return

        if decision is CalibrationDecision.WAIT:
            attempt = wait_count(message)
            if attempt >= CALIBRATION_MAX_WAIT_ATTEMPTS:
                result_message = ExperimentResultMessage(
                    experiment_id=task.experiment_id,
                    status="failed",
                    error="calibration did not become fresh within the wait policy",
                )
                await publish_result_message(channel, result_message)
                await message.ack()
                return

            calibration_trigger.set()  # Event coalesces requests from many waiting jobs.
            await publish_result_message(
                channel,
                ExperimentResultMessage(
                    experiment_id=task.experiment_id,
                    status="waiting_for_calibration",
                ),
            )
            next_attempt = await reschedule(
                channel, message, delay_s=CALIBRATION_WAIT_DELAY_S
            )
            await message.ack()
            logger.info(
                "experiment_id=%s waiting for calibration, attempt=%d/%d",
                task.experiment_id,
                next_attempt,
                CALIBRATION_MAX_WAIT_ATTEMPTS,
            )
            return

    try:
        result = await execute_task(backend, task, kafka_producer=kafka_producer)
        result_message = ExperimentResultMessage(
            experiment_id=task.experiment_id, status="completed", result=result
        )
    except Exception as exc:  # noqa: BLE001 -- a definitive (non-retryable) application-level failure
        logger.exception("experiment_id=%s failed", task.experiment_id)
        result_message = ExperimentResultMessage(
            experiment_id=task.experiment_id, status="failed", error=str(exc)
        )

    await publish_result_message(channel, result_message)
    await message.ack()
    logger.info("experiment_id=%s -> %s", task.experiment_id, result_message.status)


async def main() -> None:
    backend = AerBackend()
    calibration_trigger = asyncio.Event()
    calibration_policy = CalibrationPolicy(
        freshness=timedelta(seconds=CALIBRATION_FRESHNESS_S),
        reject_error_rate=CALIBRATION_REJECT_ERROR_RATE,
    )
    database_pool = await asyncpg.create_pool(DATABASE_DSN)
    calibration_store = CalibrationStateStore(database_pool)

    kafka_producer = AIOKafkaProducer(bootstrap_servers=KAFKA_BOOTSTRAP_SERVERS)
    await kafka_producer.start()

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
        await declare_wait_queue(channel, TASK_QUEUE_NAME)
        # No explicit bind needed (or allowed): RabbitMQ's default exchange
        # automatically routes to any queue using the queue's own name as
        # the routing key. Declaring the queue is enough; an explicit
        # `queue.bind(channel.default_exchange, ...)` call would actually
        # fail with ACCESS_REFUSED (binding to the default exchange is
        # reserved/automatic, not something a client is allowed to do).
        await channel.declare_queue(RESULTS_QUEUE_NAME, durable=True)

        calibration_task = asyncio.create_task(
            run_calibration_loop(
                backend,
                kafka_producer,
                interval_s=CALIBRATION_INTERVAL_S,
                state_store=calibration_store,
                trigger=calibration_trigger,
            )
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

                    await handle_message(
                        message,
                        backend,
                        channel,
                        kafka_producer,
                        calibration_store,
                        calibration_policy,
                        calibration_trigger,
                    )
        finally:
            calibration_task.cancel()
            try:
                await calibration_task
            except asyncio.CancelledError:
                pass
            await kafka_producer.stop()
            await database_pool.close()


if __name__ == "__main__":
    asyncio.run(main())
