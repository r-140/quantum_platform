# VQE metrics for the hw/sw interaction loop

The "VQE metrics for the hw/sw interaction loop" item from
`docs/tech-debt.md` — instrumenting `vqe_loop.py` and publishing the
result through the same Kafka → TimescaleDB → Grafana pipeline already
built for calibration telemetry (`docs/architecture/kafka.md`).

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
- `energy` and `params` — already existed (the convergence curve); now
  travels alongside the new fields instead of being the only thing
  recorded.

There's no separate "circuit queue wait time" distinct from the above —
`polling.py` doesn't track a queued→running sub-split by wall clock
today, so `quantum_time_s` is the total wait, not decomposed further.
Adding that would need backend-level instrumentation that doesn't exist
yet; noted here rather than silently claiming a precision this data
doesn't have.

## Where the instrumentation lives

`quantum_core` stays framework/broker-agnostic — same principle as
`execution.py`/`tasks.py` elsewhere in this project — so the actual
metric *collection* lives there, and the Kafka *publishing* lives in
`orchestrator`:

- **`quantum_core/sync/polling.py`** — a new optional out-parameter,
  `PollingMetrics`, passed into `wait_for_result()`. Mutated in place
  (retry count, circuit-breaker-was-open flag, wait time, poll count)
  rather than changing `wait_for_result`'s return type — every existing
  call site that doesn't pass `metrics` behaves exactly as before. Set
  via `try/finally` so `wait_time_s` is recorded on every exit path
  (success, timeout, exhausted retries), not just the happy path.
- **`quantum_core/loops/vqe_loop.py`** — `evaluate_energy()` accepts an
  optional `VQEIterationMetrics`, which sums a fresh `PollingMetrics`
  per Hamiltonian term. `run_vqe()`'s `cost()` closure times the whole
  iteration, derives `classical_time_s`, and records everything on the
  now-extended `VQEIterationLog`.
- **`quantum_core/execution.py`** — `run_vqe_sync()`'s returned dict now
  includes `history` (one dict per iteration, via `dataclasses.asdict`),
  not just the final summary. This modestly increases the size of the
  experiment's stored result (~100 bytes/iteration, up to ~80
  iterations — a few KB, not a concern at this scale) but is what lets
  `orchestrator` publish it.
- **`orchestrator/app/tasks/vqe_metrics.py`** (new) —
  `publish_vqe_history()` sends one Kafka message per iteration to
  `vqe-iteration-metrics`, using the same shared `AIOKafkaProducer`
  instance `calibration.py` already uses (not a fresh producer per
  run — that would mean a broker handshake on every single VQE
  experiment). Best-effort: a publish failure here is logged and
  swallowed, not re-raised — the experiment's own result was already
  computed successfully by this point.

### Why "after the run completes," not truly live

`run_vqe_sync()` is synchronous and runs inside a background thread via
`run_in_executor` (see `docs/architecture/orchestration.md` for why —
VQE is the one algorithm that would otherwise block the worker's event
loop for its ~1 minute runtime). `AIOKafkaProducer` isn't safe to drive
from a thread other than the one its event loop belongs to, so
publishing happens in `run_experiment.py` **after** `run_in_executor`
returns control to the main event loop — the whole `history` gets
published in one batch, not streamed message-by-message during the run.
True live streaming during the run would need a thread-safe bridge back
to the event loop (`asyncio.run_coroutine_threadsafe`) — not
implemented, given a full VQE run is only ~1 minute total; publishing
the complete history right after completion was judged a reasonable
scope trade-off, not a fundamental limitation. Worth revisiting if VQE
runs grow long enough that watching convergence live (not just after
the fact) becomes valuable.

## Storage and visualization

