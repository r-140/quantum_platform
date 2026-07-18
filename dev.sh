#!/usr/bin/env bash
# Starts the whole local stack from the repo root: RabbitMQ (docker
# compose), then api + orchestrator, each in its own venv. Ctrl+C stops
# api/orchestrator; RabbitMQ is left running (fast restart next time) --
# `docker compose down` separately if you want to stop it too.
#
# Run from the repo root:
#   ./dev.sh
#
# Logs for api/orchestrator are written to .dev-logs/ (gitignored) and
# tailed live in this terminal.
#
# First run creates each service's .venv automatically if missing, and
# always runs `pip install -r requirements.txt` (cheap/no-op once
# dependencies are already satisfied) -- this deliberately keeps venvs in
# sync with requirements.txt on every run rather than silently drifting.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="$ROOT_DIR/.dev-logs"
mkdir -p "$LOG_DIR"

PIDS=()

cleanup() {
    echo ""
    echo "==> Stopping api + orchestrator..."
    for pid in "${PIDS[@]:-}"; do
        kill "$pid" 2>/dev/null || true
    done
    wait 2>/dev/null || true
    echo "==> Stopped. RabbitMQ container is still running -- 'docker compose down' to stop it too."
}
trap cleanup EXIT INT TERM

echo "==> Starting RabbitMQ (docker compose)..."
docker compose -f "$ROOT_DIR/docker-compose.yml" up -d

echo "==> Waiting for RabbitMQ to be healthy..."
until docker compose -f "$ROOT_DIR/docker-compose.yml" exec -T rabbitmq rabbitmq-diagnostics -q ping >/dev/null 2>&1; do
    sleep 1
done
echo "    RabbitMQ is up."

setup_and_run() {
    local name="$1"
    local service_dir="$2"
    shift 2
    local run_cmd=("$@")

    echo "==> Setting up $name..."
    # IMPORTANT: cd into the service dir *before* pip install -- pip
    # resolves relative editable paths (e.g. `-e ../quantum-core` in
    # requirements.txt) against the *current working directory of the pip
    # invocation*, not against the requirements.txt file's own location
    # (a long-standing pip quirk, see pypa/pip#6112). Passing an absolute
    # path to `-r` without also cd-ing first would silently resolve
    # `../quantum-core` against the wrong directory.
    (
        cd "$service_dir"
        if [ ! -d ".venv" ]; then
            echo "    creating venv..."
            python3 -m venv .venv
        fi
        ./.venv/bin/pip install -q -r requirements.txt
    )

    echo "==> Starting $name (log: $LOG_DIR/$name.log)..."
    (cd "$service_dir" && exec "${run_cmd[@]}") > "$LOG_DIR/$name.log" 2>&1 &
    PIDS+=($!)
}

setup_and_run "api" "$ROOT_DIR/services/api" \
    .venv/bin/uvicorn app.main:app --port 8000

setup_and_run "orchestrator" "$ROOT_DIR/services/orchestrator" \
    .venv/bin/python3 -m app.worker

echo ""
echo "All services started:"
echo "  API docs:      http://localhost:8000/docs"
echo "  RabbitMQ UI:   http://localhost:15672 (guest/guest)"
echo "  Logs:          $LOG_DIR/"
echo ""
echo "Tailing logs (Ctrl+C stops api + orchestrator)..."
echo ""

tail -f "$LOG_DIR/api.log" "$LOG_DIR/orchestrator.log"