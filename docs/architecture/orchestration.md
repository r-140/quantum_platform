# Orchestration: API → RabbitMQ → orchestrator

## What changed

Before this step, `POST /experiments` ran the experiment **synchronously
inside the API process** — the client waited for the entire execution
(~55 seconds for VQE). Now:

1. `POST /experiments` publishes a task to the `experiments` queue and
   immediately returns `202 Accepted` with status `queued`.
2. `orchestrator` (a separate process/service) reads the queue, executes
   the experiment via `quantum_core`, and publishes the result to the
   `experiment-results` queue.
3. The API listens to `experiment-results` in the background and updates
   its in-memory store. `GET /experiments/{id}` returns the current
   status — `queued` until the result arrives, then
   `completed`/`failed`.

## Why RabbitMQ, not Kafka — for this particular part

Recapping the ADR from the very start of the project: for *tasks* (a
task queue — "run this experiment once") RabbitMQ is the better fit —
it has proper per-message ack/retry out of the box, and the "one task,
one processing" semantics match it naturally. Kafka remains the
candidate for *telemetry* (calibration metrics, real-time analytics) —
that's a separate, not-yet-implemented stream, see `docs/decisions/`.

## Shared code between API and orchestrator

The business logic for running algorithms (`run_grover`,
`run_sat_grover`, `run_qpe`, `run_vqe_sync`) moved into
`quantum_core/execution.py` — a shared module using plain Python types
(no Pydantic) that both services use:

- `services/api/app/routers/experiments.py` — now just publishes a task
  and **doesn't** call `quantum_core.execution` directly at all;
- `services/orchestrator/app/worker.py` — calls `quantum_core.execution`
  directly, parsing `params` from the queue message.

Message format lives in `quantum_core/tasks.py` (`ExperimentTask`,
`ExperimentResultMessage`) — plain dataclasses + JSON, no Pydantic, so as
not to drag an HTTP framework into `quantum_core`.

## Side effect: the API got a lot thinner

Previously, `POST /experiments` for VQE needed `run_in_threadpool` (so
the synchronous `run_vqe` wouldn't block FastAPI's event loop). Now the
API doesn't execute algorithms at all — it just publishes JSON to the
queue. All the sync/async bridging concern for VQE (`run_vqe` is
synchronous and uses `asyncio.run()` internally) moved into
`orchestrator`, which handles it via `loop.run_in_executor()` — the
direct asyncio equivalent of the same `run_in_threadpool`.

A more surprising side effect: since `api/app/execution.py` is gone, and
the router no longer imports `quantum_core.algorithms.*` directly — **the
entire API service is now testable without Qiskit installed**. The only
place Qiskit is even mentioned in the API is a lazy import inside
`get_backend()` (`app/deps.py`), which the experiments router doesn't
use at all anymore.

## ack/reject/retry semantics in the orchestrator

Three different cases are handled differently:

1. **Malformed message** (JSON doesn't parse, unknown algorithm) — sent
   to the `experiments.dlq` dead-letter queue
   (`retry_policy.send_to_dead_letter_queue`), the original message gets
   `ack()`'d. Reprocessing the same message would give the same result —
   there's no point retrying, but silently dropping it isn't right
   either (the first version of `worker.py` did exactly that — this was
   fixed).
2. **Algorithm execution error** (a circuit that fails, a backend
   timeout) — the task is considered handled: the result is recorded as
   `failed` and sent to `experiment-results`, the original message gets
   `ack()`'d. This is a legitimate outcome, not a queue failure — no
   `retry_policy` applies here at all.
3. **The worker itself crashing** (connection drop, unhandled exception
   before ack/reject is called) — RabbitMQ automatically redelivers the
   message (`message.redelivered=True`). Without a policy, this could go
   on **forever** if a specific message reliably crashes the worker.
   `retry_policy.handle_redelivery()` caps this at three attempts with
   exponential backoff (2s/4s/8s, tracked via the `x-retry-count`
   header), after which the message also goes to `experiments.dlq`.

This is a separate policy from the retry/backoff in
`quantum_core/sync/polling.py` — that one handles retrying individual
backend calls *within* a single task (submit/poll/fetch against a flaky
but otherwise functioning `QuantumBackend`); `retry_policy.py` handles
the case where the worker process itself crashes, which backend-level
retry can't fix by definition.

## `orchestrator` structure: tasks/ and calibration

After the first version, `worker.py` contained all the logic (RabbitMQ
connection, dispatching by algorithm, message handling) in one file.
Split out separately:

- `app/tasks/run_experiment.py` — dispatches `task.algorithm` to
  `quantum_core.execution`. Knows nothing about `aio-pika` at all (only
  takes an `ExperimentTask`, a plain dataclass) — testable without
  RabbitMQ.
- `app/tasks/calibration.py` — periodic backend check via a Bell state
  (`error_rate` = fraction of shots landing on `01`/`10` instead of the
  expected `00`/`11`). Publishes to the `calibration-results` queue — a
  temporary stand-in for the Kafka telemetry stream from the very first
  architecture conversation of this project. Honest caveat: `AerBackend`
  is noiseless, so `error_rate` is currently always ~0 — this is a valid
  health check ("the backend responds"), but not a source of signal
  about real drift, until a noise model or actual hardware is wired in.
- `app/worker.py` — now just the RabbitMQ connection, starting the
  calibration cycle as a background task, and the main consume loop.

## ⚠️ Degree of verification

As with the API layer — **none of the RabbitMQ code has actually been
run**: I have neither `aio-pika`, Docker, nor network access. One
specific thing worth calling out that I double-checked via web search
before writing the code (rather than relying on memory): calling
`queue.bind()` on the default exchange in RabbitMQ **fails** with
`ACCESS_REFUSED` ("operation not permitted on the default exchange") —
the default exchange already routes by queue name automatically, an
explicit bind isn't needed and isn't allowed. The first version of
`worker.py` had this bug — fixed before the code was shown.

`quantum_core/tasks.py` (task/result serialization) was verified and run
locally (JSON round-trip, including the failure case) with zero external
dependencies, pure stdlib. The `error_rate` arithmetic in
`calibration.py` was also verified separately (trivial, but for
consistency with the rest of the project).

## Running the whole thing

```bash
# from the repo root
docker compose up -d          # brings up RabbitMQ, UI on localhost:15672 (guest/guest)

# terminal 1 — API
cd services/api
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000

# terminal 2 — orchestrator
cd services/orchestrator
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python3 -m app.worker

# terminal 3 — request
curl -X POST http://localhost:8000/experiments \
  -H "Content-Type: application/json" \
  -d '{"algorithm": "grover", "marked_states": ["101"]}'
# immediate response: {"status": "queued", "id": "...", ...}

curl http://localhost:8000/experiments/<id>
# a moment later: {"status": "completed", "result": {...}}
```

## Not yet implemented

- Persistence (Postgres) for the experiments store — currently still
  in-memory on the API side, doesn't survive a restart;
- Multiple orchestrator workers for parallel processing (currently
  `prefetch_count=1`, one worker = one task at a time);
- `retry_policy.py` holds the delay between retries in the worker's
  event loop (`asyncio.sleep`) — with multiple workers this doesn't let
  the message be picked up by another worker during the delay;
  acceptable for a single worker, worth revisiting when scaling;
- A noise model for `AerBackend` — without it, `calibration.py` won't
  see real drift (`error_rate` is always ~0), it only confirms that the
  backend is responding at all;
- The Kafka telemetry stream — `calibration-results` is currently just
  another RabbitMQ queue, not a real time-series stream, as discussed in
  the project's first ADR.
