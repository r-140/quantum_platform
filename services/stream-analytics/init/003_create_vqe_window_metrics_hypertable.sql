-- Runs automatically on first container init (docker-entrypoint-initdb.d
-- convention -- only fires against an empty data directory, not on every
-- restart; if you already have a running TimescaleDB volume from before
-- this file was added, it will NOT retroactively create this table --
-- either run `./dev.sh --clean` to reinitialize from a fresh volume, or
-- apply this file manually against the running container).
--
-- Stores the derived VQE metrics produced by the Faust tumbling-window
-- topology in stream-analytics/app/faust_app.py. Each row represents the
-- progressively updated aggregate for one experiment within the current
-- 60-second tumbling window.
--
-- This table intentionally exists separately from vqe_iteration_metrics:
--   * vqe_iteration_metrics contains the raw, per-iteration source data;
--   * vqe_window_metrics contains derived streaming aggregates suitable
--     for time-series queries and Grafana dashboards.
--
-- The Faust topology publishes a new vqe-window-metrics event after each
-- processed iteration, so multiple rows can belong to the same
-- experiment/window as the aggregate is progressively updated. The
-- `iteration_count` column identifies how many iterations contributed to
-- the aggregate represented by that row.
--
-- TimescaleDB is used for the same reason as vqe_iteration_metrics:
-- these are timestamped telemetry records that are naturally queried
-- over time ranges and benefit from hypertable storage.
--
-- The experiment/time index supports the main dashboard access pattern:
-- retrieve the window metrics for one experiment ordered chronologically.
--
-- See faust_app.py's "VQE metrics topology" module documentation for the
-- Kafka -> Faust tumbling Table -> vqe-window-metrics flow.
CREATE TABLE IF NOT EXISTS vqe_window_metrics (
    time                    TIMESTAMPTZ NOT NULL,
    experiment_id           TEXT NOT NULL,
    window_size_s           INTEGER NOT NULL,
    iteration_count         INTEGER NOT NULL,
    avg_energy              DOUBLE PRECISION,
    best_energy             DOUBLE PRECISION,
    avg_quantum_time_s      DOUBLE PRECISION,
    avg_classical_time_s    DOUBLE PRECISION,
    quantum_classical_ratio DOUBLE PRECISION,
    retry_count             INTEGER NOT NULL,
    circuit_breaker_trips   INTEGER NOT NULL
);

SELECT create_hypertable(
    'vqe_window_metrics',
    'time',
    if_not_exists => TRUE
);

CREATE INDEX IF NOT EXISTS idx_vqe_window_metrics_experiment_time
    ON vqe_window_metrics (experiment_id, time DESC);