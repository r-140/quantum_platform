# Postgres: experiment persistence

## What changed

`ExperimentStore` used to be in-memory (`threading.Lock` + dict), didn't
survive a process restart, and wouldn't have worked correctly with
multiple uvicorn workers. Now it's an abstraction (mirroring
`QuantumBackend` in `quantum_core`) with two implementations:

- **`InMemoryExperimentStore`** — as before, but now `asyncio.Lock`
  instead of `threading.Lock` (rationale below), used by default and in
  tests;
- **`PostgresExperimentStore`** — real persistence via SQLAlchemy 2.0
  async + `asyncpg`, enabled through the `DATABASE_URL` environment
  variable.

Routers and `main.py` depend only on the abstract `ExperimentStore`
(`app/store/base.py`), never on a concrete implementation directly —
`app/deps.get_store()` decides which one to return based on
`DATABASE_URL`.

## Why `asyncio.Lock`, not `threading.Lock`, in the in-memory store

`threading.Lock` used to be the right call — the VQE endpoint ran in a
threadpool thread (`run_in_threadpool`), so access to the store really
was multi-threaded. After moving to a queue (RabbitMQ), the API doesn't
execute anything at all anymore — `run_in_threadpool` for VQE is no
longer needed (see `docs/architecture/orchestration.md`). That means all
store access now happens from coroutines on a single event loop, and
`asyncio.Lock` is the more idiomatic choice for purely async code.

## Schema

A single `experiments` table
(`migrations/versions/0001_create_experiments_table.py`):

| column | type | note |
|---|---|---|
| `id` | `String` | PK; a `uuid.uuid4()` string, not a native Postgres `UUID` — see rationale in `models.py` |
| `algorithm` | `String` | |
| `status` | `String` | `queued`/`completed`/`failed` |
| `submitted_at` | `TIMESTAMPTZ` | indexed — for `list_all()`/future "recent experiments" queries |
| `completed_at` | `TIMESTAMPTZ`, nullable | |
| `result` | `JSONB`, nullable | native Postgres indexing/query support, though nothing queries *into* the JSON yet |
| `error` | `Text`, nullable | |

`save()` isn't "check if a row exists, then insert/update" — it's a
single `INSERT ... ON CONFLICT (id) DO UPDATE` (SQLAlchemy:
`postgresql.insert(...).on_conflict_do_update(...)`) — no race between
checking existence and writing.

## How this was verified

As with the very first Grover math in this project: before writing
Postgres-specific code, I separately verified the **logic** of the SQL
(not Postgres itself) using `sqlite3` (stdlib, available without
network) — INSERT, `ON CONFLICT DO UPDATE`, SELECT by id, ORDER BY. All
5 scenarios passed, including that `submitted_at` isn't overwritten on
an update via upsert.

⚠️ Honest caveat: sqlite ≠ Postgres. Not covered by this check:
- `JSONB` vs. `TEXT` (in sqlite, JSON was stored via manual
  `json.dumps`; in Postgres a `JSONB` column serializes/deserializes a
  `dict` natively, with no manual `json.dumps` call in the code —
  SQLAlchemy does this transparently through the dialect);
- `UUID`/`TIMESTAMPTZ` types;
- async driver (`asyncpg`) behavior — not testable on sqlite in
  principle.

**This caveat was confirmed in practice**: the very first real run
against actual Postgres crashed with
`TypeError: can't subtract offset-naive and offset-aware datetimes`. The
cause was a mismatch between the migration (`sa.DateTime(timezone=True)`,
which creates a `TIMESTAMPTZ`) and the ORM model (`Mapped[datetime]`
without an explicit `DateTime(timezone=True)`, which SQLAlchemy maps to
a **naive** `DateTime` by default). This was visible directly in the
generated SQL: `$4::TIMESTAMP WITHOUT TIME ZONE` — a cast based on the
model's type, not the DB table's actual schema. `datetime.now(timezone.utc)`
(offset-aware) didn't fit into that cast. This is exactly the class of
bug sqlite could never have caught — it has no aware/naive timestamp
distinction at all. Fixed by adding an explicit `DateTime(timezone=True)`
to `models.py` for both columns (`submitted_at`, `completed_at`); the
actual DB table had already been created correctly before this — no
need to re-roll the migration, just restart `api` with the fixed model.

