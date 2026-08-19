# Semantic experiment search with pgvector

## Purpose

The semantic index answers questions such as “which previous VQE runs had a
similar convergence curve?” It is also the retrieval layer for the planned AI
result interpreter. The vector index does not replace PostgreSQL experiment
records: PostgreSQL remains the source of truth and the vector table is a
rebuildable projection.

## Data flow

1. The API consumes an orchestrator result from RabbitMQ and commits it to the
   `experiments` table.
2. After the commit, the API publishes the complete record to Kafka topic
   `experiment-completed`, keyed by experiment ID.
3. `result-indexer` converts the record to stable, algorithm-specific text.
4. `sentence-transformers/all-MiniLM-L6-v2` produces a normalized 384-element
   embedding locally.
5. The worker upserts the text and vector into `experiment_embeddings`.
6. `GET /experiments/{id}/similar` performs cosine nearest-neighbour search.

Kafka is used because this is a replayable derived-data pipeline. RabbitMQ
continues to carry commands and execution results. A dedicated worker must not
consume `experiment-results`, because it would compete with the API for those
work-queue messages instead of receiving a copy.

## Canonical text

Raw JSON is deliberately not embedded. UUIDs, timestamps, field order and
incidental diagnostic fields would affect vectors without describing physical
or algorithmic similarity. The canonicalizer selects meaningful fields per
algorithm. For VQE these include molecule, energy, iteration count and energy
trajectory; for Grover they include targets and measurement counts; for QPE
they include the true and estimated phases.

The exact embedding model is stored in `embedding_model`. Vectors produced by
different models must not be compared as if they occupied the same vector
space. A future model change should re-index all records, then switch readers.

## API

```http
GET /experiments/{experiment_id}/similar?limit=10&same_algorithm=true
```

`same_algorithm=true` is the default: a VQE curve is normally more usefully
compared with other VQE curves than with a Grover histogram. Set it to `false`
for an unrestricted semantic search.

The similarity value is cosine similarity. Larger values indicate closer
embeddings; it is a ranking signal, not a calibrated probability.

## Local operation

Run `./dev.sh`. The first start downloads the local embedding model and can
therefore take longer than later starts. Watch `.dev-logs/result-indexer.log`
for `indexed experiment_id=...`.

Existing completed experiments are not automatically backfilled by this MVP.
New completion events are indexed. Because Kafka retains the topic, deleting
or changing the worker's consumer-group offset provides a basic replay path; a
purpose-built backfill command remains future work.

Example:

```bash
curl 'http://localhost:8000/experiments/EXPERIMENT_ID/similar?limit=5'
```

At least two indexed experiments of the same algorithm are needed for a
non-empty default response.

## Automated validation

With `./dev.sh` running, execute:

```bash
python3 scripts/validate_vector_search.py
```

The smoke test uses Grover so it completes much faster than a molecular VQE
run. It submits two semantically equivalent experiments and one distinct
experiment, waits for execution and indexing, then asserts that the duplicate
is returned by the similarity endpoint. It uses only Python's standard library
and returns a non-zero exit code with a diagnostic message on failure.

Other algorithm projections can be exercised explicitly:

```bash
python3 scripts/validate_vector_search.py --algorithm sat_grover
python3 scripts/validate_vector_search.py --algorithm qpe
python3 scripts/validate_vector_search.py \
  --algorithm vqe --vqe-molecule lih --vqe-max-iterations 3
```
