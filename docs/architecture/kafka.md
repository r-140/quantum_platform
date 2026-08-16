# Kafka: calibration telemetry

## What changed

From the very first architecture conversation for this project: RabbitMQ
is for the task queue ("run this once"), Kafka is for the telemetry
stream ("lots of events, real-time aggregation"). Up until this point,
`calibration.py` published `error_rate` to a RabbitMQ queue,
`calibration-results`, as an explicitly documented **temporary
stand-in**. Now it's a real Kafka topic, and a new service exists —
`stream-analytics` — a real-time consumer computing a rolling average of
`error_rate` per backend.

As promised in the docs when calibration was first implemented: the
migration didn't require touching `run_calibration()` — only
`publish_calibration_result()` (now `AIOKafkaProducer.send_and_wait()`
instead of publishing to the RabbitMQ queue). All of the error-rate math,
and the Bell-circuit health check itself, are unchanged.

## Kafka in Docker: KRaft mode, no Zookeeper

Added a single Kafka broker to `docker-compose.yml`, in **KRaft** mode
(`broker,controller` in one container) — as of Kafka 3.x/Confluent
Platform 7.5+, Zookeeper is considered a legacy approach, and KRaft is
the standard way to run Kafka locally today. Checked currency via web
search (several independent sources dated 2026 confirm KRaft as the
current standard) — didn't rely on memory here, given how much and how
recently the Kafka deployment architecture has changed.

`CLUSTER_ID` is a fixed string, a widely-used example ID from Confluent's
own docs (not a secret, not environment-specific) — any valid
base64-encoded 16-byte UUID would work.

## `stream-analytics`: why not Kafka Streams/Faust

In the very first sketch of the project structure, `stream-analytics` was
meant to be Kafka Streams or Faust. Instead, it's a simple consumer loop
(`AIOKafkaConsumer`) with manual in-memory aggregation
(`RollingErrorRate`, a `collections.deque` with `maxlen`). This is a
deliberate choice, not a forced one:
- the actual event volume in this project — one calibration cycle per
  `orchestrator` instance every 5 minutes — is tiny by the standards
  Kafka Streams/Faust are built for;
