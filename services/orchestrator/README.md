# orchestrator

A worker that reads tasks from RabbitMQ (`experiments`), executes them
via `quantum_core`, and publishes the result back (`experiment-results`)
so the `api` service can update the experiment's status. Plus a periodic
calibration cycle that checks the backend's health.

Architecture details (why it's built this way, ack/reject semantics,
side effects for the API) live in
`docs/architecture/orchestration.md`.

## Structure

```
orchestrator/
├── requirements.txt
└── app/
    ├── worker.py             # thin shell: RabbitMQ connection + consume loop
    ├── retry_policy.py       # bounded retries + dead-letter queue on worker crash
    └── tasks/
        ├── run_experiment.py  # dispatches task.algorithm -> quantum_core.execution
        └── calibration.py     # periodic backend fidelity check
```

### `app/tasks/run_experiment.py`
`execute_task()` — dispatches by `task.algorithm`, calls
`quantum_core.execution` (the same shared module `api` used before
moving to the queue). Used to live directly in `worker.py`; split out so
`worker.py` stays thin, and so the dispatching logic can be tested with
zero RabbitMQ involvement (this function knows nothing about
`aio-pika` at all — only about `ExperimentTask`, a plain dataclass).

### `app/tasks/calibration.py`
Periodic backend check: runs a Bell state (the same `H`+`CX` as in
`demo_aer.py`, already confirmed working), computes `error_rate` — the
fraction of shots inconsistent with perfect entanglement (`01`/`10`
instead of the expected `00`/`11` only).

⚠️ **Honest limitation**: `AerBackend` is a noiseless simulator, so
`error_rate` currently always reads around `0.0` — there's physically
nowhere for calibration drift to come from. The module is still valuable
as a genuine health check (the backend responds, circuits behave as
expected), and this is the natural place to plug in an Aer noise model
or real hardware later on — the `error_rate` computation logic wouldn't
change.

Results are currently published to the `calibration-results` RabbitMQ
queue — a temporary stand-in for the Kafka telemetry stream discussed in
this project's very first architecture conversation (RabbitMQ for the
task queue, Kafka for time-series telemetry; see
`docs/architecture/orchestration.md`). Moving to Kafka shouldn't require
changing `run_calibration()` — only `publish_calibration_result()`.

Can be run once manually (no RabbitMQ needed, prints the result to
stdout):
```bash
python3 -m app.tasks.calibration
```

### `app/retry_policy.py`
A policy separate from `quantum_core.sync.polling` — that one handles
retrying individual backend calls *within* a single task; this one
handles what happens when the worker crashes **before** ack/reject-ing a
message at all (connection drop, unhandled exception). Without this
policy, RabbitMQ would redeliver such a message **forever** if it
reliably crashes the worker (a "poison message").

- `handle_redelivery()` — called first for every message; if
  `message.redelivered=True` (RabbitMQ already tried to deliver this
  message and got no ack/reject), decides whether to retry with
  exponential backoff (up to `MAX_RETRIES=3`, delays 2s/4s/8s) or send
  it to `experiments.dlq` (a dead-letter queue for manual triage);
  - the retry counter lives in the `x-retry-count` message header,
    tracked by the code itself (not relying on RabbitMQ's built-in
    `x-death`/TTL+DLX mechanism — simpler to verify the logic without a
    live broker).
- Malformed messages (JSON that doesn't parse) also end up in
  `experiments.dlq`, instead of silently vanishing as they did in the
  first version of `worker.py`.

### `app/worker.py`
Now just: the RabbitMQ connection, starting the background
`run_calibration_loop()` (every 5 minutes by default, configurable via
`CALIBRATION_INTERVAL_S`), and the main consume loop with
`prefetch_count=1` (one task at a time per worker — for parallelism, run
multiple `worker.py` processes rather than raising `prefetch_count`,
until it's actually measured to be the bottleneck).

VQE (the only synchronous algorithm in `quantum_core.execution`) is
offloaded via `asyncio.get_running_loop().run_in_executor()` — the same
trick as `run_in_threadpool` on the API side, just without Starlette.

## How to run it

Requires a running RabbitMQ (`docker compose up -d` from the repo root).

```bash
cd services/orchestrator
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python3 -m app.worker
```

⚠️ Specifically `python3 -m app.worker`, not `python3 app/worker.py` —
the latter fails with `ModuleNotFoundError: No module named 'app'`,
because the modules here use absolute imports (`from app import
retry_policy`), which need `services/orchestrator/` (the parent of
`app/`) on `sys.path`. `-m` adds it automatically; running the file
directly does not.

Environment variables: `RABBITMQ_URL` (default
`amqp://guest:guest@localhost/`), `CALIBRATION_INTERVAL_S` (default
`300` — every 5 minutes).

## ⚠️ Degree of verification

**Nothing here has actually been run** — I have neither `aio-pika`,
Docker, nor network access in my environment. The code was written
against `aio-pika`'s official docs (checked the current API via web
search) — including one specific point I double-checked and rewrote the
code because of: an explicit `bind()` on RabbitMQ's default exchange is
**forbidden** (`ACCESS_REFUSED`), since a queue is already automatically
reachable through the default exchange by its own name.

Checked the pure backoff/retry-count logic in `retry_policy.py` and the
`error_rate` arithmetic in `calibration.py` separately, without
`aio-pika`.

**Make sure to run the end-to-end scenario** (see
`docs/architecture/orchestration.md`, "Running the whole thing" section)
and send the results — including the worker's own logs: you should see
lines like `processing experiment_id=... algorithm=grover`,
`experiment_id=... -> completed`, and (if you wait 5 minutes, or
temporarily lower `CALIBRATION_INTERVAL_S` to check sooner) a line like
`calibration cycle: error_rate=0.0000 shots=1024`.
