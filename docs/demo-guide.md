# Quantum Platform demo guide

This guide is a repeatable playbook for demonstrating the platform after a
long break. It starts with a short presentation path, then provides deeper
scenarios and troubleshooting.

## 1. What the demo shows

The platform accepts quantum experiments through an API, executes them through
an asynchronous backend abstraction, and exposes both operational and
algorithm-specific telemetry.

```mermaid
flowchart TD
    U["API or load generator"] --> R["RabbitMQ task queue"]
    R --> O["Orchestrator"]
    O --> Q["Qiskit Aer backend"]
    O --> K["Kafka telemetry"]
    K --> S["Stream analytics"]
    S --> T["TimescaleDB"]
    T --> G["Grafana"]
    O --> A["API result queue"]
    A --> P["PostgreSQL"]
    K --> V["Result indexer and pgvector"]
```

The main stories are:

- asynchronous experiment submission and queueing;
- Grover, SAT-Grover, QPE, and molecular VQE execution;
- H2, LiH, and BeH2 Hamiltonians;
- raw and windowed VQE telemetry;
- calibration-aware VQE gating;
- semantic search over completed experiments;
- RabbitMQ, Kafka, PostgreSQL, TimescaleDB, Prometheus, and Grafana working
  together for different purposes.

## 2. Prerequisites

- Docker with the Compose plugin;
- Python 3.11 or newer;
- ports `5432`, `5433`, `5672`, `8000`, `8090`, `8091`, `9090`, `9092`,
  `15672`, and `3001` available;
- internet access on the first vector-indexer start so the local embedding
  model can be downloaded.

Run commands from the repository root unless a section says otherwise.

## 3. Start and verify the platform

For the first run after changing code:

```bash
./dev.sh --profile=verify
```

For later demo runs:

```bash
./dev.sh
```

`dev.sh` starts Docker infrastructure, waits for health checks, installs each
service's dependencies, runs Alembic migrations, and starts:

- API;
- orchestrator;
- plain stream-analytics consumer;
- result indexer.

Leave this terminal running. The application logs are also written under
`.dev-logs/`.

### Preflight checks

In a second terminal:

```bash
curl http://localhost:8000/health
docker compose ps
tail -n 30 .dev-logs/api.log
tail -n 30 .dev-logs/orchestrator.log
tail -n 30 .dev-logs/stream-analytics.log
tail -n 30 .dev-logs/result-indexer.log
```

Expected API response:

```json
{"status":"ok"}
```

### User interfaces

| Interface | URL | Credentials / purpose |
|---|---|---|
| Experiment dashboard | <http://localhost:8000/dashboard/> | experiment status and results |
| OpenAPI | <http://localhost:8000/docs> | submit and inspect API requests |
| Grafana | <http://localhost:3001> | `admin` / `admin` |
| RabbitMQ management | <http://localhost:15672> | `guest` / `guest` |
| Kafka UI | <http://localhost:8090> | topics, records, consumer groups |
| Adminer | <http://localhost:8091> | PostgreSQL/TimescaleDB inspection |
| Prometheus | <http://localhost:9090> | raw operational metrics |

## 4. Recommended ten-minute demo

Use this sequence when time is limited.

1. Open the experiment dashboard, RabbitMQ UI, Kafka UI, and Grafana.
2. Submit a mixed fast workload with no VQE:

   ```bash
   ./scripts/run_observe.sh \
     --duration 10 --rate 1 \
     --grover-weight 1 --sat-grover-weight 1 --qpe-weight 1 --vqe-weight 0
   ```

3. Show queued/completed transitions in the experiment dashboard.
4. Show the `experiments` and `experiment-results` RabbitMQ queues.
5. Start the Faust worker as described in the next section.
6. Submit a small H2 VQE and show its convergence in Grafana.
7. Run the vector-search validator.
8. Run the calibration-gate validator and show
   `waiting_for_calibration` followed by automatic resumption.

The narrative is more important than the number of experiments: RabbitMQ is
the command path, Kafka is the replayable telemetry path, PostgreSQL stores
current business state, TimescaleDB stores time-series observations, and
pgvector provides semantic retrieval.

## 5. Start Faust for window metrics

`dev.sh` does **not** start the Faust worker. Start it in another terminal
before submitting the VQE runs whose window metrics you want to observe:

