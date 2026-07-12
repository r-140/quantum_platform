# quantum-sim

A quantum computer simulator with a realistic software stack — modelling the
hardware/software interaction loops found in real QPU platforms like Alice & Bob.

## Architecture

```
┌─────────────────────────────────────────────────────┐
│                  REST API (FastAPI)                  │
└────────────────────────┬────────────────────────────┘
                         │
┌────────────────────────▼────────────────────────────┐
│            Experiment Runner (async)                 │
│         Celery task queue  ·  Redis broker           │
└───┬────────────────────────────────────────┬─────────┘
    │                                        │
┌───▼──────────────────┐    ┌────────────────▼────────┐
│   Quantum Simulator  │    │    Storage Layer        │
│  ┌────────────────┐  │    │  ┌──────────────────┐   │
│  │  QuantumState  │  │    │  │ PostgreSQL        │   │
│  │  Gates         │  │    │  │ (metadata)        │   │
│  │  Noise Model   │  │    │  ├──────────────────┤   │
│  │  Measurement   │  │    │  │ InfluxDB          │   │
│  └────────────────┘  │    │  │ (timeseries)      │   │
│  ┌────────────────┐  │    │  ├──────────────────┤   │
│  │  Algorithms    │  │    │  │ MongoDB           │   │
│  │  · Grover      │  │    │  │ (tomography)      │   │
│  │  · QFT         │  │    │  ├──────────────────┤   │
│  └────────────────┘  │    │  │ MinIO             │   │
└──────────────────────┘    │  │ (state vectors)   │   │
                            │  └──────────────────┘   │
                            └─────────────────────────┘
```

## Quickstart

```bash
# 1. Bootstrap venv
make bootstrap
source .venv/bin/activate

# 2. Start infrastructure
make up

# 3. Run tests
make test

# 4. Start API
uvicorn api.main:app --reload
```

## Infrastructure

| Service    | Port  | Purpose                        |
|------------|-------|--------------------------------|
| PostgreSQL | 5432  | Experiment metadata, circuits  |
| MongoDB    | 27017 | Tomography results, configs    |
| InfluxDB   | 8086  | Raw measurement timeseries     |
| MinIO      | 9000  | State vectors, large matrices  |
| Redis      | 6379  | Celery broker                  |
| Grafana    | 3000  | Dashboards (admin/qsim_secret) |

## Project layout

```
quantum-sim/
├── core/               # Simulator kernel (state, gates, noise, measurement)
├── algorithms/         # Grover, QFT, Shor (partial)
├── platform/           # Experiment runner, compiler, scheduler
├── storage/            # Adapters for each storage backend
├── api/                # FastAPI app
├── tests/
│   ├── test_core/
│   └── test_platform/
├── infra/              # Grafana provisioning, K8s manifests (later)
└── scripts/            # bootstrap.sh and other dev helpers
```
