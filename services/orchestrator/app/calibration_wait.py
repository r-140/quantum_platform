"""RabbitMQ TTL queue used to release VQE tasks while calibration is pending."""

from __future__ import annotations

import aio_pika

WAIT_QUEUE_NAME = "experiments.waiting-for-calibration"
WAIT_COUNT_HEADER = "x-calibration-wait-count"
DEFAULT_DELAY_S = 5.0
DEFAULT_MAX_ATTEMPTS = 12


def wait_count(message) -> int:
    return int((message.headers or {}).get(WAIT_COUNT_HEADER, 0))


async def declare_wait_queue(channel, task_queue_name: str) -> None:
    await channel.declare_queue(
        WAIT_QUEUE_NAME,
        durable=True,
        arguments={
            "x-dead-letter-exchange": "",
            "x-dead-letter-routing-key": task_queue_name,
        },
    )


async def reschedule(
    channel,
    message,
    *,
    delay_s: float = DEFAULT_DELAY_S,
) -> int:
    attempt = wait_count(message) + 1
    await channel.default_exchange.publish(
        aio_pika.Message(
            body=message.body,
            headers={**(message.headers or {}), WAIT_COUNT_HEADER: attempt},
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            # aio-pika accepts seconds and converts them to AMQP's
            # millisecond expiration string.
            expiration=delay_s,
        ),
        routing_key=WAIT_QUEUE_NAME,
    )
    return attempt
