#!/usr/bin/env bash
# Starts the whole local stack from the repo root: RabbitMQ + Postgres +
# Kafka + TimescaleDB (docker compose), Alembic migrations, then
# api + orchestrator + stream-analytics sink + Faust analytics +
# result-indexer, each in its own process (and each service in its own venv).
#
# Profiles (Maven-style -P, spelled --profile=<name> since this is bash):
#   --profile=quick   (default) -- setup + start, no test runs. Fast inner
#                      loop for "I just want the stack up."
#   --profile=verify  -- runs each service's pytest suite (if it has one)
#                      right after that service's venv is set up, BEFORE
#                      any service is started. Aborts immediately (nothing
#                      gets started) if any suite fails -- mirrors `mvn
#                      verify` failing the build before `install`/deploy,
#                      rather than silently starting a stack you can't
#                      trust. quantum-core's own test suite is included
#                      too, run via api's venv since quantum-core is only
#                      ever installed as an editable dependency of the
#                      other services here, not given its own venv setup
#                      step (see run_tests_if_present below).
#
# Flags:
#   --clean  -- runs `docker compose down -v` for THIS project's own
#               containers/volumes before starting, wiping Postgres/
#               TimescaleDB/Grafana/RabbitMQ data and starting from a
#               clean slate. Deliberately opt-in, not the default --
#               running this on every normal start would silently erase
#               your persisted data every time. NOTE what this does NOT
#               fix: it only tears down containers under this project's
#               own compose project name (container names here are
#               already prefixed `quantum-platform-*` specifically to
#               avoid colliding with other projects) -- it cannot stop
#               or free a port held by a *different*, unrelated
#               project's own container (e.g. another project's Grafana
#               on its own default host port). If you're hitting that
#               kind of conflict, stop the other project's container
#               directly, or change this project's host port mapping in
#               docker-compose.yml. (Grafana here is deliberately on
#               host port 3001, not the common default 3000, for
#               exactly this reason -- see docker-compose.yml.)
#
# Run from the repo root:
#   ./dev.sh
#   ./dev.sh --profile=verify
#   ./dev.sh --clean
#   ./dev.sh --clean --profile=verify
#
# Logs for api/orchestrator/stream-analytics/Faust/result-indexer are written to .dev-logs/
# (gitignored) and tailed live in this terminal.
#
# First run creates each service's .venv automatically if missing, and
# always runs `pip install -r requirements.txt` (cheap/no-op once
# dependencies are already satisfied) -- this deliberately keeps venvs in
# sync with requirements.txt on every run rather than silently drifting.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="$ROOT_DIR/.dev-logs"
mkdir -p "$LOG_DIR"

export DATABASE_URL="postgresql+asyncpg://quantum:quantum@localhost:5432/quantum_platform"

PROFILE="quick"
CLEAN="false"
while [[ $# -gt 0 ]]; do
    case "$1" in
        --profile=*) PROFILE="${1#*=}"; shift ;;
        --profile) PROFILE="${2:-}"; shift 2 ;;
        --clean) CLEAN="true"; shift ;;
        -h|--help)
            grep '^#' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *)
            echo "Unknown argument: $1 (see --help)" >&2
            exit 1
            ;;
    esac
done

if [[ "$PROFILE" != "quick" && "$PROFILE" != "verify" ]]; then
    echo "Unknown profile '$PROFILE' -- expected 'quick' or 'verify'" >&2
    exit 1
fi

echo "==> Profile: $PROFILE"

PIDS=()

cleanup() {
    echo ""
    echo "==> Stopping api + orchestrator + stream-analytics + stream-analytics-faust + result-indexer..."
    for pid in "${PIDS[@]:-}"; do
        kill "$pid" 2>/dev/null || true
    done
    wait 2>/dev/null || true
    echo "==> Stopped. RabbitMQ/Postgres/Kafka/TimescaleDB containers are still running -- 'docker compose down' to stop them too."
}
trap cleanup EXIT INT TERM

