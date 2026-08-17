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

Advanced topology: derived alerts stream
-----------------------------------------
Beyond the windowed rolling average (which used to only log), this app
now demonstrates a genuine "source topic -> windowed aggregate ->
derived stream" topology: a third, *non-windowed* Table (`alert_state`)
tracks per-backend hysteresis state (see app/alerting.py -- a streak of
consecutive breaches before alerting, a streak of consecutive OK
samples before clearing, so one noisy sample can't flip the state).
Every state *change* (not every sample) is published to a new topic,
`calibration-alerts`, via `AlertEvent`. `alert_state` being a Faust
Table rather than a plain dict is deliberate: it's changelog-backed like
the windowed tables, so a worker restart resumes with the correct
streak counters instead of silently forgetting how close a backend was
to alerting.

Advanced topology: statistical drift detection
------------------------------------------------
A second, independent derived stream: `baseline_stats` (another
non-windowed Table) maintains an all-time running mean/stddev per
backend via Welford's online algorithm (see app/drift.py), and every raw
sample's z-score against that baseline is checked through the same
hysteresis machinery (app.alerting.step()) used for the flat-threshold
alerts, via a second, separate state table (`drift_alert_state`) and a
second derived topic, `calibration-drift-alerts`. This answers a
different question than the flat threshold does: not "is error_rate
above 0.05" but "is this backend behaving differently from its own
history" -- the literal definition of drift, and the reason this exists
alongside, not instead of, the flat-threshold alerting above.

Dashboard
---------
`GET /api/state` (JSON snapshot: per-backend window_avg, alert/drift
levels and streak counters, baseline mean/stddev, plus a recent-events
feed) and `GET /dashboard/` (a React UI consuming it) are served from
this same Faust worker's already-running built-in web server (the one
bound to port 6066 by default) -- same-origin, no CORS, no second
service. The frontend is plain React loaded via CDN with in-browser
Babel for JSX, not a bundled/Vite build -- deliberately, to keep the
"no npm, no build step" property the experiments dashboard has (see
docs/architecture/dashboard.md), while still being genuine composable
React rather than one more hand-rolled vanilla-JS file, specifically so
new per-metric widgets can be added as new components as this project's
Faust analytics grow. See static/dashboard/index.html.
"""

from __future__ import annotations

import logging
import os
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

import faust

from app.alerting import (
    DEFAULT_BREACH_STREAK,
    DEFAULT_RECOVERY_STREAK,
    DEFAULT_THRESHOLD,
    AlertLevel,
    AlertState,
    step,
)
from app.drift import WelfordStats, update as welford_update, zscore

logger = logging.getLogger("stream-analytics.faust")

KAFKA_BOOTSTRAP_SERVERS = os.environ.get("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
CALIBRATION_TOPIC = "calibration-results"
ALERTS_TOPIC = "calibration-alerts"
DRIFT_ALERTS_TOPIC = "calibration-drift-alerts"

# In-memory only (not changelog-backed like the Tables below) -- this is
# purely a "what just happened" feed for the dashboard, not a source of
# truth for anything the alerting/drift logic depends on. Losing it on
# restart is fine; the actual hysteresis state (alert_state,
# drift_alert_state) is unaffected, since it lives in real Faust Tables.
RECENT_EVENTS_MAXLEN = 50
recent_events: deque[dict] = deque(maxlen=RECENT_EVENTS_MAXLEN)

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
DASHBOARD_HTML_PATH = STATIC_DIR / "dashboard" / "index.html"


def _record_event(kind: str, backend_name: str, level: str, **extra: float) -> None:
    recent_events.appendleft(
        {
            "kind": kind,  # "alert" or "drift"
            "backend_name": backend_name,
            "level": level,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **extra,
        }
    )

# Z-score hysteresis is deliberately separate from the flat-threshold
# alerting in alerting.py -- a z-score of 3+ is already a fairly strong
# "this differs from the backend's own history" signal on its own, so a
# shorter debounce (2, not 3) is enough to avoid single-sample flapping
# without being sluggish to react.
DRIFT_ZSCORE_THRESHOLD = 3.0
DRIFT_BREACH_STREAK = 2
DRIFT_RECOVERY_STREAK = 2

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


class AlertEvent(faust.Record, serializer="json"):
    """Published to `calibration-alerts` only on a hysteresis boundary
    crossing (see app/alerting.py) -- not one message per calibration
    event. `level` is the string value of AlertLevel ("ok"/"alert"), so
    a plain external Kafka consumer (e.g. `kafka-console-consumer`, or a
    future notifier that doesn't import this project's Python at all)
    can read it with zero coupling to this module's enum type.
    """

    backend_name: str
    level: str
    window_avg: float
    threshold: float


class AlertStateRecord(faust.Record, serializer="json"):
    """The Kafka-serializable mirror of app.alerting.AlertState -- a
    faust.Record needs concrete field types for its changelog encoding,
    so `level` is stored as AlertLevel's string value rather than the
    enum member itself. Converted to/from AlertState at the table
    read/write boundary in `process_calibration_event` below.
    """

    level: str = AlertLevel.OK.value
    consecutive_breaches: int = 0
    consecutive_ok: int = 0


class WelfordRecord(faust.Record, serializer="json"):
    """The Kafka-serializable mirror of app.drift.WelfordStats. Unlike
    AlertStateRecord, no enum conversion is needed -- count/mean/m2 are
    already plain JSON-friendly types.
    """

    count: int = 0
    mean: float = 0.0
    m2: float = 0.0


class DriftAlertEvent(faust.Record, serializer="json"):
    """Published to `calibration-drift-alerts` only on a z-score
    hysteresis boundary crossing -- distinct from `AlertEvent`
    (calibration-alerts), which fires off a flat threshold on the
    windowed average. This one fires off "how unusual is this sample
    relative to this backend's own history", which is a different
    question with a different answer, even though both ultimately watch
    the same underlying error_rate signal.
    """

    backend_name: str
    level: str
    zscore: float
    error_rate: float
    baseline_mean: float
    baseline_stddev: float


calibration_topic = app.topic(CALIBRATION_TOPIC, value_type=CalibrationEvent)
alerts_topic = app.topic(ALERTS_TOPIC, value_type=AlertEvent)
drift_alerts_topic = app.topic(DRIFT_ALERTS_TOPIC, value_type=DriftAlertEvent)

# Two parallel windowed tables (running sum, running count) rather than one
# table storing a composite value -- keeps each table's `default` a plain
# float/int, mirroring the per-key aggregation pattern used throughout
# Faust's own documentation examples.
error_rate_sum = app.Table(
    "error_rate_sum", default=float, help="Sum of error_rate per backend within the current 60s tumbling window"
).tumbling(WINDOW_SIZE_S, expires=timedelta(seconds=WINDOW_EXPIRES_S))
sample_count = app.Table(
    "sample_count", default=int, help="Number of calibration samples per backend within the current 60s tumbling window"
).tumbling(WINDOW_SIZE_S, expires=timedelta(seconds=WINDOW_EXPIRES_S))

# NOT windowed -- unlike the two tables above, alert hysteresis state is
# meant to persist indefinitely per backend (a "currently alerting"
# flag doesn't expire on a timer the way a 60s rolling window does), so
# this is a plain (non-tumbling/non-hopping) Faust Table. It's still
# changelog-backed like the windowed tables above -- a worker restart
# replays the changelog and resumes with the correct alert state and
# streak counters, rather than silently resetting to OK and needing a
# fresh breach_streak worth of samples to re-alert.
alert_state = app.Table(
    "alert_state",
    default=AlertStateRecord,
    help="Flat-threshold hysteresis state (ok/alert + streak counters) per backend, on window_avg",
)

# Also NOT windowed, for the same reason as alert_state above -- a
# baseline "what does normal look like for this backend" should
# accumulate over the backend's entire history, not reset every 60s the
# way the tumbling tables do. Two separate non-windowed tables (rather
# than combining alert_state and baseline_stats into one) because they
# answer genuinely different questions (flat-threshold breach vs.
# statistical deviation from history) and evolve independently -- a
# backend can drift from its own baseline while still being under the
# flat 0.05 threshold, or vice versa.
baseline_stats = app.Table(
    "baseline_stats",
    default=WelfordRecord,
    help="All-time running mean/stddev of error_rate per backend (Welford's algorithm)",
)
drift_alert_state = app.Table(
    "drift_alert_state",
    default=AlertStateRecord,
    help="Z-score hysteresis state (ok/alert + streak counters) per backend, on drift from baseline_stats",
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

        # Alert hysteresis: derive a new stream (calibration-alerts) from
        # the windowed aggregate above, rather than alerting off the raw
        # per-event error_rate -- this is the "source topic -> windowed
        # table -> derived stream" topology the tumbling tables alone
        # didn't demonstrate on their own (they only logged; nothing
        # consumed their output). See app/alerting.py for why this needs
        # a streak, not a flat threshold check.
        record = alert_state[event.backend_name]
        prev_state = AlertState(
            level=AlertLevel(record.level),
            consecutive_breaches=record.consecutive_breaches,
            consecutive_ok=record.consecutive_ok,
        )
        new_state, transition = step(
            prev_state,
            window_avg,
            threshold=DEFAULT_THRESHOLD,
            breach_streak=DEFAULT_BREACH_STREAK,
            recovery_streak=DEFAULT_RECOVERY_STREAK,
            backend_name=event.backend_name,
        )
        alert_state[event.backend_name] = AlertStateRecord(
            level=new_state.level.value,
            consecutive_breaches=new_state.consecutive_breaches,
            consecutive_ok=new_state.consecutive_ok,
        )

        if transition is not None:
            logger.warning(
                "[faust] ALERT TRANSITION backend=%s -> %s window_avg=%.4f threshold=%.4f",
                transition.backend_name,
                transition.level.value,
                transition.value,
                transition.threshold,
            )
            _record_event(
                "alert",
                transition.backend_name,
                transition.level.value,
                window_avg=transition.value,
                threshold=transition.threshold,
            )
            await alerts_topic.send(
                key=event.backend_name,
                value=AlertEvent(
                    backend_name=transition.backend_name,
                    level=transition.level.value,
                    window_avg=transition.value,
                    threshold=transition.threshold,
                ),
            )

        # Statistical drift: check this raw error_rate sample against the
        # backend's baseline *before* folding it in (comparing a new
        # point to what's already been seen, not to a baseline that
        # already includes the point itself -- folding first would damp
        # a true outlier's own z-score, especially with few samples of
        # history so far), then update the baseline via Welford's
        # algorithm (O(1) per sample -- see app/drift.py) for next time.
        welford_record = baseline_stats[event.backend_name]
        prev_welford = WelfordStats(
            count=welford_record.count, mean=welford_record.mean, m2=welford_record.m2
        )
        z = zscore(prev_welford, event.error_rate)

        new_welford = welford_update(prev_welford, event.error_rate)
        baseline_stats[event.backend_name] = WelfordRecord(
            count=new_welford.count, mean=new_welford.mean, m2=new_welford.m2
        )

        if z is not None:
            drift_record = drift_alert_state[event.backend_name]
            prev_drift_state = AlertState(
                level=AlertLevel(drift_record.level),
                consecutive_breaches=drift_record.consecutive_breaches,
                consecutive_ok=drift_record.consecutive_ok,
            )
            new_drift_state, drift_transition = step(
                prev_drift_state,
                abs(z),
                threshold=DRIFT_ZSCORE_THRESHOLD,
                breach_streak=DRIFT_BREACH_STREAK,
                recovery_streak=DRIFT_RECOVERY_STREAK,
                backend_name=event.backend_name,
            )
            drift_alert_state[event.backend_name] = AlertStateRecord(
                level=new_drift_state.level.value,
                consecutive_breaches=new_drift_state.consecutive_breaches,
                consecutive_ok=new_drift_state.consecutive_ok,
            )

            if drift_transition is not None:
                logger.warning(
                    "[faust] DRIFT TRANSITION backend=%s -> %s zscore=%.2f "
                    "error_rate=%.4f baseline_mean=%.4f baseline_stddev=%.4f",
                    drift_transition.backend_name,
                    drift_transition.level.value,
                    z,
                    event.error_rate,
                    new_welford.mean,
                    new_welford.stddev,
                )
                _record_event(
                    "drift",
                    drift_transition.backend_name,
                    drift_transition.level.value,
                    zscore=z,
                    error_rate=event.error_rate,
                    baseline_mean=new_welford.mean,
                    baseline_stddev=new_welford.stddev,
                )
                await drift_alerts_topic.send(
                    key=event.backend_name,
                    value=DriftAlertEvent(
                        backend_name=event.backend_name,
                        level=drift_transition.level.value,
                        zscore=z,
                        error_rate=event.error_rate,
                        baseline_mean=new_welford.mean,
                        baseline_stddev=new_welford.stddev,
                    ),
                )


@app.page("/api/state")
async def get_state(self, request):
    """Snapshot of current per-backend state, for the dashboard's poll
    loop -- reads directly from the live Tables (alert_state,
    drift_alert_state, baseline_stats) plus the two windowed tables for
    the current window_avg, rather than keeping a separate cached copy.
    `alert_state` is used as the backend registry (every backend that's
    ever produced a calibration event has an entry there, written
    unconditionally on every event) -- there's no dedicated "list of
    known backends" anywhere else.

    ⚠️ Unverified against real Faust: `Table.items()` iterating a
    non-windowed table's current key/value pairs is a standard,
    documented Faust pattern (used in Faust's own tutorial, e.g. a word
    count table view) but hasn't been run in this project -- no Faust
    install in my environment. If this route 404s or returns empty
    despite the worker log showing processed events, this is the first
    place to look.
    """
    backends = []
    for backend_name, alert_record in alert_state.items():
        welford_record = baseline_stats[backend_name]
        drift_record = drift_alert_state[backend_name]
        count = sample_count[backend_name].now()
        total = error_rate_sum[backend_name].now()
        window_avg = total / count if count else 0.0
        baseline = WelfordStats(
            count=welford_record.count, mean=welford_record.mean, m2=welford_record.m2
        )
        backends.append(
            {
                "backend_name": backend_name,
                "window_avg": window_avg,
                "window_sample_count": count,
                "alert_level": alert_record.level,
                "alert_consecutive_breaches": alert_record.consecutive_breaches,
                "alert_consecutive_ok": alert_record.consecutive_ok,
                "baseline_mean": baseline.mean,
                "baseline_stddev": baseline.stddev,
                "baseline_count": baseline.count,
                "drift_level": drift_record.level,
                "drift_consecutive_breaches": drift_record.consecutive_breaches,
                "drift_consecutive_ok": drift_record.consecutive_ok,
            }
        )

    return self.json(
        {
            "backends": backends,
            "recent_events": list(recent_events),
            "alert_threshold": DEFAULT_THRESHOLD,
            "drift_zscore_threshold": DRIFT_ZSCORE_THRESHOLD,
        }
    )


@app.page("/dashboard/")
async def get_dashboard(self, request):
    """Serves the React (CDN + in-browser Babel, no build step -- see
    static/dashboard/index.html for the rationale) dashboard, same-origin
    with /api/state so no CORS setup is needed -- mirroring why the
    experiments dashboard is served from `api` itself rather than a
    separate frontend host (see docs/architecture/dashboard.md).
    """
    html = DASHBOARD_HTML_PATH.read_text(encoding="utf-8")
    return self.text(html, content_type="text/html")


if __name__ == "__main__":
    app.main()