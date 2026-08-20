# Extending VQE stream analytics with Faust

This is a learning roadmap for extending the existing VQE window topology. It
is intentionally a set of implementation hints rather than a finished patch.
The goal is to explore the ideas behind Faust and Kafka Streams: keyed state,
window semantics, repartitioning, derived streams, joins, event time,
deduplication, and replay.

The current implementation already provides a useful starting point:

```text
vqe-iteration-metrics
    -> group_by(experiment_id)
    -> vqe_window_state: 60-second tumbling Faust Table
    -> vqe-window-metrics
    -> plain Kafka consumer
    -> TimescaleDB vqe_window_metrics
    -> Grafana
```

The relevant code is concentrated in:

- `services/stream-analytics/app/faust_app.py` — Faust records, topics,
  tables, agents, and derived-event publication;
- `services/stream-analytics/app/consumer.py` — durable sink consumer;
- `services/stream-analytics/app/sinks/timescale_sink.py` — TimescaleDB
  inserts;
- `services/stream-analytics/init/003_create_vqe_window_metrics_hypertable.sql`
  — derived-metric schema;
- `infra/grafana/provisioning/dashboards/vqe-window-overview.json` — Grafana
  dashboard;
- `services/stream-analytics/tests/` — aggregation and sink tests.

## Faust tables versus Grafana tables

These are two different kinds of table.

### Faust Table

A Faust `Table` is local processor state, partitioned by Kafka key and backed
by a Kafka changelog. The worker uses it while evaluating the stream:

```python
vqe_window_state = app.Table(
    "vqe_window_state",
    default=VQEWindowState,
).tumbling(
    WINDOW_SIZE_S,
    expires=timedelta(seconds=WINDOW_EXPIRES_S),
)
```

It is not the appropriate Grafana data source. Its contents are distributed
according to Kafka partitions, retained according to window expiry, and owned
by the stream processor.

### TimescaleDB table

Grafana should continue querying TimescaleDB. The normal path is:

1. Faust updates its internal table.
2. Faust publishes a derived Kafka record.
3. `app.consumer` consumes the derived topic.
4. `timescale_sink.py` persists it.
5. Grafana queries the durable hypertable.

This separation keeps processing state out of the presentation layer and
allows Kafka replay to reconstruct the analytical database.

### When to extend or create a TimescaleDB table

| Change | Storage recommendation |
|---|---|
| New scalar for the existing per-experiment 60-second window | Add a column to `vqe_window_metrics` |
| Variance, slope, improvement, or state for that same window grain | Usually add columns to `vqe_window_metrics` |
| Hopping windows alongside tumbling windows | Prefer a separate `vqe_hopping_window_metrics` table, or add explicit `window_type`, `window_start`, `window_end`, and `window_step_s` dimensions |
| Aggregate by molecule/backend instead of experiment | Use a separate table because the key and grain are different |
| Convergence state transitions | Use a separate append-only alert/transition table if historical queries are required |
| Deduplication identities | Keep them in Faust state; do not expose them to Grafana |
| Experiment metadata | Use a compacted Kafka topic/Faust table for stream enrichment; persist selected dimensions with the Timescale metric rows for Grafana |

Do not put values with different grains into the same table without explicit
dimensions. A row keyed by `experiment_id + 60-second window` and a row keyed
by `molecule + 10-minute window` do not represent the same fact.

The SQL files in `init/` run only when the TimescaleDB volume is initially
created. During development, either recreate the volume with `./dev.sh
--clean` or apply the new `ALTER TABLE`/`CREATE TABLE` statement manually.
For a longer-lived project, introduce real TimescaleDB migrations rather than
relying only on container initialization scripts.

## Exercise 1: energy improvement

Add:

```text
first_energy
energy_improvement = first_energy - best_energy
relative_energy_improvement = energy_improvement / abs(first_energy)
```

Lower energy is better, so a positive `energy_improvement` means that the
optimizer found a better state during the window.

### Faust implementation hints

Extend `VQEWindowState`:

```python
class VQEWindowState(faust.Record, serializer="json"):
    iteration_count: int = 0
    first_energy: float = 0.0
    # existing fields...
```

In `process_vqe_iteration`, capture the first value before incrementing the
count:

```python
state = vqe_window_state[experiment_id].now()

if state.iteration_count == 0:
    state.first_energy = event.energy

state.iteration_count += 1
state.best_energy = min(state.best_energy, event.energy)
```

Be careful with `best_energy` initialization. Zero is not a safe sentinel for
negative molecular energies. Use the first event to initialize both
`first_energy` and `best_energy`, or make initialization explicit in the
update function.

Then extend `VQEWindowMetricsEvent`, the Kafka send call, sink SQL, table
schema, consumer log, tests, and Grafana panel.

