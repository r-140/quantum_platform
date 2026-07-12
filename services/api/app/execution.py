"""
Bridges validated API requests to quantum_core execution. Kept separate
from routers/ so this logic is testable without spinning up FastAPI, and
so routers stay thin (HTTP concerns only: status codes, request/response
shaping).

Execution is currently synchronous-in-process: the API calls straight into
quantum_core and waits for the result before responding. There is no
queue yet -- that's the next piece of this project (RabbitMQ +
orchestrator service), at which point POST /experiments will enqueue and
return immediately with status=queued, and a separate worker process will
call the same functions defined here. Nothing in this module should need
to change when that happens; only the router's control flow will.
"""

from __future__ import annotations

from quantum_core.algorithms.grover import GroverProblem, build_grover_circuit, optimal_iterations
from quantum_core.algorithms.qpe import build_qpe_circuit
from quantum_core.algorithms.sat_search import BooleanSearchProblem, build_sat_grover_circuit
from quantum_core.algorithms.vqe import H2_NUCLEAR_REPULSION
from quantum_core.backends.base import Circuit, QuantumBackend
from quantum_core.loops.vqe_loop import run_vqe
from quantum_core.sync.polling import PollingConfig, wait_for_result

from app.schemas.experiments import (
    GroverRequest,
    QPERequest,
    SatGroverRequest,
    VQERequest,
)


async def run_grover(backend: QuantumBackend, request: GroverRequest) -> dict:
    num_qubits = len(request.marked_states[0])
    problem = GroverProblem(num_qubits=num_qubits, marked_states=request.marked_states)
    iterations = optimal_iterations(problem.num_qubits, len(problem.marked_states))

    qc = build_grover_circuit(problem, iterations=iterations)
    circuit = Circuit(name="grover", num_qubits=num_qubits, payload=qc, shots=request.shots)

    handle = await backend.submit(circuit)
    result = await wait_for_result(backend, handle, config=PollingConfig(timeout_s=30.0))

    return {
        "marked_states": request.marked_states,
        "iterations": iterations,
        "counts": result.counts,
    }


async def run_sat_grover(backend: QuantumBackend, request: SatGroverRequest) -> dict:
    problem = BooleanSearchProblem(variables=request.variables, expression=request.expression)
    solutions = problem.count_solutions()
    iterations = optimal_iterations(problem.num_qubits, len(solutions)) if solutions else 0

    if iterations == 0:
        return {
            "expression": request.expression,
            "solutions": solutions,
            "counts": None,
            "note": "no satisfying assignment exists -- nothing to search for",
        }

    qc = build_sat_grover_circuit(problem, iterations=iterations)
    circuit = Circuit(name="sat-grover", num_qubits=problem.num_qubits, payload=qc, shots=request.shots)

    handle = await backend.submit(circuit)
    result = await wait_for_result(backend, handle, config=PollingConfig(timeout_s=30.0))

    return {
        "expression": request.expression,
        "solutions": sorted(solutions),
        "iterations": iterations,
        "counts": result.counts,
    }


async def run_qpe(backend: QuantumBackend, request: QPERequest) -> dict:
    import math

    from qiskit import QuantumCircuit
    from qiskit.circuit.library import PhaseGate

    theta = 2 * math.pi * request.phi
    unitary = PhaseGate(theta)
    eigenstate_prep = QuantumCircuit(1)
    eigenstate_prep.x(0)

    qc = build_qpe_circuit(unitary, num_counting_qubits=request.num_counting_qubits, eigenstate_prep=eigenstate_prep)
    circuit = Circuit(name="qpe", num_qubits=qc.num_qubits, payload=qc, shots=request.shots)

    handle = await backend.submit(circuit)
    result = await wait_for_result(backend, handle, config=PollingConfig(timeout_s=30.0))

    t = request.num_counting_qubits
    counts_with_estimates = None
    if result.counts:
        counts_with_estimates = {
            bitstring: {"count": count, "phi_estimate": int(bitstring, 2) / (2**t)}
            for bitstring, count in result.counts.items()
        }

    return {
        "true_phi": request.phi,
        "resolution": 1 / (2**t),
        "results": counts_with_estimates,
    }


def run_vqe_sync(backend: QuantumBackend, request: VQERequest) -> dict:
    """Synchronous by design -- `run_vqe` bridges to asyncio internally per
    optimizer iteration (see quantum_core/loops/vqe_loop.py) and must be
    called from a plain sync context, not from inside a running event loop.
    The router offloads this to a threadpool (`run_in_threadpool`) rather
    than awaiting it directly -- see routers/experiments.py for why.
    """
    result = run_vqe(backend, shots=request.shots, max_iterations=request.max_iterations)
    return {
        "optimal_params": result.optimal_params,
        "electronic_energy": result.electronic_energy,
        "nuclear_repulsion": H2_NUCLEAR_REPULSION,
        "total_energy": result.total_energy,
        "iterations_run": len(result.history),
    }