# quantum-core

A library (not a service) providing an abstraction over quantum
backends, a synchronization mechanism for the hardware/software
interaction loop, and (as they get implemented) the quantum algorithms
themselves. Used by the `api` and `orchestrator` services, and can also
be run standalone for experiments/demos.

## Structure and file purposes

```
quantum-core/
├── pyproject.toml
├── requirements.txt
├── demo_polling.py
├── demo_aer.py
└── quantum_core/
    ├── backends/
    │   ├── base.py              # abstract QuantumBackend contract
    │   ├── mock_hw_backend.py   # fake "hardware" for development without a QPU
    │   └── aer_backend.py       # a real simulator (Qiskit Aer)
    ├── sync/
    │   └── polling.py           # hw/sw loop synchronization (retry/backoff/circuit breaker)
    ├── algorithms/
    │   ├── grover.py            # "hello world": search where the answer is already known
    │   ├── sat_search.py        # real search: a SAT criterion via PhaseOracleGate
    │   ├── qft.py                # QFT / inverse-QFT (a reusable primitive)
    │   ├── qpe.py                # Quantum Phase Estimation
    │   └── vqe.py                 # VQE: H2 Hamiltonian, ansatz, per-term measurements
    ├── loops/
    │   └── vqe_loop.py             # the closed classical-quantum feedback loop
    ├── execution.py                 # shared "run this algorithm" logic -- used by api and orchestrator
    └── tasks.py                     # queue messages (ExperimentTask/ExperimentResultMessage)

tests/unit/
├── conftest.py                     # fake_clock fixture (no real waiting)
├── fakes.py                        # ScriptedBackend -- test double for QuantumBackend
├── test_circuit_breaker.py
└── test_wait_for_result.py
```

### `quantum_core/backends/base.py`
The hardware/software boundary. Defines:
- `Circuit`, `JobHandle`, `ExperimentResult`, `JobStatus` — shared data
  types;
- `QuantumBackend` — an abstract class with `submit`, `poll_status`,
  `fetch_result`, `cancel` methods. Any backend (simulator, mock, real
  QPU) must implement this interface;
- `TransientBackendError` — a distinct error type for retryable
  failures (as opposed to a hard hardware failure).

Doesn't run anything itself — it's just the contract everything else
relies on.

### `quantum_core/backends/mock_hw_backend.py`
A `QuantumBackend` implementation simulating real hardware:
- random queue delay and execution time;
- random transient failures (e.g. "calibration in progress") and hard
  failures (e.g. "qubit readout error");
- parameters (`transient_failure_rate`, `hard_failure_rate`,
  `min_queue_s`/`max_queue_s`, etc.) let you tune how "temperamental"
  the backend should be — handy for testing retry logic.

Needed so orchestration can be developed and checked without access to
real quantum hardware.

### `quantum_core/sync/polling.py`
The mechanism for waiting on a result from a backend:
- `wait_for_result()` — the main function: polls `poll_status` with
  adaptive backoff (an exponentially growing interval), handles
  transient errors with a bounded number of retries, supports a
  timeout;
- `CircuitBreaker` — stops polling the backend after a run of
  consecutive failures, so it doesn't keep hammering clearly unhealthy
  hardware; tries again after a set time (half-open);
- `CancellationToken` — cooperative cancellation: the caller can
  interrupt waiting, and the current job gets cancelled on the backend.

This is the actual "synchronization mechanism for hardware/software
interaction loops" from the original job requirements — the first place
worth looking during code review.

### `quantum_core/backends/aer_backend.py`
A real backend on **Qiskit Aer** (`AerSimulator`) — the first backend in
the project that genuinely executes a quantum circuit rather than
faking a result.

Important implementation details:
- `circuit.payload` for this backend must be a `qiskit.QuantumCircuit`
  object with measurements already added (`.measure_all()`). The
  `Circuit` abstraction in `base.py` doesn't force a specific SDK, so
  responsibility for what goes into `payload` sits with the specific
  backend implementation;
- `AerSimulator.run()` is a synchronous, blocking call. To avoid
  blocking the event loop (which the rest of the async orchestration
  depends on), the actual simulation run happens in a separate thread
  via `loop.run_in_executor()`. The job goes through the same
  `QUEUED → RUNNING → COMPLETED/FAILED` statuses as
  `MockHardwareBackend` — from the outside (for `polling.py`), both
  backends look identical;
- `cancel()` is best-effort: a real simulation already running in a
  thread can't be cancelled (a limitation of Aer itself), so
  cancellation only works for jobs still in `QUEUED`.

