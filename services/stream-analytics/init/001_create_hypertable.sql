-- Runs automatically on first container init (docker-entrypoint-initdb.d
-- convention -- only fires against an empty data directory, not on every
-- restart). Creates the append-only table backing timescale_sink.py's
-- writes, then converts it to a TimescaleDB hypertable.
--
-- Uses the older, backward-compatible create_hypertable(table, column)
-- signature rather than the newer generalized `by_range(...)` form
-- introduced in TimescaleDB 2.13 -- a real bug report was found
-- (timescale/timescaledb#6875) where `by_range()` failed to resolve
-- depending on search_path/image variant. The older signature is
-- documented as still fully supported for backward compatibility and has
-- no such reported issue, so it's the safer choice here given this
-- couldn't be tested against a live container before shipping (see
-- docs/architecture/kafka.md).

CREATE EXTENSION IF NOT EXISTS timescaledb;

CREATE TABLE IF NOT EXISTS calibration_events (
    time         TIMESTAMPTZ      NOT NULL,
    backend_name TEXT             NOT NULL,
    error_rate   DOUBLE PRECISION NOT NULL,
    shots        INTEGER          NOT NULL
);

SELECT create_hypertable('calibration_events', 'time', if_not_exists => TRUE);