if [[ "$CLEAN" == "true" ]]; then
    echo "==> --clean: tearing down this project's own containers and volumes (docker compose down -v)..."
    echo "    (this only affects quantum-platform-* containers/volumes -- see --help for what it does NOT fix)"
    docker compose -f "$ROOT_DIR/docker-compose.yml" down -v
fi

echo "==> Starting RabbitMQ + Postgres + Kafka + TimescaleDB (docker compose)..."
docker compose -f "$ROOT_DIR/docker-compose.yml" up -d

echo "==> Waiting for RabbitMQ to be healthy..."
until docker compose -f "$ROOT_DIR/docker-compose.yml" exec -T rabbitmq rabbitmq-diagnostics -q ping >/dev/null 2>&1; do
    sleep 1
done
echo "    RabbitMQ is up."

echo "==> Waiting for Postgres to be healthy..."
until docker compose -f "$ROOT_DIR/docker-compose.yml" exec -T postgres pg_isready -U quantum >/dev/null 2>&1; do
    sleep 1
done
echo "    Postgres is up."

echo "==> Waiting for Kafka to be healthy..."
until docker compose -f "$ROOT_DIR/docker-compose.yml" exec -T kafka kafka-broker-api-versions --bootstrap-server localhost:9092 >/dev/null 2>&1; do
    sleep 1
done
echo "    Kafka is up."

echo "==> Waiting for TimescaleDB to be healthy..."
until docker compose -f "$ROOT_DIR/docker-compose.yml" exec -T timescaledb pg_isready -U quantum >/dev/null 2>&1; do
    sleep 1
done
echo "    TimescaleDB is up."

setup_venv() {
    local service_dir="$1"
    (
        cd "$service_dir"
        if [ ! -d ".venv" ]; then
            echo "    creating venv..."
            python3 -m venv .venv
        fi
        # IMPORTANT: cd into the service dir *before* pip install -- pip
        # resolves relative editable paths (e.g. `-e ../quantum-core` in
        # requirements.txt) against the *current working directory of the
        # pip invocation*, not against the requirements.txt file's own
        # location (a long-standing pip quirk, see pypa/pip#6112).
        ./.venv/bin/pip install -q -r requirements.txt
    )
}

# Runs `pytest tests/ -v` for a service using ITS OWN venv, but only if
# that service actually has a tests/ directory containing at least one
# `test_*.py` file -- not every service has a test suite yet (e.g.
# orchestrator doesn't, at time of writing), and this should be a silent
# no-op for those rather than an error. Aborts the *entire* script (before
# any service is started) if a suite that DOES exist fails -- see the
# --profile=verify description at the top of this file for why that's the
# desired behavior, not just "log a warning and carry on."
#
# Third argument overrides which venv to run pytest from -- used for
# quantum-core, which has no venv-setup step of its own in this script (see
# call site below).
run_tests_if_present() {
    local name="$1"
    local service_dir="$2"
    local venv_owner_dir="${3:-$service_dir}"

    if [ ! -d "$service_dir/tests" ] || [ -z "$(find "$service_dir/tests" -name 'test_*.py' 2>/dev/null | head -1)" ]; then
        return 0
    fi

    echo "==> [verify] running tests for $name..."
    if ! (cd "$service_dir" && "$venv_owner_dir/.venv/bin/pytest" tests/ -v); then
        echo "==> [verify] $name tests FAILED -- aborting before starting any service" >&2
        exit 1
    fi
}

echo "==> Setting up api..."
setup_venv "$ROOT_DIR/services/api"

