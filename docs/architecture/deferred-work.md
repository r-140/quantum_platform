# Deferred parts of the architecture

These pieces were in the very first sketch of the repo structure (before
any real code was written) and still aren't implemented. The placeholder
directories for them have been removed from the project tree — an empty
folder with no explanation is worse than no folder at all; the idea and
rationale live here instead.

## Kafka telemetry: `telemetry-ingest` + `stream-analytics`

From the very first architecture conversation for this project: RabbitMQ
is for the task queue ("run this once"), Kafka is for the telemetry
stream ("many events, real-time aggregation, history/replay").

Right now `orchestrator/app/tasks/calibration.py` publishes `error_rate`
to a **RabbitMQ** queue, `calibration-results` — an explicitly documented
**temporary stand-in**, not the final design. When we get to Kafka:

- **`telemetry-ingest`** — a producer that writes measurement/calibration
  events to a Kafka topic. In practice, `calibration.py` already does
  half of this job; the migration means replacing
  `publish_calibration_result()` (which currently publishes to RabbitMQ)
  with a Kafka producer, without touching `run_calibration()` itself —
  that separation is already baked into the current code.
- **`stream-analytics`** — Kafka Streams/Faust, real-time aggregation of
  the telemetry stream (rolling error rate per backend, anomalies, alerts
  on calibration degradation). This requires the data stream itself
  (`telemetry-ingest`) to exist first, so it doesn't make sense to build
  before that.

**Trigger for the migration**: as soon as `calibration.py` stops being
the only source of telemetry (for example, if a second backend shows up,
or calibration starts running more often than once every 5 minutes, or
we need history/replay of events rather than just the current snapshot)
— the RabbitMQ queue stops being the right tool for this (it wasn't
designed for it — see `docs/architecture/orchestration.md`, "Why
RabbitMQ, not Kafka").

## `fast-control` — low-latency control loop (Rust/Go)

An idea from the very first conversation: the timing-critical part of the
hw/sw interaction loop (real hardware may require microsecond-scale
latency, while the Python services operate at a higher-level
orchestration layer) — an isolated Rust/Go service, rather than smeared
across the Python code just to tick a "we use Rust" box.

Not implemented, because the current infrastructure runs entirely on
`AerBackend` (a local simulator with no real latency constraints) —
`fast-control` only makes sense to design once there's a real, or at
least realistically emulated, source of timing pressure.
`MockHardwareBackend` already partly plays this role (artificial
latency/failures), but isn't yet demanding enough on real-time response
to justify a separate Rust service.

## `orchestration/dags/calibration_then_run.py` — gate execution on calibration freshness

From the very first sketch: a Prefect/Airflow DAG for multi-step
experiments ("calibrate → run → analyze").

Right now, calibration (`calibration.py`) and running an experiment (the
`experiments` queue) are **independent, disconnected flows**: calibration
runs on its own schedule, and an experiment runs regardless of when the
last calibration happened or how "healthy" it was.

An unimplemented but genuine idea: **don't run an experiment** (especially
an expensive one, like VQE) if the last calibration:
- happened too long ago (the backend may have "drifted" since then), or
- showed an `error_rate` above an acceptable threshold.

This would require: (a) the API/orchestrator reading the latest
`CalibrationResult` before enqueueing a task (or `worker.py` itself
checking freshness before `execute_task`), and (b) a store for the last
known calibration state, accessible to both sides — a natural fit for
Postgres/Redis once we get there, not for a separate DAG engine
(Prefect/Airflow would be overkill for a single "freshness" check before
a run; a full DAG engine would be more useful for genuinely multi-step
scenarios like "calibrate → run a series of VQEs at different bond
lengths → build a potential-energy curve").
