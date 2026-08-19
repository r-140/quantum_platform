# Hardware/software interaction loop

## Purpose

Quantum hardware is asynchronous. Submitting a circuit returns a job handle,
not a measurement result. The platform must then poll the backend, tolerate
transient failures, enforce a timeout, and retrieve the result when the job
reaches a terminal state.

This boundary is represented by `QuantumBackend` in
`quantum_core/backends/base.py`. Algorithms depend on that abstraction rather
than on Qiskit Aer or a vendor-specific SDK.

## Job lifecycle

```text
algorithm
   |
   | submit(Circuit)
   v
QuantumBackend ----------> JobHandle
   ^                           |
   | poll_status(handle)       | queued/running
   |<--------------------------+
   |
   | fetch_result(handle) when completed
   v
ExperimentResult
```

`wait_for_result()` in `quantum_core/sync/polling.py` owns this lifecycle. It
polls until the job completes, fails, is cancelled, or exceeds its timeout.
Callers do not duplicate backend-status loops.

## Resilience semantics

Three mechanisms solve different problems:

- **polling interval** prevents a tight loop against the backend;
- **retry with exponential backoff** handles transient errors during polling
  or result retrieval;
- **circuit breaker** stops repeated calls when the backend is already known
  to be unhealthy.

These backend-level retries are not RabbitMQ task redelivery. A transient QPU
API error occurs inside one experiment; RabbitMQ redelivery handles a worker
crash before the task was acknowledged. Conflating the two would either retry
entire experiments unnecessarily or fail to retry the actual backend call.

`PollingMetrics` is an optional out-parameter. It records total wait time,
transient retries, and whether the circuit breaker was already open without
changing `wait_for_result()`'s result type.

## Why VQE is the main interaction-loop workload

Grover and QPE build a circuit, submit it, and retrieve one result. VQE repeats
the full hardware/software round trip for every optimizer evaluation and every
non-identity Hamiltonian term:

```text
COBYLA proposes parameters
          |
          v
build ansatz for selected molecule
          |
          v
for each Pauli term: rotate basis -> submit -> poll -> measure
          |
          v
sum expectation values -> energy
          |
          +--------------------> COBYLA proposes new parameters
```

For H₂ there are five measured terms per energy evaluation. LiH and BeH₂ will
have substantially more terms after active-space reduction, so the number of
backend round trips—not only the qubit count—is an important scaling axis.

## Sync/async boundary

The backend contract is asynchronous, but SciPy's `minimize()` callback is
synchronous. `run_vqe()` therefore invokes `asyncio.run(evaluate_energy(...))`
for each optimizer evaluation. The orchestrator does not run this synchronous
loop on its event loop; it offloads `run_vqe_sync()` with
`run_in_executor()`.

This is acceptable for the current single-worker demonstration, but it creates
a new event loop per optimizer evaluation. A long-running or highly concurrent
production implementation should keep a persistent loop and bridge to it with
`asyncio.run_coroutine_threadsafe()`, or expose an optimizer integration that
is async-native.

## Molecule and ansatz boundaries

Molecule data and circuit behavior are separate concepts:

- `MolecularHamiltonian` contains geometry, mapping, qubit count, Pauli terms,
  nuclear repulsion, provenance, and reference energy;
- `Ansatz` is a circuit-building strategy;
- `HardwareEfficientAnsatz` derives its parameter count from the molecule's
  qubit count;
- the molecule registry resolves an API name such as `h2` to immutable,
  validated Hamiltonian data.

This avoids an inheritance hierarchy whose subclasses would contain only
different constants, and it permits future ansätze such as UCCSD without
changing molecule definitions.

## Telemetry

Each VQE iteration records energy and optimizer parameters, total backend wait
time (`quantum_time_s`), remaining wall time (`classical_time_s`), retry count,
and circuit-breaker trips.

The orchestrator publishes these records to `vqe-iteration-metrics` after the
VQE run returns. Faust builds 60-second tumbling-window aggregates, which are
published to `vqe-window-metrics`, persisted in TimescaleDB, and visualized in
Grafana. See `vqe-metrics.md` for the full pipeline and the limitation that
metrics are currently published after, rather than during, optimization.

## Failure and cancellation

- A terminal backend failure becomes an experiment failure.
- A timeout attempts backend cancellation before returning an error.
- Metrics use `try/finally`, so elapsed wait time is retained on unsuccessful
  exits.
- An open circuit breaker prevents an expensive circuit from being submitted
  to a backend already considered unavailable.

Future real-QPU integrations should distinguish queue time, physical execution
time, and result-retrieval time. The current `quantum_time_s` is the whole
backend-facing wait and must not be presented as pure QPU execution time.
