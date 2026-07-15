"""
Redelivery/retry policy for the orchestrator -- a different concern from
`quantum_core.sync.polling`'s retry/backoff.

polling.py retries individual *backend calls* within a single task's
execution (submit/poll/fetch against a QuantumBackend that might be
transiently flaky) -- the task's own code is assumed fine, only the
backend is unreliable. This module handles the opposite case: a task
message that fails to reach a definitive completed/failed result because
something went wrong at the *worker* level (the process crashed, the
connection dropped mid-task) before it could ack/reject at all. No amount
of backend-level retry logic fixes that, since the problem isn't the
backend.

Design choice: retry count is tracked via a custom message header
(`x-retry-count`) that this module manages directly in application code,
rather than RabbitMQ's automatic dead-lettering/x-death mechanism (a
TTL+DLX "retry loop", the other standard way to do this). Both are valid;
this one keeps the logic fully in Python -- easier to reason about, test,
and explain without a live broker to experiment against -- at the cost of
one extra republish step and tying up this worker's event loop for the
backoff delay, rather than freeing the message for another worker to pick
up in the meantime. Revisit if that trade-off stops being acceptable
(e.g. once there are multiple orchestrator instances and a slow retry
shouldn't block one of them).

Only applies to messages RabbitMQ marks `redelivered=True` -- i.e.
messages that were delivered once already and not acked/rejected before
the connection or consumer went away. A task that runs to completion and
is explicitly recorded as FAILED (see worker.py's handle_message) is NOT
retried by this policy -- that's a definitive answer, not a crash.
"""

from __future__ import annotations

import asyncio
import logging

import aio_pika
from aio_pika.abc import AbstractChannel, AbstractIncomingMessage

logger = logging.getLogger("orchestrator.retry_policy")

MAX_RETRIES = 3
BASE_DELAY_S = 2.0
BACKOFF_FACTOR = 2.0
DEAD_LETTER_QUEUE_NAME = "experiments.dlq"
RETRY_COUNT_HEADER = "x-retry-count"


def get_retry_count(message: AbstractIncomingMessage) -> int:
    headers = message.headers or {}
    return int(headers.get(RETRY_COUNT_HEADER, 0))


def compute_backoff_delay(retry_count: int) -> float:
    """retry_count=1 -> BASE_DELAY_S, doubling each attempt after that.
    Verified standalone (no aio-pika needed) before being wired in here --
    see docs/architecture/orchestration.md.
    """
    return BASE_DELAY_S * (BACKOFF_FACTOR ** (retry_count - 1))


async def schedule_retry(
    channel: AbstractChannel,
    message: AbstractIncomingMessage,
    task_queue_name: str,
) -> None:
    """Republishes `message` to `task_queue_name` with an incremented
    retry-count header, after an exponential-backoff delay.

    Publishes a *new* message rather than nack/requeue-ing the original --
    this is deliberate: the republished copy has `redelivered=False` from
    RabbitMQ's perspective (it's a fresh delivery), so it flows through
    normal processing in worker.py without re-triggering this retry path
    unless *it* also fails to be acked/rejected. Caller is responsible for
    ack-ing the original message; this function only publishes the retry
    copy.
    """
    retry_count = get_retry_count(message) + 1
    delay = compute_backoff_delay(retry_count)

    logger.warning(
        "worker-crash recovery: scheduling retry %d/%d, delay=%.1fs",
        retry_count,
        MAX_RETRIES,
        delay,
    )
    await asyncio.sleep(delay)

    await channel.default_exchange.publish(
        aio_pika.Message(
            body=message.body,
            headers={**(message.headers or {}), RETRY_COUNT_HEADER: retry_count},
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
        ),
        routing_key=task_queue_name,
    )


async def send_to_dead_letter_queue(channel: AbstractChannel, message: AbstractIncomingMessage) -> None:
    """Publishes an exhausted (retried MAX_RETRIES times) message to the
    dead-letter queue for manual inspection, rather than silently
    discarding it -- this also covers malformed/unrecognized messages that
    worker.py rejects outright (see handle_message), which previously had
    nowhere to go and were simply dropped.
    """
    logger.error(
        "task message exceeded %d retries (or was malformed) -- sending to %s",
        MAX_RETRIES,
        DEAD_LETTER_QUEUE_NAME,
    )
    await channel.declare_queue(DEAD_LETTER_QUEUE_NAME, durable=True)
    await channel.default_exchange.publish(
        aio_pika.Message(
            body=message.body,
            headers=message.headers,
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
        ),
        routing_key=DEAD_LETTER_QUEUE_NAME,
    )


async def handle_redelivery(
    channel: AbstractChannel,
    message: AbstractIncomingMessage,
    task_queue_name: str,
) -> bool:
    """Call this first for every message pulled off the task queue.

    Returns True if the caller should proceed with normal processing (a
    fresh message, or a retry copy this policy itself published -- both
    have `redelivered=False`). Returns False if this function has already
    handled the message (scheduled a retry, or sent it to the dead-letter
    queue) -- the caller should just `ack()` the original and move on, not
    process it further.
    """
    if not message.redelivered:
        return True

    retry_count = get_retry_count(message)
    if retry_count >= MAX_RETRIES:
        await send_to_dead_letter_queue(channel, message)
        return False

    await schedule_retry(channel, message, task_queue_name)
    return False