⚠️ **Not verified by actually running it.** The code was written
against the current Qiskit Aer 0.17.x API (checked via the docs), but my
working environment has no network/pip access, so I couldn't physically
run `AerSimulator` and confirm there are no typos or version mismatches.
Unlike the mock-backend files (which I did run and show real output
for), here **you need to run `demo_aer.py` first** and let me know if
anything doesn't line up — for example, if your `qiskit`/`qiskit-aer`
version is newer and some method has been renamed.

### `quantum_core/algorithms/grover.py`
The first real quantum algorithm in the project. Implements searching
for marked entries in an unindexed space of N = 2ⁿ elements —
classically O(N) checks, Grover — O(√N).

- `GroverProblem` — the problem description (number of qubits + list of
  marked bit strings);
- `optimal_iterations()` — computes the optimal iteration count
  (`⌊(π/4)·√(N/M)⌋`); important not to hardcode an arbitrary number —
  past the optimum, success probability drops again;
- `build_grover_circuit()` — builds a `qiskit.QuantumCircuit` that can
  be passed as `Circuit.payload` to `AerBackend`.

The oracle and diffuser are built via multi-controlled Z (`H`–`mcx`–`H`),
rather than via Qiskit's ready-made helpers (`PhaseOracle`) — this
avoids an optional dependency on `tweedledum`.

⚠️ **Degree of verification**: the algorithm's math itself (oracle +
diffuser as matrices) was verified independently via numpy without
Qiskit — for 3 qubits and one marked state, got a 94.5% success
probability versus 12.5% for random guessing, confirming the logic is
correct. However, the specific translation into Qiskit calls (`qc.mcx`,
`qc.h(range(n))`, etc.) hasn't been run — as with `aer_backend.py`, I
don't have network access to install `qiskit`. Run `demo_grover.py`
first and send the result.

### `demo_grover.py`
Builds a search problem over 3 qubits (8 entries), searches for the
marked entry `101`, runs it through `AerBackend` and the same
`wait_for_result()`. Expected result: a histogram where `101` shows up
noticeably more often than the rest (around 900+ out of 1024 shots at 2
iterations, consistent with the numpy check above — exact numbers will
differ slightly due to seed/transpilation noise).

### `quantum_core/algorithms/sat_search.py`
A more realistic version of Grover: the search criterion is a boolean
expression (a SAT clause), not a pre-known answer. The oracle is built
via `qiskit.circuit.library.PhaseOracleGate` (the modern replacement for
the old `PhaseOracle`/`classical_function`, which depended on the
external `tweedledum` library, removed in Qiskit 2.0).

- `BooleanSearchProblem(variables, expression)` — the problem defined by
  a criterion;
- `eval_boolean_expression()` — a classical evaluator for the same
  syntax (`&`/`|`/`~`/`^`) that `PhaseOracleGate` understands. Used both
  for brute-force counting the number of solutions (demo-only — in a
  real problem this would defeat the point of using Grover) and for
  checking the quantum result;
- `build_sat_grover_circuit(problem, iterations)` — assembles the
  circuit.

⚠️ **Degree of verification**: `eval_boolean_expression`/
`count_solutions` were verified against an independent brute-force
implementation (see the code — 7 solutions out of 16 combinations, match
confirmed). Integration with `PhaseOracleGate` and qubit/variable
ordering **haven't** been run against real Qiskit. Run
`demo_sat_grover.py` first.

Details on this approach's limitations (unknown number of solutions,
QRAM, where Grover doesn't fit) are in `docs/algorithms/grover.md`.

### `demo_sat_grover.py`
Solves a small SAT clause over 4 variables:
`(x0 | x1) & (~x1 | x2) & (x0 | ~x3)`. The answer isn't hardcoded
anywhere — only the condition itself. Brute-force solution counting is
used solely to pick the iteration count (rationale in the
`count_solutions` docstring). At the end, the quantum result is checked
against the classical evaluator.

### `quantum_core/algorithms/qft.py` and `qpe.py`
Quantum Fourier Transform and Quantum Phase Estimation — estimating a
unitary operator's eigenphase. Unlike Grover (amplitude amplification),
this is a spectral method: it reads out phase via constructive
interference.

`qft.py` is a reusable primitive (`build_qft_circuit`), independent of
QPE. `qpe.py` builds the full QPE circuit (`build_qpe_circuit`) on top
of it, with controlled-U^(2^j) via exact matrix exponentiation.

