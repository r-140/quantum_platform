# VQE metrics for the hw/sw interaction loop

The "VQE metrics for the hw/sw interaction loop" item from
`docs/tech-debt.md` — instrumenting `vqe_loop.py` and publishing the
result through the Kafka → Faust → TimescaleDB → Grafana pipeline.

The implementation deliberately keeps two levels of telemetry:

- **iteration metrics** — the raw measurements produced for every COBYLA
  iteration;
- **window metrics** — derived streaming aggregates produced by the Faust
  tumbling-window topology.

This separation keeps the raw optimizer history available while providing
a compact, query-friendly representation for operational dashboards.

## What's captured, per COBYLA iteration

- `quantum_time_s` — total time spent waiting on the backend across all
  non-identity Hamiltonian terms this iteration (summed
  `wait_for_result` durations);
- `classical_time_s` — total iteration wall time minus `quantum_time_s`.
  An **approximation**, not an isolated measurement of COBYLA's own CPU
  time (that would need instrumenting `scipy.optimize` internals,
  out of scope) — reasonable given everything else in an iteration
  (`asyncio.run()` setup/teardown, Python overhead) is negligible next
  to real quantum wait time on anything but a trivially fast backend;
- `retry_count` — transient-error retries across all terms this
  iteration (`polling.py`'s existing retry/backoff machinery);
- `circuit_breaker_trips` — how many of this iteration's terms hit an
  already-open circuit breaker;
- `energy` and `params` — the existing optimizer/convergence data,
  now travelling alongside the execution telemetry.

There's no separate "circuit queue wait time" distinct from the above —
`polling.py` doesn't track a queued→running sub-split by wall clock
today, so `quantum_time_s` is the total wait, not decomposed further.
Adding that would need backend-level instrumentation that doesn't exist
yet.

## Where the instrumentation lives

`quantum_core` stays framework/broker-agnostic — the same principle used
by `execution.py`/`tasks.py` elsewhere in this project. Metric collection
happens in `quantum_core`, while Kafka publishing happens in
`orchestrator`:

- **`quantum_core/sync/polling.py`** — `PollingMetrics` is an optional
  out-parameter passed to `wait_for_result()`. It records retry count,
  circuit-breaker state and wait time without changing the existing
  return type. Existing callers that don't pass `metrics` remain fully
  backward-compatible. `try/finally` ensures `wait_time_s` is recorded
  on every exit path, including success, timeout and exhausted retries.

- **`quantum_core/loops/vqe_loop.py`** — `evaluate_energy()` accepts an
  optional `VQEIterationMetrics` and aggregates a fresh
  `PollingMetrics` for each Hamiltonian term. `run_vqe()`'s `cost()`
  closure measures the whole iteration, derives `classical_time_s`,
  and records the resulting telemetry in the extended
  `VQEIterationLog`.

- **`quantum_core/execution.py`** — `run_vqe_sync()` returns the
  iteration `history` in addition to the final summary. Each history
  entry is serialized with `dataclasses.asdict`, allowing the
  orchestrator to publish the complete iteration history.

- **`orchestrator/app/tasks/vqe_metrics.py`** — publishes one
  `vqe-iteration-metrics` Kafka message per VQE iteration using the
  shared `AIOKafkaProducer` already used by the calibration pipeline.
  Publishing is best-effort: a metrics publication failure is logged
  rather than causing an otherwise successful VQE experiment to fail.

## Why publishing happens after the VQE run

`run_vqe_sync()` is synchronous and runs in a background thread via
`run_in_executor` (see `docs/architecture/orchestration.md`). This
prevents the approximately one-minute VQE execution from blocking the
worker's event loop.

The Kafka producer belongs to the main event loop and is not driven
directly from the VQE worker thread. Therefore the current implementation
publishes the collected history after `run_in_executor` returns control
to the event loop.

The result is:

    VQE execution
          |
          v
    iteration history
          |
          v
    run completes
          |
          v
    publish N Kafka messages
          |
          v
    vqe-iteration-metrics

This is not true live streaming during optimization.

True live publication would require a thread-safe bridge back to the
event loop, for example `asyncio.run_coroutine_threadsafe`. That was
considered unnecessary for the current approximately one-minute VQE
runs and would add synchronization complexity to the algorithm
execution path.

The design can be revisited if VQE experiments become long-running and
live convergence monitoring becomes important.

# Faust window metrics

The raw iteration stream is consumed by the Faust implementation in
`services/stream-analytics/app/faust_app.py`.

The topology is intentionally separate from the calibration alerting
pipeline: VQE metrics describe the quantum/classical optimization loop,
while calibration events describe backend error rates.

The flow is:

    vqe-iteration-metrics
            |
            v
    group_by(experiment_id)
            |
            v
    vqe_window_state
    (60s tumbling Table)
            |
            v
    VQEWindowMetricsEvent
            |
            v
    vqe-window-metrics
            |
            v
    TimescaleDB / Grafana

`vqe_window_state` is a Faust tumbling Table keyed by
`experiment_id`. It maintains only the aggregate state required to
calculate the current window:

- iteration count;
- sum of energies;
- best/minimum energy;
- total quantum execution time;
- total classical execution time;
- retry count;
- circuit-breaker trips.

The aggregation is O(1) per iteration and does not retain the complete
iteration history in the Faust table. The raw history remains available
through Kafka and the `vqe_iteration_metrics` hypertable.

## Tumbling window semantics

The window is a **60-second tumbling window**: fixed-size,
non-overlapping intervals.

The table state expires after the configured window lifetime
(`WINDOW_EXPIRES_S = 300s`). The table therefore represents the current
streaming aggregation rather than the historical source of truth.

The `VQEWindowMetricsEvent` is emitted **after every processed
iteration**, not only when the 60-second window closes.

For example:

    iteration 1 -> iteration_count = 1
    iteration 2 -> iteration_count = 2
    ...
    iteration 20 -> iteration_count = 20

Consequently, multiple rows can represent progressively updated
aggregates for the same experiment/window. This makes the derived
stream suitable for near-real-time dashboards while the experiment is
running.

`avg_energy` and the execution-time metrics are calculated from the
running sums. `best_energy` is the minimum energy observed so far in
the current window.

`quantum_classical_ratio` is:

    avg_quantum_time_s / avg_classical_time_s

and is reported as `0.0` when classical execution time is zero.

## Why a separate window-metrics stream?

The raw and derived streams serve different purposes:

    vqe_iteration_metrics
        |
        | one event per optimizer iteration
        | complete raw telemetry
        v
    TimescaleDB: vqe_iteration_metrics


    vqe_iteration_metrics
        |
        | Faust aggregation
        v
    vqe-window-metrics
        |
        | derived streaming metrics
        v
    TimescaleDB: vqe_window_metrics

The raw table is the detailed source of telemetry and supports
iteration-level analysis.

The window table provides a smaller, directly consumable representation
for operational visualization and time-series analysis without requiring
Grafana to understand Kafka or Faust state.

# Storage

## Raw iteration metrics

`vqe_iteration_metrics` is a TimescaleDB hypertable created by
`init/002_create_vqe_metrics_hypertable.sql`.

It contains one row per VQE optimizer iteration:

- timestamp;
- experiment ID;
- iteration number;
- optimizer parameters;
- energy;
- quantum execution time;
- classical execution time;
- retry count;
- circuit-breaker trips.

`params` is stored as `JSONB`, rather than flattened into separate
columns. The ansatz parameter count isn't fixed forever — the LiH/BeH₂
item from `docs/tech-debt.md` would require more than H₂'s current
four parameters — and JSONB avoids a schema migration when the
parameter vector changes.

## Window metrics

`vqe_window_metrics` is a separate TimescaleDB hypertable created by
`init/003_create_vqe_window_metrics_hypertable.sql`.

It contains the derived Faust metrics:

- `window_size_s`;
- `iteration_count`;
- `avg_energy`;
- `best_energy`;
- `avg_quantum_time_s`;
- `avg_classical_time_s`;
- `quantum_classical_ratio`;
- `retry_count`;
- `circuit_breaker_trips`.

The table has an index on:

    (experiment_id, time DESC)

which matches the primary dashboard access pattern: retrieve the
time-ordered window metrics for one experiment.

Both tables use `time` as their TimescaleDB hypertable dimension.

# Kafka and TimescaleDB pipeline

`consumer.py` subscribes to both:

- `calibration-results`;
- `vqe-iteration-metrics`;
- `vqe-window-metrics`.

The low-volume telemetry streams share the same TimescaleDB connection
pool and consumer lifecycle.

The Faust application independently consumes
`vqe-iteration-metrics` under its own Kafka consumer group and produces
`vqe-window-metrics`.

This means the raw iteration stream can simultaneously be:

1. persisted directly by the hand-written TimescaleDB consumer; and
2. consumed by Faust to produce a derived windowed stream.

The two consumers do not interfere because they use different Kafka
consumer groups.

The resulting architecture is:

    orchestrator
        |
        | vqe-iteration-metrics
        v
    +-------------------------+
    |                         |
    v                         v
 consumer.py              Faust
    |                         |
    v                         v
 TimescaleDB             vqe-window-metrics
 raw metrics                  |
                              v
                         consumer.py
                              |
                              v
                         TimescaleDB
                         window metrics
                              |
                              v
                           Grafana

# Grafana

Grafana uses the already-provisioned:

    TimescaleDB (telemetry)

datasource. No additional datasource is required.

The VQE visualization is intentionally split into two dashboards:

### VQE Overview

`infra/grafana/provisioning/dashboards/vqe-overview.json`

This dashboard focuses on raw iteration telemetry, including:

- VQE energy/convergence;
- quantum vs. classical execution time.

The dashboard uses the `experiment_id` variable to select an experiment.

### VQE Window Metrics

A separate dashboard is used for the derived
`vqe_window_metrics` data rather than mixing window aggregates with the
raw iteration-level dashboard.

This separation mirrors the data model:

- **VQE Overview** → what happened at each optimizer iteration;
- **VQE Window Metrics** → how the streaming aggregate evolved over
  time.

Grafana queries TimescaleDB directly. Kafka and Faust remain part of the
data-processing pipeline rather than becoming dashboard dependencies.

# First-run / database initialization note

Like all `init/*.sql` files, these scripts are executed automatically
only when the TimescaleDB container initializes a genuinely empty data
directory.

If a TimescaleDB volume already exists from before the table was added,
Docker will not execute the new SQL file automatically.

Either:

    ./dev.sh --clean

to recreate the development environment from a fresh volume, or apply
the SQL file manually against the running TimescaleDB instance.

For example:

    docker compose exec timescaledb psql \
      -U quantum \
      -d telemetry \
      -f /docker-entrypoint-initdb.d/003_create_vqe_window_metrics_hypertable.sql

# Verification

The end-to-end pipeline has now been exercised against the actual
Docker stack.

## Raw iteration metrics

The TimescaleDB table was verified with:

    SELECT COUNT(*) FROM vqe_iteration_metrics;

Result:

    count
    -------
       140

The actual iteration data was also inspected successfully, for example:

    SELECT experiment_id, iteration, energy, time
    FROM vqe_iteration_metrics
    ORDER BY time DESC
    LIMIT 10;

The expected VQE iteration records were present.

## Window metrics

The derived table was verified with:

    SELECT COUNT(*) FROM vqe_window_metrics;

Result:

    count
    -------
       140

The derived records were then inspected with:

    SELECT
        experiment_id,
        time,
        window_size_s,
        iteration_count,
        avg_energy,
        best_energy,
        avg_quantum_time_s,
        avg_classical_time_s,
        quantum_classical_ratio
    FROM vqe_window_metrics
    ORDER BY time DESC
    LIMIT 10;

The expected progressively updated aggregates were present. For the
verified experiment, `window_size_s` was 60 seconds and
`iteration_count` increased with each processed iteration.

The observed values also demonstrate the intended relationship between
quantum and classical execution time: quantum execution was around
1.25–1.28 seconds per iteration while classical execution was around
0.004 seconds, producing a quantum/classical ratio around 300.

## Grafana

The TimescaleDB datasource was already provisioned as:

    TimescaleDB (telemetry)

The VQE window-metrics dashboard was added as a separate dashboard
rather than extending the raw iteration dashboard.

The dashboard successfully reads the derived
`vqe_window_metrics` data from TimescaleDB.

# Unit-level verification

The lower-level implementation was also verified independently:

- **`PollingMetrics` / `wait_for_result`** — five scenarios were run
  against the existing `ScriptedBackend` test double:
  immediate success, transient retries, already-open circuit breaker,
  `metrics=None` backward compatibility, and timeout with `finally`
  recording `wait_time_s`.

- **`evaluate_energy()`** — verified that one circuit is submitted per
  non-identity Hamiltonian term and that metrics from the individual
  terms are aggregated correctly.

- **`insert_vqe_iteration_metric()`** — verified against a hand-written
  fake asyncpg pool that all expected parameters are bound and that
  `params` is JSON-encoded before insertion.

The corresponding tests are kept in the existing test suite.

# Remaining limitations

The current implementation intentionally does not provide live
iteration-by-iteration Kafka publication while the VQE algorithm is
running. Metrics become available after `run_vqe_sync()` completes.

The Faust window aggregation also represents the progressively updated
current tumbling window. It is not intended to replace the raw
iteration history.

If VQE executions become significantly longer, the next architectural
step would be a thread-safe bridge allowing the synchronous optimizer
thread to publish iteration events back to the main asyncio event loop
during execution.

If backend instrumentation becomes more detailed, `quantum_time_s`
could also be split into queue time, execution time and result
retrieval time.

Neither limitation affects the current use case: the complete VQE
history is available after each run, the streaming aggregation is
persisted in TimescaleDB, and both raw and derived telemetry are
visualized in Grafana.