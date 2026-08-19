# scripts

## `validate_vector_search.py`

Runs a dependency-free end-to-end smoke test of semantic experiment search.
With the full stack already running, execute:

```bash
python3 scripts/validate_vector_search.py
```

The script submits two equivalent Grover experiments and one distinct run,
waits for the orchestrator to finish them, waits for `result-indexer` to consume
their Kafka events, and verifies that pgvector returns the duplicate as a
neighbour with a valid cosine similarity. This exercises the complete path,
not only the database schema. It exits `0` on success and `1` on failure, so it
can also be used in CI or a shell pipeline.

Useful options:

```bash
python3 scripts/validate_vector_search.py --timeout 180
python3 scripts/validate_vector_search.py --api-url http://localhost:8000
python3 scripts/validate_vector_search.py --minimum-similarity 0.85
python3 scripts/validate_vector_search.py --algorithm sat_grover
python3 scripts/validate_vector_search.py --algorithm qpe
python3 scripts/validate_vector_search.py --algorithm vqe --vqe-molecule lih
```

Supported algorithms are `grover` (default), `sat_grover`, `qpe`, and `vqe`.
VQE defaults to three optimizer iterations and a 600-second timeout because it
is much slower. Tune it with `--vqe-molecule`, `--vqe-max-iterations`,
`--shots`, and `--timeout`. The default VQE similarity threshold is lower
(`0.75`) because shot noise and optimizer trajectories can differ between two
otherwise identical runs; other algorithms default to `0.90`.

## `observe.py`

Generates load via experiments through the API, tracks their statuses,
listens to the Kafka calibration topic, and periodically shows a
snapshot of Postgres and TimescaleDB — all in one terminal, as four
parallel async tasks.

Not a tool for rigorous load testing (no latency percentiles, no ramp-up
profiles) — the goal is to **see** how the system reacts to varying
load, not to measure it down to the microsecond. "Varying load" happens
naturally here from the mix of algorithms: Grover/SAT-Grover/QPE resolve
quickly (<1s), VQE takes noticeably longer — with a mixed submission
pattern you can see the `orchestrator`'s single-worker queue (see
`docs/architecture/orchestration.md`) visibly "stall" behind a long VQE
run.

### How to run it

Requires the full stack running (`./dev.sh` from the repo root).

```bash
./scripts/run_observe.sh                                    # defaults: 1 exp/sec, 20 seconds
./scripts/run_observe.sh --rate 2.0 --duration 30            # more intense and longer
./scripts/run_observe.sh --vqe-weight 0.5 --grover-weight 0.5 --sat-grover-weight 0 --qpe-weight 0  # almost all VQE — see the queue backlog clearly
./scripts/run_observe.sh --vqe-molecule h2 --vqe-weight 1 --grover-weight 0 --sat-grover-weight 0 --qpe-weight 0
./scripts/run_observe.sh --vqe-molecule lih --vqe-shots 1024 --vqe-max-iterations 5 --vqe-weight 1 --grover-weight 0 --sat-grover-weight 0 --qpe-weight 0 --rate 0.05 --duration 5
./scripts/run_observe.sh --vqe-molecule lih --vqe-weight 1 --grover-weight 0 --sat-grover-weight 0 --qpe-weight 0
./scripts/run_observe.sh --vqe-molecule beh2 --vqe-weight 1 --grover-weight 0 --sat-grover-weight 0 --qpe-weight 0
```

`--vqe-molecule` accepts `h2`, `lih`, `beh2`, or `mixed`. The default is
`h2`. `mixed` selects a molecule independently for every VQE submission.
LiH and BeH2 Hamiltonians are generated once per orchestrator process from
the pinned PySCF/Qiskit Nature configuration and then cached.

The observer deliberately uses smaller VQE defaults than the API:
`--vqe-shots 1024` and `--vqe-max-iterations 5`. Larger molecules contain
many more Pauli terms than H2, so the API defaults (8192 shots and 80
iterations) are unsuitable for a quick mixed-load demonstration.

`run_observe.sh` creates its own `.venv` and installs dependencies on
first run (and just quickly re-checks them on later runs), so you don't
have to manually repeat
`python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt`
every time. Same pattern `dev.sh` already uses for the other services —
calling binaries directly from `.venv/bin/`, without `source activate`.

If you prefer to run it manually (e.g. from an already-activated venv)
— `observe.py` can still be invoked directly as before:
```bash
cd scripts
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python3 observe.py --rate 2.0
```

Flags: `--rate` (exp/sec), `--duration` (how many seconds to submit
for), `--grover-weight`/`--sat-grover-weight`/`--qpe-weight`/
`--vqe-weight` — relative algorithm weights for random sampling (don't
need to sum to 1 — they're just weights for `random.choices`).

The script exits on its own once every submitted experiment reaches a
terminal status (or after `--max-wait` seconds, 180 by default, if
something gets stuck — e.g. `orchestrator` isn't running).

### What it prints

- `[submit] <algorithm> id=<uuid>` — request submitted
- `[status] <algorithm> id=<uuid> -> completed (0.34s)` — status
  changed, with execution time
- `[kafka] backend=... error_rate=... shots=...` — a calibration event
  from Kafka (via its own dedicated consumer group — doesn't compete
  with `stream-analytics`)
- `[postgres] grover/completed=12, vqe/queued=1, ...` — a snapshot of
  experiment metadata
- `[timescale] events=8 avg_error_rate=0.0000` — a snapshot of
  calibration telemetry

### ⚠️ Degree of verification

`observe.py` has already been confirmed working against the real stack
(see the history — a full run, 20 experiments, all reached
`completed`, including a visible VQE backlog in the queue). Along the
way, found and worked around an open `aiokafka` bug (`consumer.getone()`
+ `asyncio.wait_for()` = hang, `aio-libs/aiokafka#712`) — replaced with
`consumer.getmany(timeout_ms=...)`.

`run_observe.sh` is new; its syntax has been checked (`bash -n`), but
the actual venv-creation/dependency-install step hasn't been run against
a real network in my environment — though the logic is identical to the
already-verified and confirmed `setup_venv()` pattern from `dev.sh`.
