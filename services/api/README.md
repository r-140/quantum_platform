# api

A thin FastAPI layer that accepts requests for quantum experiments
(Grover, SAT-Grover, QPE, VQE).

**With RabbitMQ/orchestrator now in the picture (see
`docs/architecture/orchestration.md`), the API no longer executes
experiments itself.** `POST /experiments` publishes a task to the queue
and immediately returns `202 Accepted` with status `queued`. A separate
`orchestrator` service executes the experiment; once the result is
ready, the API finds out via the `experiment-results` queue and updates
its store. `GET /experiments/{id}` shows the current status.

## Structure

```
api/
├── requirements.txt
└── app/
    ├── main.py                  # entry point, lifespan (RabbitMQ), results consumer
    ├── deps.py                  # experiment store + RabbitMQ publish_task
    ├── schemas/
    │   └── experiments.py       # Pydantic models: discriminated union by algorithm
    └── routers/
        ├── experiments.py       # POST (publishes a task) / GET /experiments
        └── backends.py          # GET /backends (informational)
```

Note: `app/execution.py` has been **removed** — the business logic for
running algorithms moved into `quantum_core/execution.py`, a module
shared between `api` and `orchestrator` (plain Python types, no
Pydantic). The API no longer imports `quantum_core.algorithms.*`
directly at all.

### `app/schemas/experiments.py`
`ExperimentRequest` — a discriminated union of 4 models
(`GroverRequest`/`SatGroverRequest`/`QPERequest`/`VQERequest`),
distinguished by the `algorithm` field.

⚠️ The discriminator is a string literal (`Literal["grover"]`), not an
enum member (`Literal[Algorithm.GROVER]`) — the latter has a documented
OpenAPI-schema-generation bug in Pydantic.

`ExperimentStatus` now includes `queued` (not just
`completed`/`failed`) — reflecting the fact that execution is now
asynchronous.

### `app/deps.py`
- `ExperimentStore` — in-memory, thread-safe (`threading.Lock`).
  ⚠️ Temporary simplification: doesn't survive a process restart,
  doesn't work correctly with multiple uvicorn workers — this is meant
  to be closed by Postgres once we get to the storage layer;
- `get_backend()` — kept around (lazy `AerBackend` import, doesn't pull
  in Qiskit at module load time), but currently unused by the router —
  just available for the future (debug/sync mode, tests);
- `init_rabbitmq()`/`close_rabbitmq()`/`publish_task()`/`get_rabbitmq_channel()`
  — the RabbitMQ connection as a process-lifetime singleton, initialized
  via `lifespan` in `main.py`.

### `app/main.py`
`lifespan` connects to RabbitMQ on startup, starts the
`consume_results()` background task (listens to `experiment-results`,
updates the store via `apply_result_message()`), and cleanly shuts
everything down on exit.

### `app/routers/experiments.py`
`POST /experiments` saves the record with status `queued` **before**
publishing to the queue (so the results consumer doesn't miss a fast
response if it arrives before the record-writing function finishes) —
then publishes an `ExperimentTask`. A publish failure (e.g. RabbitMQ
unreachable) also doesn't produce a 500 — instead a `FAILED` record with
a clear reason.

## Unit tests

```
tests/
├── conftest.py                # client fixture (TestClient) + fresh_store
├── test_store.py               # ExperimentStore directly
├── test_experiments_router.py  # dispatch (publish_task mocked), enqueue errors, GET
├── test_results_consumer.py    # apply_result_message() -- updating the store from a result
└── test_validation.py          # edge cases of the Pydantic schemas
```

⚠️ **Important nuance**: `TestClient(app)` in `conftest.py` is used
**without** a `with` block — deliberately: without `with`, Starlette
**doesn't run `lifespan`**, meaning the tests never try to establish a
real RabbitMQ connection. All tests touching task publishing stub out
`app.deps.publish_task` directly — real RabbitMQ isn't needed for unit
tests at all. If a test ever needs lifespan state, switching to
`with TestClient(app) as client:` will trigger a real connection
attempt — worth keeping in mind.

Another result of the refactor: since `execution.py` is gone and the
router doesn't touch `quantum_core.algorithms.*` — **the entire API test
suite no longer requires Qiskit to be installed** (only
`fastapi`/`pydantic`/`httpx`).

```bash
cd services/api
source .venv/bin/activate
pytest tests/ -v
```

## ⚠️ Degree of verification

The synchronous version of this service (before moving to the queue) was
**confirmed working** — run by hand via `curl` for all four algorithms.
Everything added during the RabbitMQ migration (lifespan, publish_task,
consume_results, the new tests) **hasn't been run**: I have neither
`aio-pika`, Docker, nor network access. Run `pytest tests/ -v`, then the
full end-to-end scenario (see `docs/architecture/orchestration.md`,
"Running the whole thing" section — you'll also need `orchestrator` for
that).

Interactive docs: `http://localhost:8000/docs`.
