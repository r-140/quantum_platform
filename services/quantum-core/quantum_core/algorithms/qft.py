"""
Quantum Fourier Transform (QFT) and its inverse.

QFT maps a computational basis state |x> to a superposition whose
amplitudes are the discrete Fourier transform of the standard basis vector:
QFT|x> = (1/sqrt(N)) * sum_y  exp(2*pi*i*x*y/N) |y>.

This is a building block, not an algorithm on its own: QPE (`qpe.py`) uses
the inverse QFT to read out an eigenphase, and (in the future) it would
also be the core of Shor's period-finding.

The exact gate sequence -- which qubit is processed first, the sign of the
controlled-phase angle, and whether the final swaps happen before or after
the H/controlled-phase loop -- has several inequivalent-looking but
individually self-consistent conventions in textbooks, and getting it wrong
produces a circuit that *looks* like QFT but silently computes the wrong
thing. Before writing this file, the exact construction below was checked
against the direct DFT definition via a standalone numpy script (bit-exact
match, error ~1e-15) rather than trusted from memory. See
docs/algorithms/qft_qpe.md for the verification and the specific
convention chosen.
"""

from __future__ import annotations

import math

from qiskit import QuantumCircuit


def build_qft_circuit(num_qubits: int, *, inverse: bool = False) -> QuantumCircuit:
    """Builds a QFT (or, if `inverse=True`, inverse-QFT) circuit on
    `num_qubits` qubits, no measurement included -- callers append/compose
    this into a larger circuit (see `qpe.py`).
    """
    qc = QuantumCircuit(num_qubits, name="qft_dagger" if inverse else "qft")
    t = num_qubits

    if not inverse:
        # Verified convention: swaps first, then process qubits 0..t-1 in
        # order, each followed by controlled phases from later qubits with
        # POSITIVE angle pi/2^d.
        for i in range(t // 2):
            qc.swap(i, t - i - 1)
        for j in range(t):
            qc.h(j)
            for k in range(j + 1, t):
                d = k - j
                qc.cp(math.pi / (2 ** d), k, j)
    else:
        # Inverse: process qubits t-1..0 (reverse order), NEGATIVE angle,
        # swaps at the end. This is the literal adjoint of the forward
        # construction above, verified independently (not just "assumed
        # dagger") against the direct inverse-DFT definition.
        idxs = list(reversed(range(t)))
        for pos, j in enumerate(idxs):
            qc.h(j)
            for k in idxs[pos + 1 :]:
                d = abs(idxs.index(k) - idxs.index(j))
                qc.cp(-math.pi / (2 ** d), k, j)
        for i in range(t // 2):
            qc.swap(i, t - i - 1)

    return qc