- `deque(maxlen=N)` fully expresses the semantics needed ("rolling
  average over the last N samples") without a third-party framework;
- the `rolling.py` docstring honestly states when this decision should be
  revisited: multiple producers, meaningfully higher volume, or a need
  for the window's state to survive a process restart — which is exactly
  what Kafka Streams' RocksDB-backed state store exists for.

## Verification

As with the algorithm math in `quantum_core`, the `RollingErrorRate`
logic was verified **independently of Kafka** — first as a standalone
script (no pytest, no broker), then moved into the real files
(`app/rolling.py` + `tests/test_rolling.py`) and **run in that form**
(not a draft):

```
test_single_sample_average_equals_itself       PASSED
test_average_over_growing_window                PASSED
test_oldest_sample_evicted_once_window_full     PASSED
test_backends_tracked_independently             PASSED
test_unknown_backend_has_zero_samples           PASSED
```

`test_oldest_sample_evicted_once_window_full` is worth calling out
specifically — it checks that once the window fills up, the oldest
sample gets evicted rather than the window growing without bound; this
is the most substantive part of the logic, and worth verifying
explicitly rather than trusting that `deque(maxlen=...)` "just works"
correctly when combined with the average calculation.

Separately checked the current `aiokafka` API
(`AIOKafkaProducer`/`AIOKafkaConsumer`, `send_and_wait`, `async for msg
in consumer`) via web search against the official docs and PyPI — didn't
rely on memory for this.

⚠️ **Not verified at all**: the `aiokafka` code itself against a real
Kafka, the KRaft Docker configuration (healthcheck,
`KAFKA_ADVERTISED_LISTENERS`, etc.) — I don't have `aiokafka`, Docker, or
network access for full verification. Given that a real run has already
twice in this project (RabbitMQ `bind()`, the Alembic `-m` import)
surfaced things I couldn't have anticipated without a broker/DB on hand
— it's quite likely something similar will show up here on first real
run too (Kafka listener configuration in particular is known for being
finicky on first launch, based on the sources found — "critical setting
is KAFKA_ADVERTISED_LISTENERS; if it points at the wrong host/port,
clients connect once and then fail on broker metadata").

## TimescaleDB: persisting raw calibration events

`RollingErrorRate` lives only in the `stream-analytics` process's memory
— a restart zeroes out the accumulated history.
`app/sinks/timescale_sink.py` closes that gap: every raw calibration
event (not just the current rolling average) is written to TimescaleDB,
into a `calibration_events` hypertable.

A separate database (`timescaledb`, port 5433), not the same Postgres
already used for experiment metadata (`postgres`, port 5432) — a
deliberate split: different purposes (transactional experiment metadata
vs. time-series telemetry), different lifecycles, different access
patterns (upsert by id vs. append-only inserts). The schema (one table +
`create_hypertable`) is created automatically on first container startup
via the `docker-entrypoint-initdb.d` convention — Alembic for a single
table of this kind would be overkill (unlike `api`, where a number of
evolving fields and relationships already justify migrations).

⚠️ **Version risk, caught up front**: TimescaleDB 2.13+ introduced a new
generalized API for `create_hypertable` — `by_range('time')` instead of
the old `create_hypertable('table', 'time')` signature. Found an open bug
(`timescale/timescaledb#6875`) where `by_range()` fails to resolve
depending on `search_path`/image variant. Used the **old** signature —
it's documented as supported for backward compatibility and doesn't show
up in the bug report found.

`asyncpg` is used directly (not through SQLAlchemy, as in `api`) — the
only DB need here is a single append-only insert per event, so a full
ORM would be pure overhead.

**`counts` (the raw measurement histogram) is deliberately not written**
to the hypertable — the table is meant for an aggregate metric over time,
not for duplicating raw data that already lives in the Kafka log itself.
A dedicated test (`test_insert_calibration_event_does_not_forward_counts`)
specifically checks that this doesn't silently change in the future.

### Verification

The logic in `insert_calibration_event` (timestamp parsing, the order of
bound parameters, and the fact that `counts` doesn't end up in the
INSERT) was verified with a hand-written `FakePool` that records
`execute()` calls instead of hitting a real DB. Run against the **real
files** (`timescale_sink.py` + `test_timescale_sink.py`), with a
temporary stub for the `asyncpg` package itself (I don't have it) — 2/2
tests passed.

Separately checked the `datetime.isoformat()` →
`datetime.fromisoformat()` round trip for timezone-aware values — the
exact spot where a naive/aware datetime bug already showed up once (see
`docs/architecture/postgres.md`).

⚠️ **Not verified at all**: the SQL itself against a real TimescaleDB,
the `docker-entrypoint-initdb.d` initialization, the `asyncpg` connection
to the container. I have neither `asyncpg` nor Docker.

## Faust: a real "Kafka Streams for Python"

`stream-analytics/app/consumer.py` (a hand-rolled `aiokafka` consumer)
and `stream-analytics/app/faust_app.py` (new) solve **the same** problem
— a rolling average of `error_rate` per backend — with two different
tools, specifically for comparison. "Kafka Streams" in the strict sense
is a Java/Scala library; the Python equivalent with similar semantics
(tables, windowing, changelog-backed state) is `faust-streaming`, an
actively maintained fork of the original `faust` (Robinhood, abandoned
since 2020).

Both consumers can run **simultaneously** against the same topic — each
has its own consumer group (`stream-analytics` for the hand-rolled one,
`stream-analytics-faust` for Faust), so they don't compete for
partitions.

**Key difference from `RollingErrorRate`**: Faust's windowed `Table` is
changelog-backed. Every table update is additionally written to an
internal Kafka topic (the changelog); on worker restart, Faust replays
that changelog and restores state. `RollingErrorRate` (a plain in-memory
`deque`) has no such property — a restart wipes it completely.
TimescaleDB (see above) closes the same gap a different way — via an
external DB rather than a built-in Kafka mechanism. Three different
answers to "how do we not lose state on restart" in one small project —
a convenient excuse to compare them directly.

Used tumbling-window aggregation (60 seconds, deliberately not matching
the default 300-second calibration interval — so there's something to
observe within a reasonable demo timeframe; most windows will be empty,
and that's expected) — two parallel `Table`s (sum + count) instead of one
composite one, following the pattern from the official Faust docs.

⚠️ **Version risk, checked up front**: `faust-streaming` has had
compatibility issues with newer Python versions in the past (didn't
support 3.10 as of issue #762 in 2022). Official Python 3.12 support was
added in PR #587 — pinned the minimum version
(`faust-streaming>=0.10.21`) explicitly in `requirements.txt`, rather
than assuming "latest" would work.

By default, Faust stores table state in RocksDB (a native C++ extension)
— deliberately switched to `store="memory://"` so the demo/learning
environment doesn't need to build `rocksdb`. This doesn't change the
changelog-backed property (that comes from Kafka, not local storage) —
it only affects where table data lives *between* accesses within a
single running process.

### Running it

```bash
cd services/stream-analytics
source .venv/bin/activate
pip install -r requirements.txt

python3 -m app.faust_app worker -l info
# or, more idiomatic for Faust:
faust -A app.faust_app worker -l info
```

Useful built-in Faust CLI commands (when the worker isn't running):
```bash
faust -A app.faust_app tables    # list tables
faust -A app.faust_app agents    # list agents
```

## Advanced topology: a derived alerts stream

Both `stream-analytics` consumers (hand-rolled and Faust) computed a
rolling/windowed average and only **logged** it — nothing consumed that
output. `faust_app.py` now demonstrates a real "source topic → windowed
aggregate → derived stream" topology: a third table,
`alert_state` (deliberately **not** windowed — hysteresis state should
persist indefinitely per backend, unlike the 60-second rolling window),
tracks per-backend hysteresis via `app/alerting.py`, and every state
*change* (not every sample) is published to a new topic,
`calibration-alerts`.

**Why hysteresis, not a flat threshold check**: `consumer.py`'s existing
`ALERT_THRESHOLD` check flips on every message that crosses it — a
single noisy sample would flap the alert state on and off. `AlertTracker`
(and the underlying pure `step()` function) instead requires
`breach_streak` consecutive samples above the threshold before entering
ALERT, and `recovery_streak` consecutive samples at/below it before
clearing — a standard debounce pattern, and specifically the reason this
is a genuine state machine rather than a stateless per-message check.

`alert_state` being a **Faust `Table`** rather than a plain Python dict
is deliberate, not incidental: like the windowed tables, it's
changelog-backed, so a worker restart replays the changelog and resumes
with the correct streak counters — it doesn't silently forget that a
backend was, say, 2 out of 3 samples into an alert streak.

### Verification

`app/alerting.py`'s hysteresis logic (`step()` / `AlertTracker`) was
verified the same way as `RollingErrorRate` — first as a standalone
script covering 7 scenarios (single breach doesn't trigger; a streak of
breaches triggers on the last one; continued breaches don't re-trigger;
a single recovery sample doesn't clear; a breach mid-recovery correctly
resets the recovery streak; backends are tracked independently;
exactly-at-threshold is not a breach — the boundary is exclusive), then
transcribed into `tests/test_alerting.py` in that same verified form.
The module was refactored once, from three parallel dicts into a single
immutable `AlertState` + pure `step()` function (so the same logic can
drive either a plain dict, via `AlertTracker`, or a Faust `Table`
directly, via `step()`) — all 7 scenarios were re-run against the
refactored version and matched exactly before it was wired into
`faust_app.py`.

⚠️ **Not verified**: the actual `faust.Table`/`app.topic().send()`
wiring in `faust_app.py` against real Kafka/Faust — same caveat as the
rest of this file (no `faust-streaming`, Kafka, or network access in my
environment). Run it and check the `calibration-alerts` topic directly:

```bash
docker exec -it quantum-platform-kafka kafka-console-consumer \
  --bootstrap-server localhost:9092 --topic calibration-alerts --from-beginning
```

**Update, confirmed on first real run**: the windowed-average logic
(`error_rate_sum`/`sample_count` tables, `group_by`, the changelog
mechanism) ran correctly end to end against real Kafka — the very first
`[faust] backend=... window_avg(...)=...` log line came out exactly as
expected. Immediately after, the worker crashed with
`PartitionsMismatch` on the two newest tables
(`baseline_stats`, `drift_alert_state`, added for the drift-detection
work below) — a **topic-metadata propagation race**, not a logic bug:
those two changelog topics were created only ~100ms before the consumer
group's metadata fetch, on a single-broker KRaft setup that hadn't yet
propagated them internally, so Faust briefly saw them as "0 partitions"
and the first write to `baseline_stats` tripped the mismatch check.
Restarting the worker (the topics genuinely exist by then) resolves it.
If it recurs consistently rather than just on a topic's very first
creation, pre-create the changelog topics manually with the matching
partition count before starting Faust:

```bash
docker exec -it quantum-platform-kafka kafka-topics --create \
  --topic stream-analytics-faust-baseline_stats-changelog \
  --partitions 8 --replication-factor 1 --bootstrap-server localhost:9092
docker exec -it quantum-platform-kafka kafka-topics --create \
  --topic stream-analytics-faust-drift_alert_state-changelog \
  --partitions 8 --replication-factor 1 --bootstrap-server localhost:9092
```

Given `AerBackend` is noiseless (`error_rate` stays ~0), you shouldn't
expect to see anything on `calibration-alerts` under normal operation —
that's expected, not a sign of something broken; the state machine
itself is what's been verified, not "real" drift, which doesn't exist
yet without a noise model. To see a transition fire for real, the
easiest path is a temporary manual test: drive `step()`/`AlertTracker`
directly with synthetic values above `DEFAULT_THRESHOLD` (as the tests
already do), rather than trying to provoke a real breach out of a
noiseless simulator.

## Advanced topology: statistical drift detection

A second, independent derived stream, answering a different question
than the flat-threshold alert above: not "is `error_rate` above 0.05"
but "is this backend behaving differently from its own history" — the
actual definition of drift.

`app/drift.py` maintains an all-time running mean/stddev of
`error_rate` per backend via **Welford's online algorithm** (Welford
1962), rather than the naive "keep a growing list, call
`statistics.stdev()`" approach — Welford's algorithm is O(1) per sample
in both time and memory, which matters here because this baseline is
meant to accumulate over a backend's *entire* history, not a bounded
recent window like the tumbling tables. Every raw sample's z-score
against that baseline (computed against the baseline **before** folding
the new sample in — scoring against a baseline that already includes
the point itself would damp a true outlier's own z-score, especially
early on when there's little history yet) is checked through the same
hysteresis machinery from the alerting section above
(`app.alerting.step()`, reused generically — it doesn't care whether the
"value" it's watching is a raw error rate or a z-score), via a second,
separate state table (`drift_alert_state`) and a second derived topic,
`calibration-drift-alerts`.

Two entirely separate state tables (`alert_state` for the flat
threshold, `drift_alert_state` for z-score drift) rather than combining
them — they answer genuinely different questions and evolve
independently: a backend can drift meaningfully from its own baseline
while still sitting under the flat 0.05 threshold, or the reverse.

### Verification

`app/drift.py`'s Welford implementation was checked directly against
Python's `statistics.mean`/`statistics.stdev` on an 80-sample random
series — matched to 1e-9 (this is the same kind of independent-of-Kafka
verification used for `RollingErrorRate` and `AlertTracker`). Also
verified: a genuine outlier (0.10) against a tight synthetic baseline
(~0.02 ± 0.001) scores a z-score in the hundreds, comfortably above the
default threshold of 3.0; a value close to the mean scores well under
1.0; and — a case worth calling out specifically — a **constant-input
baseline has zero variance, and `zscore()` returns `None` rather than
an infinite or undefined number in that case**, which is exactly what
`AerBackend`'s currently-noiseless `error_rate` looks like (see
`calibration.py`'s "Honest limitation") — so on the current stack, no
drift alert will fire, which is correct behavior given the input, not a
sign the feature is broken. All 6 scenarios were transcribed into
`tests/test_drift.py` in their verified form.

⚠️ **Not verified**: the Faust `Table`/topic wiring in `faust_app.py`
against real Kafka/Faust — same caveat as everywhere else in this
document. Check the topic the same way as `calibration-alerts`:

```bash
docker exec -it quantum-platform-kafka kafka-console-consumer \
  --bootstrap-server localhost:9092 --topic calibration-drift-alerts --from-beginning
```

## How to run it

Already wired into `./dev.sh` — it brings up Kafka and TimescaleDB
alongside RabbitMQ/Postgres, waits for both healthchecks, and starts
`stream-analytics` as a third service.

Manually:

```bash
docker compose up -d kafka timescaledb

cd services/orchestrator
source .venv/bin/activate
pip install -r requirements.txt
python3 -m app.worker          # publishes calibration-results to Kafka every 5 minutes

# in another terminal
cd services/stream-analytics
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python3 -m app.consumer        # listens, logs the rolling average, writes to TimescaleDB
```

Checking the topic directly (no Python, via the container itself):
```bash
docker exec -it quantum-platform-kafka kafka-console-consumer \
  --bootstrap-server localhost:9092 --topic calibration-results --from-beginning
```

Checking the data in TimescaleDB directly:
```bash
docker exec -it quantum-platform-timescaledb psql -U quantum -d telemetry \
  -c "SELECT * FROM calibration_events ORDER BY time DESC LIMIT 10;"
```

## Not yet implemented

- `ALERT_THRESHOLD = 0.05` in `stream-analytics/app/consumer.py`, and
  `DRIFT_ZSCORE_THRESHOLD`/`DEFAULT_THRESHOLD` in the Faust app, are
  essentially placeholders: without a noise model on `AerBackend` there
  is no real `error_rate` distribution to calibrate any of them
  against — every threshold here is a reasonable-looking guess, not a
  tuned value;
- Nothing yet consumes `calibration-alerts` or `calibration-drift-alerts`
  besides the manual `kafka-console-consumer` check above — no
  notification channel (Slack, email, PagerDuty) is wired up, and
  nothing writes these alert events to TimescaleDB the way raw
  calibration events are;
- Multiple consumers within the same consumer group
  (`stream-analytics`) for horizontal scaling — not needed yet at the
  current volume;
- Queries against `calibration_events` (trends, dashboards) aren't used
  by anything yet — the table is being populated, but nothing reads it
  besides the manual `psql` check above.