if [ "$PROFILE" = "verify" ]; then
    # quantum-core is only ever installed as an editable dependency of the
    # services below (`-e ../quantum-core` in their requirements.txt) --
    # it has no venv-setup step of its own here. Its own test suite
    # (polling.py's CircuitBreaker/wait_for_result tests) is still worth
    # running in verify mode, so it borrows api's already-set-up venv
    # (pytest is run *from* quantum-core's own directory, so its
    # pyproject.toml's asyncio_mode=auto setting still applies).
    run_tests_if_present "quantum-core" "$ROOT_DIR/services/quantum-core" "$ROOT_DIR/services/api"
    run_tests_if_present "api" "$ROOT_DIR/services/api"
fi

echo "==> Running Alembic migrations..."
(cd "$ROOT_DIR/services/api" && ./.venv/bin/python3 -m alembic upgrade head)

echo "==> Setting up orchestrator..."
setup_venv "$ROOT_DIR/services/orchestrator"
[ "$PROFILE" = "verify" ] && run_tests_if_present "orchestrator" "$ROOT_DIR/services/orchestrator"

echo "==> Setting up stream-analytics..."
setup_venv "$ROOT_DIR/services/stream-analytics"
[ "$PROFILE" = "verify" ] && run_tests_if_present "stream-analytics" "$ROOT_DIR/services/stream-analytics"

echo "==> Setting up result-indexer..."
setup_venv "$ROOT_DIR/services/result-indexer"
[ "$PROFILE" = "verify" ] && run_tests_if_present "result-indexer" "$ROOT_DIR/services/result-indexer"

run_service() {
    local name="$1"
    local service_dir="$2"
    shift 2
    local run_cmd=("$@")

    echo "==> Starting $name (log: $LOG_DIR/$name.log)..."
    (cd "$service_dir" && exec "${run_cmd[@]}") > "$LOG_DIR/$name.log" 2>&1 &
    PIDS+=($!)
}

run_service "api" "$ROOT_DIR/services/api" \
    .venv/bin/uvicorn app.main:app --port 8000

run_service "orchestrator" "$ROOT_DIR/services/orchestrator" \
    .venv/bin/python3 -m app.worker

run_service "stream-analytics" "$ROOT_DIR/services/stream-analytics" \
    .venv/bin/python3 -m app.consumer

run_service "stream-analytics-faust" "$ROOT_DIR/services/stream-analytics" \
    .venv/bin/python3 -m app.faust_app worker -l info

run_service "result-indexer" "$ROOT_DIR/services/result-indexer" \
    .venv/bin/python3 -m app.worker

echo ""
echo "All services started:"
echo "  API docs:          http://localhost:8000/docs"
echo "  Experiments board: http://localhost:8000/dashboard/"
echo "  RabbitMQ UI:       http://localhost:15672 (guest/guest)"
echo "  Postgres:          localhost:5432 (quantum/quantum, db=quantum_platform)"
echo "  Kafka:             localhost:9092"
echo "  TimescaleDB:       localhost:5433 (quantum/quantum, db=telemetry)"
echo "  Faust dashboard:   http://localhost:6066/dashboard/"
echo "  Logs:              $LOG_DIR/"
echo "  Faust log:         $LOG_DIR/stream-analytics-faust.log"
echo "  Vector index log:  $LOG_DIR/result-indexer.log"
echo ""
echo "  Debug/ops stack (see docs/architecture/observability.md):"
echo "  Grafana:           http://localhost:3001 (admin/admin)"
echo "  Prometheus:        http://localhost:9090"
echo "  Kafka UI (Kafbat): http://localhost:8090"
echo "  Adminer (SQL):     http://localhost:8091"
echo "    (these come up as part of docker compose above -- may take a few"
echo "    extra seconds after this script prints its own services as ready)"
echo ""
echo "Tailing logs (Ctrl+C stops api + orchestrator + stream-analytics + Faust + result-indexer)..."
echo ""

tail -f "$LOG_DIR/api.log" "$LOG_DIR/orchestrator.log" \
    "$LOG_DIR/stream-analytics.log" "$LOG_DIR/stream-analytics-faust.log" \
    "$LOG_DIR/result-indexer.log"