```bash
cd services/stream-analytics
.venv/bin/python3 -m app.faust_app worker -l info
```

The Faust dashboard is then available at:

<http://localhost:6066/dashboard/>

Faust consumes `vqe-iteration-metrics`, maintains a 60-second tumbling table,
and publishes `vqe-window-metrics`. The plain consumer started by `dev.sh`
persists both raw and derived events to TimescaleDB.

## 6. Scenario: individual algorithms

### Grover

```bash
curl -X POST http://localhost:8000/experiments \
  -H 'Content-Type: application/json' \
  -d '{"algorithm":"grover","marked_states":["101"],"shots":512}'
```

Expected result: the marked state dominates the measurement histogram.

### SAT-Grover

```bash
curl -X POST http://localhost:8000/experiments \
  -H 'Content-Type: application/json' \
  -d '{"algorithm":"sat_grover","variables":["x0","x1"],"expression":"x0 & x1","shots":512}'
```

Expected result: satisfying assignments and their measurement counts.

### QPE

```bash
curl -X POST http://localhost:8000/experiments \
  -H 'Content-Type: application/json' \
  -d '{"algorithm":"qpe","phi":0.125,"num_counting_qubits":3,"shots":512}'
```

Expected result: the dominant bit string corresponds to a phase estimate near
`0.125` at resolution `1/8`.

Copy an experiment ID from a response and inspect it with:

```bash
curl http://localhost:8000/experiments/EXPERIMENT_ID | python3 -m json.tool
```

## 7. Scenario: molecular VQE

For a short, deterministic demo, submit directly to the API so that shots and
optimizer iterations are explicit. LiH and especially BeH2 have more Pauli
terms than H2 and take longer.

```bash
curl -sS -X POST http://localhost:8000/experiments \
  -H 'Content-Type: application/json' \
  -d '{"algorithm":"vqe","molecule":"h2","shots":256,"max_iterations":3}' \
  | python3 -m json.tool
```

Repeat by changing `"molecule":"h2"` to `"lih"` or `"beh2"`.

To demonstrate sustained VQE load and let `observe.py` monitor completion,
use its molecule selector. The script currently submits VQE with its built-in
`max_iterations=20`:

```bash
./scripts/run_observe.sh \
  --vqe-molecule lih --vqe-weight 1 \
  --grover-weight 0 --sat-grover-weight 0 --qpe-weight 0 \
  --rate 0.05 --duration 5 --max-wait 300
```

Observe:

- molecule name and parameters immediately on the experiment dashboard;
- total/reference energy in the completed result;
- `vqe-iteration-metrics` in Kafka UI;
- the **VQE Overview** and **VQE Window Metrics** Grafana dashboards;
- raw rows in `vqe_iteration_metrics` and derived rows in
  `vqe_window_metrics`.

Useful SQL check:

```bash
docker compose exec -T timescaledb psql -U quantum -d telemetry -c \
  "SELECT experiment_id, iteration, energy, quantum_time_s, classical_time_s
   FROM vqe_iteration_metrics ORDER BY time DESC LIMIT 10;"
```

## 8. Scenario: queue backlog

The orchestrator consumes one task at a time. A VQE run therefore makes queue
behavior visible:

```bash
./scripts/run_observe.sh \
  --duration 15 --rate 2 \
  --vqe-weight 0.3 --grover-weight 0.4 \
  --sat-grover-weight 0.15 --qpe-weight 0.15
```

Watch the RabbitMQ queue depth and the observer's PostgreSQL status summary.
This demonstrates head-of-line blocking without requiring artificial sleeps.

## 9. Scenario: semantic experiment search

Fast default validation:

```bash
python3 scripts/validate_vector_search.py
```

If your checkout contains the parameterized validator, other projections are:

```bash
python3 scripts/validate_vector_search.py --algorithm sat_grover
python3 scripts/validate_vector_search.py --algorithm qpe
python3 scripts/validate_vector_search.py \
  --algorithm vqe --vqe-molecule lih --shots 256 --vqe-max-iterations 3
```

Confirm supported options with
`python3 scripts/validate_vector_search.py --help`. On an older checkout that
only exposes connection and timeout options, the default Grover validation is
still valid; apply the parameterization patch before using the examples above.

