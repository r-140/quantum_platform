"""
Faust-streaming implementation of the same "rolling error rate" idea as
consumer.py (the hand-rolled aiokafka consumer) -- built to get hands-on
with Faust/"Kafka Streams for Python" concepts specifically (typed
Records, windowed Tables, changelog-backed state, `group_by`
repartitioning), not because this project's actual event volume needs it.
See rolling.py's docstring for the argument that a plain in-memory deque
is enough at this project's scale -- that argument still holds; this file
exists to demonstrate the heavier tool, not to replace the lighter one.
Both can run at the same time against the same topic: each subscribes
under its own Kafka consumer group (`stream-analytics` for consumer.py,
`stream-analytics-faust` here), so they don't interfere.

Key difference from consumer.py's RollingErrorRate: Faust's windowed Table
is changelog-backed -- every update is also written to an internal Kafka
changelog topic, so this table's state survives a worker restart (Faust
replays the changelog on startup). RollingErrorRate's plain deque has no
such property; TimescaleDB (see sinks/timescale_sink.py) closes that same
gap a different way, via an external database rather than Kafka's own
changelog mechanism. Three different answers to "how do you not lose
state on restart" in one small project, each illustrating a different
trade-off -- worth comparing directly if this comes up in an interview.

Run with (from services/stream-analytics/):
    python3 -m app.faust_app worker -l info
or, the more idiomatic Faust convention:
    faust -A app.faust_app worker -l info
(both work -- `app.main()` at the bottom of this file makes this module
behave as the full Faust CLI, so `python3 -m app.faust_app` accepts the
same subcommands as the `faust` console script, e.g. `... agents`,
`... tables`, `... reset`.)
"""

from __future__ import annotations

import logging
import os
from datetime import timedelta

import faust

logger = logging.getLogger("stream-analytics.faust")

KAFKA_BOOTSTRAP_SERVERS = os.environ.get("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
CALIBRATION_TOPIC = "calibration-results"

# Tumbling window: fixed-size, non-overlapping, contiguous intervals.
# 60s (rather than something closer to CALIBRATION_INTERVAL_S's default of
# 300s) so there's usually something to observe within a reasonable demo
# timeframe -- most 60s windows will simply be empty between calibration
# cycles, which is expected, not a bug (see docs/architecture/kafka.md).
WINDOW_SIZE_S = 60.0
WINDOW_EXPIRES_S = 300.0

app = faust.App(
    "stream-analytics-faust",
    broker=f"kafka://{KAFKA_BOOTSTRAP_SERVERS}",
    # Plain in-memory table storage, not the RocksDB-backed default --
    # avoids requiring the `rocksdb` native C++ extension to be built for
    # what's a learning/demo deployment, not a production one. Table state
    # is still changelog-backed regardless of this setting (that property
    # comes from Kafka, not from the local storage backend) -- this only
    # affects whether a *running* worker keeps table data in a Python dict
    # vs. persisted to local disk between restarts of the same instance.
    store="memory://",
)


class CalibrationEvent(faust.Record, serializer="json"):
    """Must match the JSON shape orchestrator/app/tasks/calibration.py's
    CalibrationResult.to_json() produces. `counts` is deliberately omitted
    -- Faust would still parse a message containing an unlisted extra
    field without error (a Record only declares the fields it cares
    about), so leaving it out here isn't a compatibility risk.
    """

    timestamp: str
    backend_name: str
    error_rate: float
    shots: int


calibration_topic = app.topic(CALIBRATION_TOPIC, value_type=CalibrationEvent)

# Two parallel windowed tables (running sum, running count) rather than one
# table storing a composite value -- keeps each table's `default` a plain
# float/int, mirroring the per-key aggregation pattern used throughout
# Faust's own documentation examples.
error_rate_sum = app.Table("error_rate_sum", default=float).tumbling(
    WINDOW_SIZE_S, expires=timedelta(seconds=WINDOW_EXPIRES_S)
)
sample_count = app.Table("sample_count", default=int).tumbling(
    WINDOW_SIZE_S, expires=timedelta(seconds=WINDOW_EXPIRES_S)
)


@app.agent(calibration_topic)
async def process_calibration_event(stream):
    # group_by repartitions the stream by backend_name -- required for
    # correctness once there's more than one partition/worker (guarantees
    # all events for the same backend land on the worker instance holding
    # that key's table partition; see Faust's tables/windowing docs on
    # this exact pitfall). With this project's single-partition topic it's
    # a no-op in practice today, but it's the correct pattern to have in
    # place regardless of today's scale.
    async for event in stream.group_by(CalibrationEvent.backend_name):
        error_rate_sum[event.backend_name] += event.error_rate
        sample_count[event.backend_name] += 1

        total = error_rate_sum[event.backend_name].now()
        count = sample_count[event.backend_name].now()
        window_avg = total / count if count else 0.0

        logger.info(
            "[faust] backend=%s error_rate=%.4f window_avg(%.0fs, n=%d)=%.4f",
            event.backend_name,
            event.error_rate,
            WINDOW_SIZE_S,
            count,
            window_avg,
        )


if __name__ == "__main__":
    app.main()