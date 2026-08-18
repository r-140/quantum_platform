-- Runs automatically on first container init (docker-entrypoint-initdb.d
-- convention -- only fires against an empty data directory, not on every
-- restart; if you already have a running TimescaleDB volume from before
-- this file was added, it will NOT retroactively create this table --
-- either run `./dev.sh --clean` to reinitialize from a fresh volume, or
-- apply this file manually against the running container).
--
-- Backs vqe_iteration_metrics inserts from timescale_sink.py -- the "VQE
-- metrics for the hw/sw interaction loop" item from docs/tech-debt.md.
-- Same create_hypertable() signature choice and rationale as
-- 001_create_hypertable.sql -- see that file's comment.

CREATE TABLE IF NOT EXISTS vqe_iteration_metrics (
    time                  TIMESTAMPTZ      NOT NULL,
    experiment_id         TEXT             NOT NULL,
    iteration             INTEGER          NOT NULL,
    params                JSONB            NOT NULL,
    energy                DOUBLE PRECISION NOT NULL,
    quantum_time_s        DOUBLE PRECISION NOT NULL,
    classical_time_s      DOUBLE PRECISION NOT NULL,
    retry_count           INTEGER          NOT NULL,
    circuit_breaker_trips INTEGER          NOT NULL
);

SELECT create_hypertable('vqe_iteration_metrics', 'time', if_not_exists => TRUE);
