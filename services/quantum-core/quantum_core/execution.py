"""
Algorithm execution functions -- the actual "run this experiment" logic,
shared between the API service (synchronous-in-process mode) and the
orchestrator (queued mode, via RabbitMQ).

Deliberately takes plain Python types (str/list/float/int) as parameters,
not framework-specific request/message objects -- neither the API's
Pydantic schemas nor the orchestrator's task-message format need to be a
dependency of quantum_core this way, and the actual algorithm-running logic
isn't duplicated between the two call sites. `services/api/app/execution.py`
and `services/orchestrator/app/worker.py` are both thin adapters around
these functions: unpack their respective request/message format, call
here, repack the result.
"""

from __future__ import annotations

import math

from quantum_core.algorithms.grover import GroverProblem, build_grover_circuit, optimal_iterations
from quantum_core.algorithms.qpe import build_qpe_circuit
from quantum_core.algorithms.sat_search import BooleanSearchProblem, build_sat_grover_circuit
from quantum_core.algorithms.vqe import H2_NUCLEAR_REPULSION
from quantum_core.backends.base import Circuit, QuantumBackend
from quantum_core.loops.vqe_loop import run_vqe
from quantum_core.sync.polling import PollingConfig, wait_for_result


async def run_grover(backend: QuantumBackend, marked_states: list[str], shots: int = 1024) -> dict:
    num_qubits = len(marked_states[0])
    problem = GroverProblem(num_qubits=num_qubits, marked_states=marked_states)
    iterations = optimal_iterations(problem.num_qubits, len(problem.marked_states))

    qc = build_grover_circuit(problem, iterations=iterations)
    circuit = Circuit(name="grover", num_qubits=num_qubits, payload=qc, shots=shots)

    handle = await backend.submit(circuit)
    result = await wait_for_result(backend, handle, config=PollingConfig(timeout_s=30.0))

    return {
        "marked_states": marked_states,
        "iterations": iterations,
        "counts": result.counts,
    }


async def run_sat_grover(
    backend: QuantumBackend, variables: list[str], expression: str, shots: int = 1024
) -> dict:
    problem = BooleanSearchProblem(variables=variables, expression=expression)
    solutions = problem.count_solutions()
    iterations = optimal_iterations(problem.num_qubits, len(solutions)) if solutions else 0

    if iterations == 0:
        return {
            "expression": expression,
            "solutions": solutions,
            "counts": None,
            "note": "no satisfying assignment exists -- nothing to search for",
        }

    qc = build_sat_grover_circuit(problem, iterations=iterations)
    circuit = Circuit(name="sat-grover", num_qubits=problem.num_qubits, payload=qc, shots=shots)

    handle = await backend.submit(circuit)
    result = await wait_for_result(backend, handle, config=PollingConfig(timeout_s=30.0))

    return {
        "expression": expression,
        "solutions": sorted(solutions),
        "iterations": iterations,
        "counts": result.counts,
    }


async def run_qpe(
    backend: QuantumBackend, phi: float, num_counting_qubits: int = 3, shots: int = 1024
) -> dict:
    from qiskit import QuantumCircuit
    from qiskit.circuit.library import PhaseGate

    theta = 2 * math.pi * phi
    unitary = PhaseGate(theta)
    eigenstate_prep = QuantumCircuit(1)
    eigenstate_prep.x(0)

    qc = build_qpe_circuit(unitary, num_counting_qubits=num_counting_qubits, eigenstate_prep=eigenstate_prep)
    circuit = Circuit(name="qpe", num_qubits=qc.num_qubits, payload=qc, shots=shots)

    handle = await backend.submit(circuit)
    result = await wait_for_result(backend, handle, config=PollingConfig(timeout_s=30.0))

    t = num_counting_qubits
    counts_with_estimates = None
    if result.counts:
        counts_with_estimates = {
            bitstring: {"count": count, "phi_estimate": int(bitstring, 2) / (2**t)}
            for bitstring, count in result.counts.items()
        }

    return {
        "true_phi": phi,
        "resolution": 1 / (2**t),
        "results": counts_with_estimates,
    }


def run_vqe_sync(backend: QuantumBackend, shots: int = 8192, max_iterations: int = 80) -> dict:
    """Synchronous by design -- `run_vqe` bridges to asyncio internally per
    optimizer iteration (see quantum_core/loops/vqe_loop.py) and must be
    called from a plain sync context. Callers in an async context (like the
    API) must offload this via a threadpool; callers already running in a
    plain worker thread/process (like the orchestrator, depending on its
    consumer library) may be able to call it directly -- see each caller
    for specifics.
    """
    result = run_vqe(backend, shots=shots, max_iterations=max_iterations)
    return {
        "optimal_params": result.optimal_params,
        "electronic_energy": result.electronic_energy,
        "nuclear_repulsion": H2_NUCLEAR_REPULSION,
        "total_energy": result.total_energy,
        "iterations_run": len(result.history),
    }