"""
Runs a simple Bell-state circuit through AerBackend, using the same
wait_for_result() synchronization primitive used with MockHardwareBackend.
This is the first end-to-end run of a *real* quantum circuit through the
platform's abstraction layer.

Run with:  python demo_aer.py
"""

from __future__ import annotations

import asyncio

from qiskit import QuantumCircuit

from quantum_core.backends.aer_backend import AerBackend
from quantum_core.backends.base import Circuit
from quantum_core.sync.polling import PollingConfig, wait_for_result


def build_bell_circuit() -> QuantumCircuit:
    qc = QuantumCircuit(2)
    qc.h(0)
    qc.cx(0, 1)
    qc.measure_all()
    return qc


async def main() -> None:
    backend = AerBackend(seed_simulator=42)
    circuit = Circuit(name="bell-state", num_qubits=2, payload=build_bell_circuit(), shots=1024)

    handle = await backend.submit(circuit)
    print(f"submitted job_id={handle.job_id} to {handle.backend_name}")

    result = await wait_for_result(backend, handle, config=PollingConfig(timeout_s=15.0))
    print(f"status={result.status} counts={result.counts} metadata={result.metadata}")


if __name__ == "__main__":
    asyncio.run(main())
