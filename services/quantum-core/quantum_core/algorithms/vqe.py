"""
Variational Quantum Eigensolver (VQE) for the H2 molecule -- the NISQ-era
counterpart to `qpe.py`. QPE estimates an eigenvalue exactly but needs deep
circuits (Hamiltonian simulation via Trotterization, many ancilla qubits for
precision); VQE trades that exactness for shallow, hardware-friendly
circuits by pushing the hard part onto a classical optimizer. This is why
VQE, not QPE, is what actually runs on today's NISQ hardware for chemistry.

Hamiltonian: H2 at bond length 0.75 Angstrom, Bravyi-Kitaev-mapped and
symmetry-reduced to 2 qubits (O'Malley et al., "Scalable Quantum Simulation
of Molecular Energies", Phys. Rev. X 6, 031007 (2016), Table 1):

    H = g0*I + g1*Z0 + g2*Z1 + g3*Z0Z1 + g4*Y0Y1 + g5*X0X1

Before being used here, these coefficients were verified independently (no
Qiskit) by directly diagonalizing the 4x4 matrix in numpy: the lowest
eigenvalue plus the nuclear repulsion term reproduces the literature ground
energy (~-1.137 Hartree) to within 0.01 Hartree. See docs/algorithms/vqe.md
for the derivation and the full verification, including a second,
independent check of the *measurement*-based expectation-value pipeline
(basis rotations + sign formula) against the direct <psi|H|psi>
calculation, and of the ansatz's ability to reach the exact ground state
under classical (noiseless) optimization.
"""

from __future__ import annotations

from dataclasses import dataclass

from qiskit import QuantumCircuit

# g0..g5, O'Malley et al. Table 1, H2 at R=0.75 Angstrom.
H2_COEFFICIENTS = {
    "I": -0.4804,
    "Z0": 0.3435,
    "Z1": -0.4347,
    "Z0Z1": 0.5716,
    "Y0Y1": 0.0910,
    "X0X1": 0.0910,
}
H2_NUCLEAR_REPULSION = 0.7055696146  # Hartree, R=0.75 Angstrom
H2_LITERATURE_GROUND_ENERGY = -1.137  # Hartree, approximate, for sanity checks


@dataclass(frozen=True)
class PauliTerm:
    """One term of the Hamiltonian: `coefficient` * product of Pauli
    operators named in `qubits` (e.g. {0: 'Z', 1: 'Z'} for Z0Z1). An empty
    `qubits` dict represents the identity term -- it contributes a constant
    energy shift and needs no quantum circuit at all.
    """

    coefficient: float
    qubits: dict[int, str]


H2_HAMILTONIAN: list[PauliTerm] = [
    PauliTerm(H2_COEFFICIENTS["I"], {}),
    PauliTerm(H2_COEFFICIENTS["Z0"], {0: "Z"}),
    PauliTerm(H2_COEFFICIENTS["Z1"], {1: "Z"}),
    PauliTerm(H2_COEFFICIENTS["Z0Z1"], {0: "Z", 1: "Z"}),
    PauliTerm(H2_COEFFICIENTS["Y0Y1"], {0: "Y", 1: "Y"}),
    PauliTerm(H2_COEFFICIENTS["X0X1"], {0: "X", 1: "X"}),
]


def build_ansatz(params: list[float]) -> QuantumCircuit:
    """Hardware-efficient 2-qubit ansatz: RY-RY, a single CX, then RY-RY
    again. 4 free parameters.

    This is deliberately *not* the chemically-motivated UCC single-excitation
    ansatz sometimes used for this exact Hamiltonian in the literature --
    it's a generic, NISQ-friendly ansatz, chosen because (a) it's simple
    enough to verify unambiguously, and (b) hardware-efficient ansätze of
    this kind are what's actually used on real NISQ devices, precisely
    because chemically-derived UCC circuits are often too deep to run
    reliably on today's hardware. It was verified (via noiseless classical
    optimization in numpy/scipy, independent of Qiskit) to be expressive
    enough to reach the exact ground state of the H2 Hamiltonian above --
    see docs/algorithms/vqe.md.
    """
    if len(params) != 4:
        raise ValueError(f"expected 4 parameters, got {len(params)}")
    qc = QuantumCircuit(2, name="h2-ansatz")
    qc.ry(params[0], 0)
    qc.ry(params[1], 1)
    qc.cx(1, 0)
    qc.ry(params[2], 0)
    qc.ry(params[3], 1)
    return qc


def build_measurement_circuit(params: list[float], term: PauliTerm) -> QuantumCircuit:
    """Ansatz + basis-rotation gates for `term`'s Pauli factors + full
    measurement. Basis rotations (verified independently, see
    docs/algorithms/vqe.md): X -> H; Y -> Sdg then H; Z -> no rotation.

    Identity terms (`term.qubits == {}`) don't need a circuit at all --
    callers should special-case them and add `term.coefficient` directly
    (see `vqe_loop.evaluate_energy`).
    """
    if not term.qubits:
        raise ValueError("identity term needs no measurement circuit -- special-case it")

    qc = build_ansatz(params)
    for qubit, pauli in term.qubits.items():
        if pauli == "X":
            qc.h(qubit)
        elif pauli == "Y":
            qc.sdg(qubit)
            qc.h(qubit)
        elif pauli != "Z":
            raise ValueError(f"unknown Pauli operator {pauli!r}")
    qc.measure_all()
    return qc


def pauli_expectation_from_counts(counts: dict[str, int], term: PauliTerm) -> float:
    """Computes <term> from measurement counts, assuming the appropriate
    basis rotation was already applied (see `build_measurement_circuit`).

    Convention (matches the rest of this project): counts keys use Qiskit's
    ordering -- leftmost character is the highest-index qubit, rightmost is
    qubit 0. For each shot, the term's eigenvalue is the product of
    (-1)^bit over exactly the qubits the term acts on (qubits not in
    `term.qubits` are marginalized out, contributing nothing to the sign).
    """
    total = sum(counts.values())
    if total == 0:
        raise ValueError("counts is empty")

    acc = 0.0
    for bitstring, count in counts.items():
        n = len(bitstring)
        sign = 1
        for qubit in term.qubits:
            bit = bitstring[n - 1 - qubit]
            if bit == "1":
                sign *= -1
        acc += sign * count
    return acc / total