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
from dataclasses import dataclass, field

from scipy.optimize import minimize

from quantum_core.algorithms.vqe import (
    H2_HAMILTONIAN,
    H2_NUCLEAR_REPULSION,
    build_measurement_circuit,
    pauli_expectation_from_counts,
)
from quantum_core.backends.base import Circuit, QuantumBackend
from quantum_core.sync.polling import PollingConfig, wait_for_result


@dataclass
class VQEIterationLog:
    params: list[float]
    energy: float


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
    """
    total = 0.0
    for term in H2_HAMILTONIAN:
        if not term.qubits:
            total += term.coefficient  # identity term, no circuit needed
            continue

        qc = build_measurement_circuit(params, term)
        circuit = Circuit(
            name=f"vqe-term-{''.join(f'{q}{p}' for q, p in term.qubits.items())}",
            num_qubits=2,
            payload=qc,
            shots=shots,
        )
        handle = await backend.submit(circuit)
        result = await wait_for_result(backend, handle, config=polling_config or PollingConfig())
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
) -> VQEResult:
    """Runs the full VQE feedback loop. Must be called from a plain
    synchronous context (see module docstring for why) -- e.g. directly
    from `if __name__ == "__main__":`, not from inside `asyncio.run(...)`.
    """
    params0 = initial_params or [0.0, 0.0, 0.0, 0.0]
    history: list[VQEIterationLog] = []

    def cost(params: list[float]) -> float:
        energy = asyncio.run(evaluate_energy(backend, list(params), shots=shots))
        history.append(VQEIterationLog(params=list(params), energy=energy))
        return energy

    res = minimize(cost, params0, method="COBYLA", options={"maxiter": max_iterations})

    return VQEResult(
        optimal_params=list(res.x),
        electronic_energy=res.fun,
        total_energy=res.fun + H2_NUCLEAR_REPULSION,
        history=history,
    )