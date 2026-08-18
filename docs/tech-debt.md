# Tech debt / roadmap

A single consolidated list of what's been consciously deferred — instead
of keeping scattered "Not yet implemented" sections in each individual
`docs/architecture/*.md`. Those documents can still mention their own
local items — this file aggregates them and adds anything not tied to
one specific service.

## Housekeeping

- **Translate all documentation into English** — all documentation
  (`README.md`, `docs/**/*.md`, in-code docstrings) was in Russian;
  needed translation before a public presentation/demo in English.
  Priority order: `README.md` → `docs/architecture/*.md` →
  `docs/algorithms/*.md` → each service's own README. In-code
  docstrings — nice to have, not critical for a demo.

  **✅ Done** — `README.md`, every `docs/architecture/*.md` and
  `docs/algorithms/*.md`, every service README, and the stale English
  placeholder stubs (`docs/setup.md`, `docs/chemistry/h2_hamiltonian.md`,
  `docs/decisions/*.md`, `docs/architecture/storage.md`,
  `hw_sw_interaction_loop.md`, `messaging.md` — these were leftover
  duplicates of an old, unrelated architecture sketch and have been
  replaced with real content, not just translated) are now in English.
  In-code docstrings remain in Russian — still the "nice to have, not
  critical" item from the original note, unchanged.

## Future architectural ideas

- **An AI agent for result interpretation + a vector database.** At
  real scale (not a pet project), the volume of experiment results is
  large enough that a human can't realistically review every one by
  hand. The idea: an agent on the Claude API that:
  - analyzes completed experiments' results (e.g. interprets a Grover
    measurement histogram, judges whether VQE converged to a reasonable
    energy value, flags anomalies in `error_rate`);
  - writes a short interpretation/summary back into `ExperimentResponse`
    (a new field, e.g. `ai_summary`) or into a separate table;
  - uses a vector database (Pinecone/Weaviate/pgvector — pgvector is
    probably the most natural choice here, since the project already
    has Postgres and doesn't need another infrastructure dependency)
    for semantic search over past results — "show experiments similar
    to this one," or "which VQE runs had a similar convergence curve."

  Not designed in detail yet — a substantial conversation of its own
  once we get there: where the agent physically lives (a separate
  service? part of `orchestrator`? reacting to a Kafka "experiment
  completed" event?), how to prompt it differently per algorithm type,
  and whether to use structured output (Claude supports a JSON schema
  via tool use) for a reliably parseable response.

- **VQE metrics for the hw/sw interaction loop** (already under
  discussion at the time this item was written) — instrument
  `vqe_loop.py`: quantum vs. classical (COBYLA) time per iteration,
  circuit queue wait time, retry events from `polling.py`, the energy
  convergence curve — published to a new Kafka topic → TimescaleDB →
  Grafana dashboard. A natural extension of the calibration pipeline
  already built, applied to VQE itself.

  **✅ Done** — see `docs/architecture/vqe-metrics.md`. One scope note:
  "circuit queue wait time" ended up captured as total wait time per
  term (`quantum_time_s`), not decomposed into a separate queued-vs-
  running split — `polling.py` doesn't track that sub-phase by wall
  clock, and adding it would need backend-level instrumentation that
  doesn't exist yet. Also: published *after* the full VQE run
  completes, not truly live during it (see that doc for why) — worth
  revisiting if VQE runs grow long enough for live convergence-curve
  viewing to matter.

- **Faust → output topic → TimescaleDB → Grafana** (see `kafka.md`) —
  expose Faust's VQE tumbling-window aggregation through a dedicated
  output topic and persist it in TimescaleDB for visualization.

  **✅ Done** — `process_vqe_iteration()` maintains a changelog-backed
  Faust tumbling `Table` (`vqe_window_state`) keyed by `experiment_id`
  and publishes the derived `VQEWindowMetricsEvent` to the
  `vqe-window-metrics` Kafka topic. `consumer.py` consumes that topic
  and persists the aggregates to the `vqe_window_metrics` TimescaleDB
  hypertable. A separate **VQE Window Metrics** Grafana dashboard
  visualizes the aggregated metrics, keeping them separate from the
  raw per-iteration VQE dashboard.

  The window currently uses a 60-second tumbling interval and exposes
  iteration count, average/best energy, average quantum/classical
  execution time, their ratio, retry count, and circuit-breaker trips.
  The resulting TimescaleDB table has been verified with real data
  (`140` rows in both `vqe_iteration_metrics` and
  `vqe_window_metrics` during the initial end-to-end run).

- **LiH/BeH₂ + an OOP refactor of the molecule code** — extend VQE from
  H₂ to larger molecules; an abstract `Molecule`/`MolecularHamiltonian`,
  concrete subclasses per molecule, an ansatz parameterized by qubit
  count. The Hamiltonian coefficients will need to be re-verified
  against a source again (possibly the same O'Malley et al., but not
  relying on memory for it) — a multi-session task, not a one-day one.

## Minor tech debt (low priority)

- Pagination on the experiments dashboard — `GET /experiments` returns
  the entire list at once;
- Server-side search by id on the dashboard — currently client-side
  only, over the already-loaded page;
- WebSocket/SSE instead of the dashboard's 3-second polling;
- Postgres connection pooling hasn't been tuned under load;
- `ALERT_THRESHOLD = 0.05` in `stream-analytics/app/consumer.py` — a
  placeholder with no real noise model to calibrate it against;
- Multiple consumers within the same consumer group
  (`stream-analytics`) for horizontal scaling — not needed at the
  current volume;
- `retry_policy.py` (orchestrator) holds the delay between retries in
  the worker's own event loop — with multiple workers, this doesn't let
  the message be picked up by another worker during the delay;
- Gate execution on calibration freshness — don't run an expensive
  experiment (VQE) if the last calibration was too long ago or showed a
  high `error_rate`. Discussed as a rejected candidate for a
  Prefect/Airflow DAG — decided that a full DAG engine is overkill for
  a single "freshness" check; more natural via a Postgres/Redis snapshot
  of the last known calibration state;
- `fast-control` (a low-latency control loop in Rust/Go) — from the very
  first architecture sketch; not implemented, since the current
  infrastructure runs entirely on `AerBackend` (a local simulator with
  no real latency constraints) — only worth designing once there's a
  real, or realistically emulated, source of timing pressure.