`vqe_iteration_metrics` — a new TimescaleDB hypertable
(`init/002_create_vqe_metrics_hypertable.sql`), same pattern as
`calibration_events` (`docs/architecture/kafka.md`): append-only,
`create_hypertable()` with the same older, deliberately-chosen signature
(see that file's comment for the TimescaleDB 2.13+ `by_range()` bug that
motivated it). `params` is stored as `JSONB`, not flattened into
separate columns — the ansatz parameter count isn't fixed forever (the
LiH/BeH₂ item in `docs/tech-debt.md` would need more than H2's current
4 parameters), and JSONB avoids a schema migration if/when that happens.

`consumer.py` (the hand-rolled `stream-analytics` consumer) now
subscribes to both `calibration-results` and `vqe-iteration-metrics` on
one `AIOKafkaConsumer`, dispatching on `message.topic` — one consumer
process rather than two, since both topics are low-volume and share the
same TimescaleDB pool and consumer-group lifecycle.

Grafana already has a `TimescaleDB (telemetry)` datasource provisioned
(`docs/architecture/observability.md`) — no new datasource needed. A
convergence-curve panel (`energy` over `time`, filtered by
`experiment_id`) or a quantum-vs-classical time breakdown is a Grafana
Explore query against the new table away, following the same
"provision datasources automatically, build panels manually" split
already established for `calibration_events`.

⚠️ **First-run note**: like any `init/*.sql` file, this only runs
against a genuinely empty TimescaleDB data directory. If you already
have a running TimescaleDB volume from before this table was added, it
will **not** retroactively appear — either run `./dev.sh --clean` (wipes
and reinitializes; see `dev.sh --help`) or apply the SQL file manually
against the running container.

## ⚠️ Degree of verification

Unlike most new code added late in this project, a meaningful chunk of
this **was** actually run, not just reasoned about:

- **`PollingMetrics`/`wait_for_result`**: 5 scenarios run directly
  against the real `ScriptedBackend` test double already in this repo
  (immediate success, transient retries counted correctly, an
  already-open breaker short-circuiting with `wait_time_s == 0`,
  `metrics=None` staying fully backward-compatible, and the timeout
  path still setting `wait_time_s` via the `finally` block) — all
  passed. Transcribed into `tests/unit/test_polling_metrics.py` in that
  verified form.
- **`evaluate_energy`'s metrics aggregation across Hamiltonian terms**:
  run directly too, with `qiskit` stubbed out at import time (same
  "stub the heavy dependency, verify the logic" approach this project
  already uses for `pydantic` elsewhere) and
  `build_measurement_circuit`/`pauli_expectation_from_counts` swapped
  for simple stand-ins. Confirmed `evaluate_energy` submits exactly one
  circuit per non-identity term (5 of H2's 6 terms) and sums their
  metrics correctly.
- **`insert_vqe_iteration_metric`**: run directly against a
  hand-written fake pool (same `FakePool` pattern as
  `test_timescale_sink.py`'s existing test), with `asyncpg` stubbed at
  import time — confirmed all 9 bound parameters, including that
  `params` is JSON-encoded before binding. Transcribed into
  `tests/test_timescale_sink.py`.

⚠️ **Not verified**: the actual Kafka publish/consume round trip
(`vqe_metrics.py` → real Kafka → `consumer.py` → real TimescaleDB), the
new SQL file against a real TimescaleDB container, and the
`run_experiment.py`/`worker.py` wiring (`kafka_producer` threaded
through `handle_message`) against a real RabbitMQ+Kafka stack — no
Docker, `aio-pika`, `aiokafka`, or `asyncpg` in my environment for any
of that. Run a VQE experiment end-to-end and check:

```bash
docker exec -it quantum-platform-kafka kafka-console-consumer \
  --bootstrap-server localhost:9092 --topic vqe-iteration-metrics --from-beginning

docker exec -it quantum-platform-timescaledb psql -U quantum -d telemetry \
  -c "SELECT time, iteration, energy, quantum_time_s, classical_time_s FROM vqe_iteration_metrics ORDER BY time DESC LIMIT 10;"
```