⚠️ **Degree of verification**: the math itself (QFT/inverse-QFT against
direct DFT, and the full QPE pipeline for exactly/inexactly
representable phase) was verified independently via numpy — **and
didn't match on the first try** (65% error); the bug was found by
enumerating conventions specifically because the math was double-checked
before porting to Qiskit, not after. The actual Qiskit API calls
(`qc.cp`, `Operator.power`, `UnitaryGate(...).control(1)`, `qc.compose`)
haven't been run — no network access. Details in
`docs/algorithms/qft_qpe.md`. Run `demo_qpe.py` first.

### `demo_qpe.py`
Recovers the known phase `φ=5/8` via a `PhaseGate` and its `|1⟩`
eigenstate. Expected result: `101` with probability close to 100%. The
`--inexact` flag shows a case where the phase isn't exactly
representable in 3 bits — the result spreads across neighboring values
(this is expected QPE behavior, not a bug).

### `quantum_core/algorithms/vqe.py` and `quantum_core/loops/vqe_loop.py`
VQE for the H₂ ground-state energy — a NISQ-friendly alternative to QPE,
and **the project's main demonstration of the hw/sw interaction loop**:
unlike Grover/QPE (one circuit, one measurement), VQE is literally a
"classical optimizer ↔ quantum hardware" loop, repeating dozens of
times.

`vqe.py` — the H₂ Hamiltonian (coefficients from O'Malley et al., Phys.
Rev. X 6, 031007), a 4-parameter hardware-efficient ansatz, building
measurement circuits per Pauli term, recovering `⟨P⟩` from counts.

`vqe_loop.py` — `evaluate_energy()` (one full classical-quantum round
trip across all Hamiltonian terms via `wait_for_result`) and
`run_vqe()` (the loop using `scipy.optimize.minimize`, COBYLA method).

⚠️ **The sync/async bridge**: `scipy.optimize` has a synchronous API,
while all of `QuantumBackend` is asynchronous. `run_vqe()` is the only
**synchronous** function among all of the project's entry points,
bridging via `asyncio.run()` on every iteration. This was specifically
checked against this project's real `MockHardwareBackend` before use
(not a stub) — without that check, it's easy to hit a `RuntimeError`
about an already-running event loop. Because of this, `demo_vqe.py` is
the one demo where `main()` is **not** wrapped in `asyncio.run()`.
Details in `docs/algorithms/vqe.md`.

⚠️ **Degree of verification**: the math (Hamiltonian, ansatz, X/Y/Z
basis rotations, the sign formula, the full measurement-based pipeline,
convergence with 8192 shots — 0.0015 Hartree off the exact value, below
"chemical accuracy") was verified independently via numpy/scipy — in
more depth and more rigorously than for previous algorithms, since there
are more moving parts here. The specific Qiskit calls haven't been run.
Run `demo_vqe.py` first.

### `demo_vqe.py`
Finds the H₂ ground-state energy via the full feedback loop on
`AerBackend`. Expected result: a final energy around `-1.14` Hartree
(close to the literature value of `-1.137`), with a convergence log
across iterations. Slower than the previous demos — up to 5 circuit
submissions per iteration, dozens of COBYLA iterations.

### `quantum_core/execution.py`
The shared "run this algorithm" business logic — one function per
algorithm (`run_grover`, `run_sat_grover`, `run_qpe`, `run_vqe_sync`),
taking plain Python types (lists, strings, numbers), not Pydantic or
other framework-specific objects. This came about when moving to
RabbitMQ-based orchestration (see
`docs/architecture/orchestration.md`) — this logic used to live
directly in `services/api/app/execution.py` and was tied to the API's
Pydantic schemas; now both `api` and `orchestrator` use the same code,
each with its own thin adapter on top (an HTTP request → plain types for
`api`; a JSON queue message → plain types for `orchestrator`).

### `quantum_core/tasks.py`
The queue message format — `ExperimentTask` (published by `api`, read
by `orchestrator`) and `ExperimentResultMessage` (the reverse). Plain
`dataclass`es with `to_json()`/`from_json()`, no Pydantic — so as not to
drag an HTTP framework into `quantum_core`. The one file in this project
that was actually run and verified (serialization round-trip, including
the failure case) specifically in the context of orchestration — pure
stdlib, no external dependencies.

### `demo_aer.py`
Runs a Bell state (`H` + `CX` on 2 qubits) through `AerBackend` and
`wait_for_result()` — the same synchronization mechanism as in
`demo_polling.py`, but now on an actual quantum circuit. Expected
result: a histogram roughly evenly split between `00` and `11` (and
almost no `01`/`10` — that's the essence of entanglement in a Bell
pair).