The script submits two equivalent experiments and one different experiment,
waits for Kafka indexing, and verifies that pgvector returns the duplicate as
a semantic neighbour.

Manual lookup:

```bash
curl 'http://localhost:8000/experiments/EXPERIMENT_ID/similar?limit=5' \
  | python3 -m json.tool
```

## 10. Scenario: calibration-aware waiting and recovery

First inspect the latest persisted probe:

```bash
curl http://localhost:8000/backends/aer-simulator/calibration \
  | python3 -m json.tool
```

Then run the end-to-end validator:

```bash
python3 scripts/validate_calibration_gate.py
```

It deliberately makes the snapshot stale, submits a minimal H2 VQE, observes
`waiting_for_calibration`, and verifies that an immediate probe refreshes the
snapshot and allows VQE to resume. The worker is not sleeping: the task waits
in `experiments.waiting-for-calibration`, a RabbitMQ TTL/dead-letter queue.

## 11. Scenario: deterministic calibration rejection

Stop the running `dev.sh` with `Ctrl+C`, then restart it with:

```bash
CALIBRATION_DEMO_ERROR_RATE=0.20 ./dev.sh
```

Wait for a calibration cycle and submit a VQE. Expected result:

```text
failed: backend calibration rejected execution: bell parity error_rate=0.2002
```

The injected value is a deterministic demo hook, not a physical Aer noise
model. Restore normal operation by stopping the process and running:

```bash
unset CALIBRATION_DEMO_ERROR_RATE
./dev.sh
```

## 12. Inspecting each transport and store

### RabbitMQ messages and queues

Use <http://localhost:15672> and inspect:

- `experiments`;
- `experiment-results`;
- `experiments.waiting-for-calibration`;
- `experiments.dlq` when testing failures.

### Kafka topics

Use <http://localhost:8090> or:

```bash
docker compose exec -T kafka kafka-topics \
  --bootstrap-server kafka:29092 --list
```

Important topics include:

- `calibration-results`;
- `calibration-alerts`;
- `calibration-drift-alerts`;
- `vqe-iteration-metrics`;
- `vqe-window-metrics`;
- `experiment-completed`.

### PostgreSQL

```bash
docker compose exec -T postgres psql -U quantum -d quantum_platform -c \
  "SELECT id, algorithm, status, parameters FROM experiments
   ORDER BY submitted_at DESC LIMIT 10;"
```

### TimescaleDB

```bash
docker compose exec -T timescaledb psql -U quantum -d telemetry -c \
  "SELECT * FROM calibration_events ORDER BY time DESC LIMIT 10;"
```

## 13. Troubleshooting

### HTTP 422 when submitting VQE

Confirm that the request uses:

```json
{"algorithm":"vqe","molecule":"lih","shots":256,"max_iterations":3}
```

Restart the API after schema changes.

### Experiments remain queued

Check:

```bash
tail -n 100 .dev-logs/orchestrator.log
docker compose exec -T rabbitmq rabbitmqctl list_queues name messages consumers
```

Long VQE jobs block later tasks because the demonstration worker has
`prefetch_count=1` and processes one task at a time.

### No VQE window metrics

Ensure the Faust worker was started separately before the VQE run. Check both
`vqe-iteration-metrics` and `vqe-window-metrics` in Kafka UI.

### Similarity endpoint returns 404

The experiment has not been indexed yet. Check:

```bash
tail -n 100 .dev-logs/result-indexer.log
```

Only completion events produced after the vector pipeline was installed are
automatically indexed unless Kafka offsets are replayed.

### Migration or missing-table failure

Run:

```bash
export DATABASE_URL='postgresql+asyncpg://quantum:quantum@localhost:5432/quantum_platform'
cd services/api
.venv/bin/python3 -m alembic upgrade head
```

TimescaleDB `init/*.sql` files run only when its Docker volume is first
created. For an existing volume, apply a new SQL file manually or deliberately
recreate the project volumes.

### Port collision

Use `docker compose ps` and your operating system's port inspection tools.
Do not use `./dev.sh --clean` merely to fix a port owned by another project.

## 14. Shutdown and reset

`Ctrl+C` stops the host Python services started by `dev.sh` but leaves Docker
infrastructure and data running.

Stop containers while preserving volumes:

```bash
docker compose down
```

Destructively reset all project Docker volumes only when intentional:

```bash
./dev.sh --clean
```
