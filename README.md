# quantum-platform

A pet project: a platform for running quantum algorithms (Grover, SAT-Grover,
QFT/QPE, VQE) wrapped in a full production-style architecture — API, task
queue, orchestration, persistence, streaming telemetry, dashboards. The
original goal was to get hands-on with architecture/platform design for
quantum computing infrastructure, not just "run a couple of algorithms."

## Quick start

Requires Docker, Python 3.11+.

```bash
./dev.sh                    # brings up everything: infra + api + orchestrator + stream-analytics
./dev.sh --profile=verify   # same, but runs each service's tests first
./dev.sh --help             # flag reference
```

The first run will create a `.venv` in each service and install
dependencies automatically. Logs go to `.dev-logs/` (gitignored) and are
streamed to the terminal via `tail -f`. `Ctrl+C` stops `api`/`orchestrator`/
`stream-analytics`; the Docker containers keep running — use
`docker compose down` separately if you want to tear everything down.

## Endpoints

| Service | URL | Purpose |
|---|---|---|
| API — Swagger | http://localhost:8000/docs | interactive REST API docs |
| **Experiments dashboard** | http://localhost:8000/dashboard/ | live table, filters, drill-down into results |
| RabbitMQ | http://localhost:15672 (guest/guest) | task queue management UI |
| **Grafana** | http://localhost:3001 (admin/admin) | metrics (Prometheus) + direct SQL queries against the DBs |
| Prometheus | http://localhost:9090 | raw metrics/targets |
| **Kafka UI (Kafbat)** | http://localhost:8090 | browse topics/messages/consumer groups |
| **Adminer** | http://localhost:8091 | ad-hoc SQL browser |
| Postgres | `localhost:5432` (quantum/quantum, db=`quantum_platform`) | experiment metadata |
| TimescaleDB | `localhost:5433` (quantum/quantum, db=`telemetry`) | calibration history |
| Kafka | `localhost:9092` | broker |

## Structure

```
quantum-platform/
├── dev.sh                     # spins up the whole stack, quick/verify profiles
├── docker-compose.yml         # RabbitMQ, Postgres, Kafka, TimescaleDB + debug/ops stack
├── infra/                     # Prometheus/Grafana/RabbitMQ-plugin configs
├── scripts/
│   └── observe.py             # load generator + live observation of the stack
├── services/
│   ├── quantum-core/          # library: algorithms, hw/sw abstraction, execution
│   ├── api/                   # FastAPI: request intake, dashboard, Postgres store
│   ├── orchestrator/          # RabbitMQ worker: execution + retry + calibration
│   └── stream-analytics/      # Kafka consumers (hand-rolled + Faust) + TimescaleDB sink
└── docs/
    ├── algorithms/            # algorithm physics/math, independent verification
    └── architecture/          # architecture decisions, ADR-style
```

Every service has its own `README.md` with implementation details, degree
of verification, and instructions for running it standalone.

## Documentation

### Algorithms (`docs/algorithms/`)
- [`grover.md`](docs/algorithms/grover.md) — Grover: hello-world version,
  real SAT search via `PhaseOracleGate`, limitations (QRAM,
  BBHT adaptive search), how it differs from Shor's algorithm
- [`qft_qpe.md`](docs/algorithms/qft_qpe.md) — QFT/QPE, independent
  verification against numpy (found and fixed a real bug in the QFT
  convention before it ever reached Qiskit)
- [`vqe.md`](docs/algorithms/vqe.md) — VQE on the H₂ molecule,
  hardware-efficient ansatz, full verification of the measurement-based
  pipeline

### Architecture (`docs/architecture/`)
- [`orchestration.md`](docs/architecture/orchestration.md) — moving from
  synchronous execution to RabbitMQ, retry/dead-letter policy
- [`postgres.md`](docs/architecture/postgres.md) — experiment persistence,
  storage abstraction, Alembic
- [`kafka.md`](docs/architecture/kafka.md) — calibration telemetry,
  hand-rolled consumer vs. Faust, TimescaleDB sink
- [`dashboard.md`](docs/architecture/dashboard.md) — the experiments
  dashboard, and why it isn't Grafana for this part
- [`observability.md`](docs/architecture/observability.md) — debug/ops
  stack: Grafana, Prometheus, Kafka UI, Adminer
- [`deferred-work.md`](docs/architecture/deferred-work.md) — deferred
  pieces from the original sketch (fast-control, advanced Faust
  topologies)

### Other
- [`testing.md`](docs/testing.md) — the testing approach, including how
  tests were verified without access to pytest in the working
  environment

## Tests

```bash
./dev.sh --profile=verify        # all services at once, before the stack starts
# or individually:
cd services/quantum-core && pytest tests/ -v
cd services/api && pytest tests/ -v
cd services/stream-analytics && pytest tests/ -v
```

## Architecture at a glance

```
                     ┌─────────────┐
  POST /experiments  │     api     │  GET /experiments (filters/sorting/stats)
  ──────────────────▶│  (FastAPI)  │◀────────────────── dashboard (static/dashboard/)
                      └──────┬──────┘
                             │ publish task           ▲ apply result
                             ▼                         │
                      ┌─────────────┐           ┌──────┴──────┐
                      │  RabbitMQ   │──────────▶│ orchestrator │
                      │ (queue)     │  consume  │  (worker)    │
                      └─────────────┘           └──────┬──────┘
                                                         │ execute via quantum_core
                                                         ▼
                                                  ┌──────────────┐
                                                  │ QuantumBackend│ (Aer simulator)
                                                  └──────┬───────┘
                                                         │ calibration cycle
                                                         ▼
                                                  ┌─────────────┐
                                                  │    Kafka     │  calibration-results
                                                  └──────┬──────┘
                                       ┌─────────────────┴────────────────┐
                                       ▼                                  ▼
                              ┌────────────────┐                ┌─────────────────┐
                              │ stream-analytics│                │  stream-analytics│
                              │ (hand-rolled)   │                │     (Faust)      │
                              └────────┬────────┘                └─────────────────┘
                                       ▼
                                ┌─────────────┐
                                │ TimescaleDB │ ──▶ Grafana
                                └─────────────┘

Postgres (experiments metadata) ──▶ api (store) + Grafana + Adminer
```

## An honest note on the degree of verification

This project was built in collaboration with Claude, mostly in an
environment without access to Docker/network — a lot of it (the Qiskit
math, pure Python logic) was independently verified before the code ever
landed in the repo; a lot of the rest (RabbitMQ, Kafka, Postgres, Grafana)
was not, and has since been verified here by hand, turning up and fixing
several real bugs along the way (a timezone mismatch in a SQLAlchemy
model, the wrong way to enable a RabbitMQ plugin, a stale Kafka UI image,
and others — details in the corresponding `docs/architecture/*.md` files).
This is a deliberate and honestly documented process, not a sign of low
quality — see the "⚠️ Degree of verification" notes in each architecture
document.