### `demo_polling.py`
An executable script that runs several experiments in parallel through
`MockHardwareBackend` and `wait_for_result()`, to see live:
- successful completions with a measurement histogram;
- retry with exponential backoff on transient failures (visible as
  warning logs);
- cases where retries are exhausted and the job is marked failed.

Not part of the library — a purely demonstrative / manual-run script.

### `pyproject.toml`
The package description. Contains `[build-system]` (needed for
`pip install -e .`) and `[tool.setuptools] packages = ["quantum_core"]`
— an explicit list instead of auto-discovery (`packages.find`), because
auto-discovery on some `setuptools` versions produced an empty `MAPPING`
in the editable-finder and import silently broke — a detailed writeup of
this bug came up while debugging the package install.

`[project.dependencies]` declares `qiskit`/`qiskit-aer`/`scipy` — this
is the single source of truth for runtime dependencies. This matters not
just for `quantum-core` itself but for the dependent services too: when
`services/api/requirements.txt` does `-e ../quantum-core`, pip
transitively installs those three packages too — without declaring them
here, this doesn't happen (that's exactly what was originally the case,
and `api` used to crash with `ModuleNotFoundError: No module named
'qiskit'`, until this was fixed).

### `requirements.txt`
Now contains only dev/test tools (`pytest`, `pytest-asyncio`) — runtime
dependencies moved into `pyproject.toml` (see above). Working with
`quantum-core` as a standalone project requires both steps:
`pip install -e .` (the package itself + runtime dependencies from
`pyproject.toml`) and `pip install -r requirements.txt` (dev tools).

## How to run it

Requires Python 3.11+.

**With no external dependencies** (mock backend, synchronization):

```bash
cd services/quantum-core
python3 -m venv .venv
source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -e .
python3 demo_polling.py
```

`pip install -e .` installs the `quantum_core` package in editable mode
— without this step, `python3 demo_polling.py` fails with
`ModuleNotFoundError: No module named 'quantum_core'`, since the package
itself can't be found via the regular sys.path. Editable mode means
code changes are picked up immediately, no reinstall needed.

No external services (DB, brokers, Docker) are needed — everything runs
locally in a single process's memory.

Expected result: stdout shows 8 experiments, some completing
successfully with a histogram (`COMPLETED counts=...`), some "GAVE UP
after retries" (if transient failures repeated more than
`max_retries_on_transient_error` times in a row). stderr shows
`WARNING` logs for each retry attempt with the attempt number. This is
normal, expected behavior, not a bug — it demonstrates exactly the
degradation the retry/backoff logic was written to handle.

**With Qiskit Aer** (real simulation) — using the same `.venv`:

```bash
cd services/quantum-core
source .venv/bin/activate    # if not already active
pip install -r requirements.txt
python3 demo_aer.py
```

Expected result: a line like
`status=JobStatus.COMPLETED counts={'00': ~512, '11': ~512} metadata=...`
(the exact numbers will vary shot-to-shot, but `01`/`10` should be close
to zero — this is a Bell pair). **This is the first run I haven't
verified myself** — if anything doesn't match with your `qiskit-aer`
version, send the error text over and we'll fix it.

**Grover** (after installing qiskit from the step above):

```bash
python3 demo_grover.py          # "hello world" — looks for '101' by default
python3 demo_grover.py 011      # or any other value
python3 demo_grover.py 011 110  # can search for several marked entries at once

python3 demo_sat_grover.py      # a real search: SAT criterion, answer not hardcoded
```

**QFT/QPE**:

```bash
python3 demo_qpe.py             # φ=5/8, exactly representable -> should give '101' almost always
python3 demo_qpe.py --inexact   # φ=0.3, not exactly representable -> a spread-out peak, that's normal
```

**VQE** (takes noticeably longer — dozens of iterations, up to 5
circuits each):

```bash
python3 demo_vqe.py
```

## Unit tests

`tests/unit/` covers `polling.py` (13 tests: `CircuitBreaker` +
`wait_for_result` — backoff, retry, timeout, cancellation, hard/transient
failure). Details on the verification approach are in `docs/testing.md`.

```bash
pytest tests/unit/ -v
```

⚠️ The tests' logic has been verified by hand (see `docs/testing.md`),
but pytest itself (fixture discovery, `pytest-asyncio`) hasn't — no
network access in my environment. Run it first.

## Not yet implemented

- Unit tests for the remaining modules (`base.py`, `mock_hw_backend.py`,
  the algorithms) — only `polling.py`, the most critical part, is
  currently covered.