This exercise demonstrates order-sensitive aggregation: unlike a sum,
“first” depends on event ordering.

## Exercise 2: energy variance and stability

Average and best energy do not show whether the optimizer is stable. Add:

```text
energy_variance
energy_stddev
```

Use Welford's online algorithm instead of retaining every energy:

```python
class VQEWindowState(faust.Record, serializer="json"):
    energy_count: int = 0
    energy_mean: float = 0.0
    energy_m2: float = 0.0
```

For every event:

```python
state.energy_count += 1
delta = event.energy - state.energy_mean
state.energy_mean += delta / state.energy_count
delta2 = event.energy - state.energy_mean
state.energy_m2 += delta * delta2

variance = (
    state.energy_m2 / state.energy_count
    if state.energy_count > 0
    else 0.0
)
stddev = variance ** 0.5
```

Decide whether you want population variance (`M2 / n`) or sample variance
(`M2 / (n - 1)`) and document it. For “describe all iterations in this
window,” population variance is the natural choice.

There is already a reusable Welford implementation in `app/drift.py`, but its
types are currently named for the calibration path. Either reuse the pure
functions directly or generalize their naming without coupling VQE to
calibration concepts.

This demonstrates sufficient statistics and O(1) state per key/window.

## Exercise 3: convergence slope

Estimate the direction of the energy curve with online linear regression:

```text
x = optimizer iteration
y = energy
```

Store:

```python
regression_count: int
sum_x: float
sum_y: float
sum_xx: float
sum_xy: float
```

Update them for every event, then calculate:

```python
denominator = n * sum_xx - sum_x * sum_x
slope = (
    (n * sum_xy - sum_x * sum_y) / denominator
    if n >= 2 and denominator != 0.0
    else 0.0
)
```

Interpretation:

- negative slope — energy is improving;
- slope near zero — possible plateau;
- positive slope — regression or measurement noise.

Do not hard-code a universal plateau epsilon. It should be configuration,
and its physical meaning depends on the Hamiltonian, ansatz, shots, and noise.

This demonstrates a non-trivial streaming aggregate built only from
sufficient statistics.

## Exercise 4: convergence state and a derived stream

Turn slope and variance into a small state machine:

```text
IMPROVING
PLATEAU
UNSTABLE
REGRESSING
```

One possible initial rule set is:

```python
if stddev >= unstable_threshold:
    candidate = "unstable"
elif slope < -slope_epsilon:
    candidate = "improving"
elif slope > slope_epsilon:
    candidate = "regressing"
else:
    candidate = "plateau"
```

Create a non-windowed table holding the current state and hysteresis counters:

```python
vqe_convergence_state = app.Table(
    "vqe_convergence_state",
    default=ConvergenceStateRecord,
)
```

Create a derived topic:

```python
VQE_CONVERGENCE_TOPIC = "vqe-convergence-alerts"

vqe_convergence_topic = app.topic(
    VQE_CONVERGENCE_TOPIC,
    value_type=VQEConvergenceEvent,
)
```

Publish only on a state transition:

```python
await vqe_convergence_topic.send(
    key=experiment_id,
    value=VQEConvergenceEvent(...),
)
```

Reuse the idea from `app.alerting.step()`: require several consecutive
observations before changing state. Without hysteresis, a noisy slope around
zero will make the state flap between `improving`, `plateau`, and
`regressing`.

If Grafana only needs the latest state, putting `convergence_state` on every
window metric row is sufficient. If you want a transition timeline or alert
history, persist the derived topic into a separate table such as:

```text
vqe_convergence_transitions(
    time,
    experiment_id,
    previous_state,
    new_state,
    slope,
    energy_stddev
)
```

This demonstrates aggregate → decision → derived stream processing.

## Exercise 5: hopping windows

Keep the existing tumbling window and introduce a second topology with a
60-second window advancing every 10 seconds:

```python
vqe_hopping_state = app.Table(
    "vqe_hopping_state",
    default=VQEWindowState,
).hopping(
    size=60.0,
    step=10.0,
    expires=timedelta(seconds=300),
)
```

An iteration can belong to several overlapping hopping windows, so state and
output volume increase. Do not replace the tumbling table immediately; keep
both and compare their dashboards.

Recommended event fields:

```text
window_type = hopping
window_start
window_end
window_size_s = 60
window_step_s = 10
```

Do not identify a window only by the publication timestamp. The actual window
boundaries should be explicit if Grafana must compare or deduplicate windows.

This demonstrates overlapping windows and the state/output cost of smoother
metrics.

## Exercise 6: event time and late events

The source record currently carries an ISO timestamp string. For explicit
event-time windows, add a numeric field that Faust can use directly:

```python
class VQEIterationMetricsEvent(faust.Record, serializer="json"):
    event_ts: float
    # existing fields...
```

Then configure the table:

