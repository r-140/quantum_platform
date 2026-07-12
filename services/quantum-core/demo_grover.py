"""
Runs Grover's algorithm against AerBackend: searches an 8-entry space
(3 qubits) for a specific marked entry, the way you might look up a row ID
in an unindexed table.

Run with:  python demo_grover.py
"""

from __future__ import annotations

import asyncio

from quantum_core.algorithms.grover import (
    GroverProblem,
    build_grover_circuit,
    optimal_iterations,
)
from quantum_core.backends.aer_backend import AerBackend
from quantum_core.backends.base import Circuit
from quantum_core.sync.polling import PollingConfig, wait_for_result


async def main() -> None:
    problem = GroverProblem(num_qubits=3, marked_states=["101"])
    iterations = optimal_iterations(problem.num_qubits, len(problem.marked_states))
    print(
        f"searching {2 ** problem.num_qubits} entries for {problem.marked_states}, "
        f"{iterations} Grover iteration(s)"
    )

    qc = build_grover_circuit(problem, iterations=iterations)

    backend = AerBackend(seed_simulator=7)
    circuit = Circuit(name="grover-search", num_qubits=problem.num_qubits, payload=qc, shots=1024)

    handle = await backend.submit(circuit)
    result = await wait_for_result(backend, handle, config=PollingConfig(timeout_s=15.0))

    print(f"status={result.status}")
    assert result.counts is not None
    for state, count in sorted(result.counts.items(), key=lambda kv: -kv[1]):
        marker = " <-- marked" if state in problem.marked_states else ""
        print(f"  {state}: {count}{marker}")


if __name__ == "__main__":
    asyncio.run(main())