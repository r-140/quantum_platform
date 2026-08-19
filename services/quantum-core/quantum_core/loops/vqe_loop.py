"""
The classical-quantum feedback loop: a classical optimizer proposes ansatz
parameters, a QuantumBackend evaluates the resulting energy (one circuit
submission + full hw/sw synchronization per non-identity Hamiltonian term),
and the loop repeats until the optimizer converges. This is the closed-loop
hardware/software interaction pattern flagged as a goal early in this
project -- unlike Grover/QPE (build one circuit, read out once), VQE's
entire premise is this repeated round-trip between classical and quantum
hardware, so it's the natural place to exercise `wait_for_result` under
realistic *repeated* load (dozens of small submissions per optimization
run, not one).

sync/async bridge: `scipy.optimize.minimize`'s cost callback must be a
plain synchronous function, but `evaluate_energy` needs `await` (it drives
`QuantumBackend.submit`/`wait_for_result`). `run_vqe` bridges this by
calling `asyncio.run(evaluate_energy(...))` once per COBYLA iteration --
verified against this project's actual MockHardwareBackend/wait_for_result
code (not a stand-in) to confirm there's no nested-event-loop error before
relying on it. This means `run_vqe` itself must be called as a plain
*synchronous* function from a non-async context (see `demo_vqe.py`) --
calling it from inside an already-running event loop (e.g. from within
`asyncio.run(main())`) would raise "asyncio.run() cannot be called from a
running event loop". A production orchestrator would instead bridge this
with a persistent loop and `run_coroutine_threadsafe`; the simpler
per-iteration `asyncio.run()` is a deliberate scope trade-off for this demo,
not an oversight -- noted here so it isn't copy-pasted into a concurrent
service without revisiting it.

The optimizer (COBYLA) is gradient-free by design, not just a default
choice: computing gradients on real hardware (parameter-shift rule) would
roughly double the number of quantum circuit evaluations per iteration,
which matters when each evaluation is a real hw/sw round trip.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

from scipy.optimize import minimize

from quantum_core.algorithms.vqe import (
    DEFAULT_ANSATZ,
    Ansatz,
    build_measurement_circuit,
    pauli_expectation_from_counts,
)
from quantum_core.backends.base import Circuit, QuantumBackend
from quantum_core.chemistry.molecules import H2, MolecularHamiltonian
from quantum_core.sync.polling import PollingConfig, PollingMetrics, wait_for_result


@dataclass
class VQEIterationMetrics:
    """Aggregated hw/sw-loop instrumentation for one `evaluate_energy()`
    call -- i.e. one COBYLA iteration's worth of circuit submissions.
    Populated by summing a fresh `PollingMetrics` per Hamiltonian term
    (see `evaluate_energy` below). Deliberately just a plain dataclass,
    not published anywhere by quantum_core itself -- this library stays
    framework/broker-agnostic (same principle as `execution.py`/
    `tasks.py`); orchestrator reads the equivalent fields off
    `VQEIterationLog` after the fact and does the actual Kafka
    publishing (see services/orchestrator/app/tasks/vqe_metrics.py).
    """

    quantum_time_s: float = 0.0
    retry_count: int = 0
    circuit_breaker_trips: int = 0


@dataclass
class VQEIterationLog:
    iteration: int
    params: list[float]
    energy: float
    quantum_time_s: float = 0.0
    classical_time_s: float = 0.0
    retry_count: int = 0
    circuit_breaker_trips: int = 0


@dataclass
class VQEResult:
    optimal_params: list[float]
    electronic_energy: float
    total_energy: float
    history: list[VQEIterationLog] = field(default_factory=list)


async def evaluate_energy(
    backend: QuantumBackend,
    params: list[float],
    *,
    shots: int = 8192,
    polling_config: PollingConfig | None = None,
    metrics: VQEIterationMetrics | None = None,
    molecule: MolecularHamiltonian = H2,
    ansatz: Ansatz = DEFAULT_ANSATZ,
) -> float:
    """One classical-quantum round trip: submits one circuit per
    non-identity Hamiltonian term, waits for each via the standard hw/sw
    synchronization primitive (`wait_for_result` -- same retry/backoff/
    circuit-breaker machinery used everywhere else in this project), and
    combines the measured expectations into the total electronic energy for
    these `params`.

    Terms are submitted and awaited sequentially, not concurrently -- on
    real hardware, circuits typically share a single queue/calibration
    cycle, so concurrent submission wouldn't necessarily be faster and
    would complicate reasoning about the circuit breaker. Revisit this if a
    real backend's constraints turn out to say otherwise.

    If `metrics` is provided, a fresh `PollingMetrics` is passed into
    `wait_for_result` for each term and folded into `metrics` -- this is
    the only reason `metrics` exists as a separate parameter rather than
    just being computed by the caller from timing `evaluate_energy` as a
    whole: per-term retry/circuit-breaker counts aren't otherwise visible
    from outside `wait_for_result` at all.
    """
    total = 0.0
    for term in molecule.terms:
        if not term.qubits:
            total += term.coefficient  # identity term, no circuit needed
            continue

        qc = build_measurement_circuit(params, term, molecule=molecule, ansatz=ansatz)
        circuit = Circuit(
            name=f"vqe-term-{''.join(f'{q}{p}' for q, p in term.qubits.items())}",
            num_qubits=molecule.num_qubits,
            payload=qc,
            shots=shots,
        )
        handle = await backend.submit(circuit)

        term_metrics = PollingMetrics() if metrics is not None else None
        result = await wait_for_result(
            backend, handle, config=polling_config or PollingConfig(), metrics=term_metrics
        )

        if metrics is not None and term_metrics is not None:
            metrics.quantum_time_s += term_metrics.wait_time_s
            metrics.retry_count += term_metrics.transient_retries
            if term_metrics.circuit_breaker_was_open:
                metrics.circuit_breaker_trips += 1

        assert result.counts is not None
        expectation = pauli_expectation_from_counts(result.counts, term)
        total += term.coefficient * expectation

    return total


def run_vqe(
    backend: QuantumBackend,
    *,
    initial_params: list[float] | None = None,
    shots: int = 8192,
    max_iterations: int = 100,
    molecule: MolecularHamiltonian = H2,
    ansatz: Ansatz = DEFAULT_ANSATZ,
) -> VQEResult:
    """Runs the full VQE feedback loop. Must be called from a plain
    synchronous context (see module docstring for why) -- e.g. directly
    from `if __name__ == "__main__":`, not from inside `asyncio.run(...)`.
    """
    expected_params = ansatz.parameter_count(molecule.num_qubits)
    params0 = initial_params if initial_params is not None else [0.0] * expected_params
    if len(params0) != expected_params:
        raise ValueError(f"expected {expected_params} initial parameters, got {len(params0)}")
    history: list[VQEIterationLog] = []
    iteration_counter = 0

    def cost(params: list[float]) -> float:
        nonlocal iteration_counter
        iteration_counter += 1

        iter_start = time.monotonic()
        iter_metrics = VQEIterationMetrics()
        energy = asyncio.run(
            evaluate_energy(
                backend,
                list(params),
                shots=shots,
                metrics=iter_metrics,
                molecule=molecule,
                ansatz=ansatz,
            )
        )
        iter_wall_time_s = time.monotonic() - iter_start

        # "Classical time" here is total iteration wall time minus time
        # actually spent waiting on the backend -- an approximation, not
        # an isolated measurement of COBYLA's own CPU time (that would
        # need instrumenting scipy.optimize internals, out of scope).
        # Reasonable given this iteration's other overhead (asyncio.run()
        # setup/teardown, this function itself) is negligible next to real
        # quantum wait time on anything but a trivially fast backend.
        classical_time_s = max(0.0, iter_wall_time_s - iter_metrics.quantum_time_s)

        history.append(
            VQEIterationLog(
                iteration=iteration_counter,
                params=list(params),
                energy=energy,
                quantum_time_s=iter_metrics.quantum_time_s,
                classical_time_s=classical_time_s,
                retry_count=iter_metrics.retry_count,
                circuit_breaker_trips=iter_metrics.circuit_breaker_trips,
            )
        )
        return energy

    res = minimize(cost, params0, method="COBYLA", options={"maxiter": max_iterations})

    return VQEResult(
        optimal_params=list(res.x),
        electronic_energy=res.fun,
        total_energy=res.fun + molecule.total_energy_offset,
        history=history,
    )