```python
vqe_event_time_state = (
    app.Table("vqe_event_time_state", default=VQEWindowState)
    .tumbling(60.0, expires=timedelta(seconds=300))
    .relative_to_field(VQEIterationMetricsEvent.event_ts)
)
```

Alternatively, `.relative_to_stream()` uses the Kafka event timestamp.
`.relative_to_now()` uses processing-machine time. Choose deliberately and
document the semantics.

Add observability fields:

```text
event_time
processing_time
event_lag_s
late_event_count
```

Create a small producer/test that delays an event or republishes historical
events. Observe whether an old window is updated, whether it has expired, and
whether the derived row revises an earlier result.

`expires` controls retention of window state; it is not automatically the
same thing as a Kafka Streams grace period or final-results guarantee.

This demonstrates event time versus processing time and the difficulty of
declaring a distributed window final.

## Exercise 7: intermediate versus final emission

The current agent emits an aggregate after every input iteration. That is good
for a live dashboard but produces multiple snapshots for the same window.

Compare three policies:

1. emit every update;
2. emit only when a significant value changes;
3. emit once when the window closes.

Faust tables accept an `on_window_close` callback:

```python
async def on_vqe_window_close(key, value):
    # Convert closed state to an output record.
    # Verify the exact key/window metadata supplied by the installed Faust
    # version before designing the persistent schema.
    ...

vqe_final_state = app.Table(
    "vqe_final_state",
    default=VQEWindowState,
    on_window_close=on_vqe_window_close,
).tumbling(60.0, expires=timedelta(seconds=300))
```

Do not assume that this is identical to Kafka Streams `suppress()` semantics.
Test worker restart, late events, and expiration explicitly.

For the database, add an `is_final` field or write final results to a separate
table. Otherwise Grafana cannot distinguish an intermediate snapshot from a
closed-window result.

This demonstrates update streams, finalization, and output-volume trade-offs.

## Exercise 8: deduplication and replay safety

Use the natural event identity:

```text
(experiment_id, iteration)
```

The most instructive implementation is a windowed Faust table whose keys are
event IDs:

```python
processed_vqe_events = app.Table(
    "processed_vqe_events",
    default=bool,
).tumbling(
    WINDOW_SIZE_S,
    expires=timedelta(seconds=WINDOW_EXPIRES_S),
)
```

Before aggregation:

```python
event_id = f"{event.experiment_id}:{event.iteration}"
if processed_vqe_events[event_id].now():
    duplicate_count[event.experiment_id] += 1
    continue

processed_vqe_events[event_id] = True
```

Think carefully about partitioning. The deduplication key and aggregate key
are different. A naïve second `group_by` may introduce another repartition and
make operations non-atomic. A simpler exercise is to store a bounded set of
iteration numbers inside the per-experiment window state. That is acceptable
for the project's small `max_iterations`, but would not generalize to an
unbounded stream.

Also add a database uniqueness decision. If the raw fact is logically unique
per `(experiment_id, iteration)`, a unique constraint provides a final layer
of sink idempotency. Because TimescaleDB uniqueness constraints must include
the partitioning time column, verify the chosen schema rather than adding a
constraint blindly. An explicit `event_id` and `ON CONFLICT` strategy may be
clearer.

This demonstrates at-least-once delivery, idempotency, and effectively-once
application behavior.

## Exercise 9: stream-table join with experiment metadata

Publish experiment metadata to a compacted Kafka topic:

```text
key: experiment_id
value: molecule, backend, shots, max_iterations
```

Declare the topic and materialized table:

```python
experiment_metadata_topic = app.topic(
    "experiment-metadata",
    key_type=str,
    value_type=ExperimentMetadata,
)

experiment_metadata = app.Table(
    "experiment_metadata",
    default=ExperimentMetadata,
)
```

Populate the table with a dedicated agent:

```python
@app.agent(experiment_metadata_topic)
async def materialize_experiment_metadata(stream):
    async for experiment_id, metadata in stream.items():
        experiment_metadata[experiment_id] = metadata
```

In the VQE agent, look up:

```python
metadata = experiment_metadata[event.experiment_id]
```

The source stream and table must be co-partitioned by `experiment_id` for a
correct distributed join. Ensure the topics have compatible partition counts
and keys. Faust's `group_by(VQEIterationMetricsEvent.experiment_id)` creates a
repartitioned stream when needed.

For Grafana, persist the useful dimensions—at least `molecule` and
`backend_name`—with the derived metric row. Grafana cannot directly join a
Faust table to TimescaleDB, and joining separate Grafana data sources is not a
replacement for a well-defined analytical fact grain.

This demonstrates compacted topics, KTable-style materialization,
co-partitioning, and stream-table enrichment.

## Exercise 10: cross-experiment aggregates

After enrichment, change the key and aggregate across experiments:

```python
async for event in stream.group_by(
    lambda event: (event.molecule, event.backend_name)
):
    ...
```

Possible metrics include:

```text
experiment_count
average_final_energy
average_absolute_reference_error
average_convergence_slope
average_quantum_time_s
retry rate
completion/failure rate
```

This is a different grain from the existing per-experiment table. Persist it
separately, for example:

```text
vqe_workload_window_metrics(
    window_start,
    window_end,
    molecule,
    backend_name,
    experiment_count,
    avg_reference_error,
    avg_quantum_time_s,
    retry_rate
)
```

The critical lesson is that changing the grouping key requires
repartitioning. Without `group_by`, different workers can hold incomplete
state for the same molecule/backend key.

## Exercise 11: join VQE behavior with calibration

Materialize the latest calibration event by backend:

```python
latest_calibration = app.Table(
    "latest_calibration",
    default=LatestCalibrationRecord,
)

@app.agent(calibration_topic)
async def materialize_latest_calibration(stream):
    async for event in stream.group_by(CalibrationEvent.backend_name):
        latest_calibration[event.backend_name] = LatestCalibrationRecord(...)
```

After enriching VQE events with `backend_name`, attach the latest calibration
snapshot:

```python
calibration = latest_calibration[metadata.backend_name]
```

Then publish fields such as:

```text
calibration_error_rate
calibration_age_s
energy_stddev
quantum_time_s
retry_count
```

Be precise about semantics: this is “latest calibration known when the VQE
event was processed,” not necessarily calibration measured at exactly the
same physical time. Include calibration observation time so that consumers
can reason about staleness.

This demonstrates multiple materialized tables, enrichment order, and
temporal consistency.

## Suggested implementation order

| Phase | Exercise | Main concept | Database change |
|---|---|---|---|
| 1 | Improvement | Order-sensitive aggregation | Extend existing table |
| 2 | Variance | Online sufficient statistics | Extend existing table |
| 3 | Slope | Online regression | Extend existing table |
| 4 | Convergence state | Derived stream and hysteresis | Column and optionally transition table |
| 5 | Hopping window | Overlapping window semantics | Separate table recommended |
| 6 | Event time | Late/out-of-order data | Add window boundaries and lag fields |
| 7 | Final emission | Window lifecycle | Add `is_final` or separate final table |
| 8 | Deduplication | Replay/idempotency | Usually Faust state; optionally sink constraint |
| 9 | Metadata join | KTable and co-partitioning | Add dimensions to metrics |
| 10 | Workload aggregate | Rekeying/repartition | Separate table required |
| 11 | Calibration join | Temporal enrichment | Add calibration snapshot fields |

Start with phases 1–4. Together they add meaningful VQE interpretation while
covering four distinct streaming patterns. Hopping windows, event time, and
deduplication are the next step when the aim shifts from dashboard features
to understanding stream-processing correctness.

## Testing strategy

Do not validate only the final SQL row. Test each boundary separately.

### Pure aggregation tests

Extract state-update calculations into functions without Faust or Kafka:

```python
def update_vqe_state(state: VQEWindowState, event: VQEIterationMetricsEvent):
    ...
```

Test first-event initialization, negative energies, a single-point slope,
constant energy, decreasing energy, high variance, and duplicated iteration
numbers.

### Faust topology tests

Verify:

- records with one `experiment_id` update one key;
- a changed grouping key creates independent state;
- state transitions emit exactly one derived event;
- replayed events do not change aggregates after deduplication;
- event-time windows behave as expected for delayed input.

### Sink tests

Update tests whenever the Kafka event or SQL insert changes. Assert parameter
order, nullable behavior, and timestamp parsing.

### End-to-end validation

Extend `scripts/validate_demo.py` only after the lower-level behavior is
stable. Query by the experiment ID created during the validation run so old
rows cannot produce a false pass.

For hopping/final-window exercises, validate the expected window identity and
not only `count(*) > 0`.

## Useful Faust APIs

```python
app.topic(..., key_type=..., value_type=...)
app.Table(..., default=..., on_window_close=...)
table.tumbling(size, expires=...)
table.hopping(size, step, expires=...)
table.relative_to_stream()
table.relative_to_field(Record.timestamp_field)
stream.group_by(Record.key_field)
stream.items()
topic.send(key=..., value=...)
windowed_table[key].now()
```

Consult the installed `faust-streaming` version when an API differs. Faust is
inspired by Kafka Streams but does not implement every Kafka Streams operator
or guarantee with identical semantics.

## References

- [Faust tables and windowing](https://faust.readthedocs.io/en/latest/userguide/tables.html)
- [Faust-streaming project](https://github.com/faust-streaming/faust)
- [Kafka Streams windowing concepts](https://developer.confluent.io/courses/kafka-streams/windowing/)