Separately checked the current SQLAlchemy 2.0 async API
(`create_async_engine`, `async_sessionmaker`, `AsyncSession`) and the
upsert syntax
(`sqlalchemy.dialects.postgresql.insert(...).on_conflict_do_update(...)`)
via web search against the docs — didn't rely on memory, given how often
this API changes between major versions.

**The logic of `InMemoryExperimentStore` and `apply_result_message`**
itself (including the upsert pattern, preserving `submitted_at` on
update, concurrent access via `asyncio.gather`) was run directly in a
sandbox, with Pydantic-dependent imports (`ExperimentResponse`) swapped
for duck-typed stubs with the right semantics (`model_copy`), since
`pydantic` itself isn't available in my environment. This isn't a full
substitute for a real run, but it catches logic errors (e.g. in the
upsert formula itself, or argument ordering) before the code ever sees
a real Postgres.

⚠️ **Not verified at all**: the `asyncpg`/SQLAlchemy code itself against
real Postgres, the Alembic migration (`alembic upgrade head`), the whole
async bridge in `migrations/env.py`. I have neither `sqlalchemy`,
`asyncpg`, `alembic`, Docker, nor network access.

## Alembic: the async bridge

Alembic generates an `env.py` by default that's built for a synchronous
engine — `engine_from_config()` doesn't work with `asyncpg`. Used the
documented SQLAlchemy pattern: the migration itself
(`context.run_migrations()`) stays synchronous code, but is invoked via
`AsyncConnection.run_sync(...)` from inside the engine's async context
(`migrations/env.py`).

`DATABASE_URL` is read from an environment variable, not from
`alembic.ini` — so the connection string doesn't get committed and the
`.ini` doesn't need editing when the environment changes.

⚠️ **Second time in this project**: `migrations/env.py` does
`from app.store.models import Base` — an absolute import that needs
`services/api/` (the parent of `app/`) on `sys.path`. The console script
`alembic upgrade head` doesn't provide that (the same reason
`python3 app/worker.py` in `orchestrator` used to fail with
`ModuleNotFoundError: No module named 'app'` — see
`docs/architecture/orchestration.md`). Same fix: run via
`python3 -m alembic upgrade head` instead of a bare
`alembic upgrade head` — `-m` puts the current directory at the front of
`sys.path` (confirmed against the official Python docs and reproduced on
a minimal example before fixing `dev.sh`). Already wired into `./dev.sh`.

## How to run it

Already wired into `./dev.sh` (brings up Postgres, waits for the
healthcheck, runs `alembic upgrade head`, starts `api` with
`DATABASE_URL` set in the environment).

Manually:

```bash
docker compose up -d postgres
cd services/api
source .venv/bin/activate
pip install -r requirements.txt

export DATABASE_URL="postgresql+asyncpg://quantum:quantum@localhost:5432/quantum_platform"
python3 -m alembic upgrade head

uvicorn app.main:app --reload --port 8000
```

Without `DATABASE_URL` set, the API still starts and works fine — just
on the in-memory store (a startup log warns about this explicitly, not
silently).

## Not yet implemented

- Connection pooling hasn't been tuned under load (default
  `create_async_engine` settings) — there's no benchmark yet that would
  justify doing so at this stage;
- `orchestrator` doesn't know anything about Postgres yet — it doesn't
  store state itself, it only reads/publishes to RabbitMQ; if we later
  need to store a history of calibration results (rather than just the
  latest snapshot in the queue), that's also a likely candidate for its
  own table.
