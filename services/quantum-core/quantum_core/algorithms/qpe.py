"""
Quantum Phase Estimation (QPE).

Given a unitary U and one of its eigenstates |psi> (so that
U|psi> = e^(2*pi*i*phi) |psi>), QPE estimates the eigenphase phi to
`num_counting_qubits` bits of precision.

Why this matters beyond being "a QFT demo": phi directly encodes an
eigenvalue of U. This is the mechanism that, once Hamiltonian simulation
(Trotterized U = e^(-iHt)) is added to this project, would estimate
molecular ground-state energies -- QPE turns "find the eigenvalue of H"
into "find the eigenphase of e^(-iHt)". For now this module is generic and
works for any single-qubit unitary Gate and eigenstate; the Hamiltonian
simulation piece is intentionally out of scope here (see
docs/algorithms/qft_qpe.md for why that's a separate, larger undertaking,
and how it connects to the VQE comparison planned for H2).

Controlled-U^(2^j) powers are computed via exact matrix exponentiation
(`Operator.power`), since we're targeting the Aer simulator where this is
cheap and exact. On real hardware this would need a different approach
(repeated controlled-U application, or a dedicated power circuit).
"""

from __future__ import annotations

from qiskit import QuantumCircuit
from qiskit.circuit import Gate
from qiskit.circuit.library import UnitaryGate
from qiskit.quantum_info import Operator

from .qft import build_qft_circuit


def controlled_power_gate(unitary: Gate, power: int) -> Gate:
    """Builds a controlled-U^power gate via exact matrix exponentiation."""
    mat = Operator(unitary).power(power).data
    return UnitaryGate(mat, label=f"{unitary.name}^{power}").control(1)


def build_qpe_circuit(
    unitary: Gate,
    num_counting_qubits: int,
    eigenstate_prep: QuantumCircuit,
) -> QuantumCircuit:
    """Builds a full QPE circuit.

    Qubit layout: counting qubits occupy [0, num_counting_qubits), the
    target (eigenstate) register occupies
    [num_counting_qubits, num_counting_qubits + unitary.num_qubits).
    Only the counting register is measured -- its classical bitstring,
    read as an integer k, gives phi_estimate = k / 2^num_counting_qubits.

    `eigenstate_prep` must act on exactly `unitary.num_qubits` qubits and
    prepare (an approximation of) an eigenstate of `unitary`. The quality
    of that approximation directly limits how sharply QPE's output peaks
    around the true phase -- garbage eigenstate in, spread-out nonsense
    out.
    """
    num_target = unitary.num_qubits
    if eigenstate_prep.num_qubits != num_target:
        raise ValueError(
            f"eigenstate_prep acts on {eigenstate_prep.num_qubits} qubits, "
            f"expected {num_target} to match the unitary"
        )

    t = num_counting_qubits
    total = t + num_target
    qc = QuantumCircuit(total, t, name="qpe")

    counting = list(range(t))
    target = list(range(t, total))

    qc.compose(eigenstate_prep, qubits=target, inplace=True)
    qc.h(counting)

    for j in range(t):
        cu = controlled_power_gate(unitary, 2 ** j)
        qc.append(cu, [counting[j], *target])

    qft_dagger = build_qft_circuit(t, inverse=True)
    qc.append(qft_dagger.to_gate(label="QFT_dagger"), counting)

    qc.measure(counting, counting)
    return qc