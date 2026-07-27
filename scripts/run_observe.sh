#!/usr/bin/env bash
# Runs observe.py, auto-creating scripts/.venv and installing dependencies
# first if that hasn't happened yet -- so you don't need to manually
# repeat `python3 -m venv .venv && source .venv/bin/activate && pip
# install -r requirements.txt` before every run.
#
# Calls the venv's python binary directly (./.venv/bin/python3), not
# `source .venv/bin/activate` -- same convention dev.sh already uses for
# every other service in this project (`.venv/bin/uvicorn`, `.venv/bin/
# python3 -m app.worker`, etc.), so there's one consistent pattern across
# the whole repo rather than two.
#
# Uses this script's own location (not the caller's current directory) to
# find scripts/, so it works the same way regardless of where you run it
# from:
#   ./scripts/run_observe.sh
#   ./scripts/run_observe.sh --rate 2.0 --duration 30
#   cd scripts && ./run_observe.sh --vqe-weight 0.5

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if [ ! -d ".venv" ]; then
    echo "==> Creating venv..."
    python3 -m venv .venv
fi

echo "==> Installing/checking dependencies..."
./.venv/bin/pip install -q -r requirements.txt

exec ./.venv/bin/python3 observe.py "$@"