# Storage architecture

The platform separates transactional experiment state, append-only telemetry,
broker retention, and future semantic-search data because they have different
access patterns and lifecycles.

## Storage map

| Data | System | Access pattern | Source of truth |
|---|---|---|---|
| Experiment status and result | PostgreSQL `quantum_platform` | point lookup, list/filter, upsert by ID | Yes |
| Calibration and VQE telemetry | TimescaleDB `telemetry` | append, time-range query, aggregation | Yes for historical telemetry |
| Pending commands/results | RabbitMQ | enqueue, competing consume, ack/redeliver | No; transport state |
| Retained event streams | Kafka | append, replay by consumer group | Event log within retention |
| Faust aggregation state | Kafka changelog + in-memory table | keyed/windowed updates | Rebuildable derived state |
| Metrics for dashboards | Prometheus | scrape and time-range query | Operational metrics only |
| AI interpretations and vectors | PostgreSQL + pgvector (planned) | upsert, vector similarity, metadata filters | Yes |

The repository does not currently use the MongoDB, InfluxDB, MinIO, Redis, or
Celery stack shown in older design sketches.

## PostgreSQL: experiment records

The API's `experiments` table stores a string UUID primary key, algorithm,
status, timezone-aware timestamps, algorithm-specific result as `JSONB`, and
error text.

`PostgresExperimentStore.save()` uses `INSERT ... ON CONFLICT DO UPDATE`, so a
queued record and its later completed result share one stable identity. The
in-memory implementation remains a fallback for tests and local execution
without `DATABASE_URL`, but it is not durable.

VQE result JSON needs enough context to be self-describing: molecule name,
geometry, mapping, qubit count, optimal parameters, electronic energy, nuclear
repulsion, total energy, reference energy, and iteration history.

## TimescaleDB: telemetry

TimescaleDB runs as a separate PostgreSQL-compatible service on host port 5433.
Its hypertables are append-oriented:

- `calibration_events`;
- `vqe_iteration_metrics`;
- `vqe_window_metrics`.

Separating telemetry from OLTP experiment records allows independent retention,
indexes, hypertable policies, and query load. Grafana reads these tables
directly. Raw VQE parameters are `JSONB` because ansatz size changes with the
molecule's qubit count.

## Why Kafka is not the permanent experiment database

Kafka retains an ordered event log and supports replay, but the API needs
current-state queries such as “get experiment by ID” and “list completed VQE
runs.” Reconstructing this state from Kafka for every request would be the
wrong access model. PostgreSQL stores current durable state; Kafka distributes
events to independent processors.

## Planned interpretation and vector schema

The AI feature should use a separate table instead of placing mutable model
output directly in the core `experiments` row:

```text
experiment_interpretations
  id                       UUID/string PK
  experiment_id            FK -> experiments.id
  interpreter_version      text
  model_provider           text
  model_name               text
  status                   pending/completed/failed
  summary                  text
  structured_analysis      JSONB
  embedding                vector(N)
  created_at               TIMESTAMPTZ
  updated_at               TIMESTAMPTZ
  UNIQUE(experiment_id, interpreter_version)
```

A separate table allows interpretations to be regenerated without rewriting
scientific results, several model versions to coexist, failures and retries to
have their own lifecycle, embeddings to be regenerated, and provenance to
remain queryable.

The vector dimension `N` must be fixed by the selected embedding model and
recorded in migration/configuration. A vector must not silently be reused after
changing embedding models.

## What is embedded

Embed a normalized, algorithm-specific document, not the raw JSON dump. For a
VQE experiment it should include molecule/model identity, final and reference
energies, convergence features, timing/retry anomalies, and the generated
summary. Large raw histories remain in `experiments.result` and TimescaleDB;
the embedding input contains derived features such as final slope, best energy,
energy error, oscillation measure, and iteration count.

Similarity queries should combine vector distance with structured filters:

```sql
WHERE algorithm = 'vqe' AND molecule = 'lih'
ORDER BY embedding <=> :query_embedding
LIMIT :k
```

Comparing H₂ and BeH₂ solely by vector proximity can be semantically
interesting, but it is not a physically controlled comparison unless geometry,
basis, mapping, and active-space metadata are included.

## Retention and reproducibility

- Experiment results and interpretations are durable research records.
- Telemetry retention may be shorter and policy-driven.
- Kafka retention is not a backup policy.
- Every generated molecular Hamiltonian should store its generation metadata
  and a deterministic content hash.
- AI records must retain interpreter, prompt/schema, LLM, and embedding-model
  versions so a later result can be reproduced or explained.

## Migrations

The API database uses Alembic. The pgvector extension and interpretation table
belong in a new Alembic revision. TimescaleDB initialization SQL remains under
`services/stream-analytics/init/` because those schemas are owned by the
telemetry service and initialized with its separate database.
