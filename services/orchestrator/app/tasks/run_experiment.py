"""
Standard quantum gates as numpy matrices.

Convention: gates act on computational basis {|0⟩, |1⟩}
|0⟩ = [1, 0]ᵀ,  |1⟩ = [0, 1]ᵀ

All single-qubit gates are 2x2 unitary matrices.
Two-qubit gates are 4x4 matrices in {|00⟩,|01⟩,|10⟩,|11⟩} basis.
"""

import numpy as np

# ─── Constants ────────────────────────────────────────────────────────────────
_I2 = np.eye(2, dtype=complex)

# ─── Single-qubit gates ───────────────────────────────────────────────────────

# Pauli gates
X = np.array([[0, 1],
              [1, 0]], dtype=complex)  # NOT / bit-flip

Y = np.array([[0, -1j],
              [1j,  0]], dtype=complex)

Z = np.array([[1,  0],
              [0, -1]], dtype=complex)  # phase-flip

# Hadamard — creates superposition: |0⟩ → (|0⟩+|1⟩)/√2
H = np.array([[1,  1],
              [1, -1]], dtype=complex) / np.sqrt(2)

# Phase gates
S = np.array([[1, 0],
              [0, 1j]], dtype=complex)   # S = √Z

T = np.array([[1, 0],
              [0, np.exp(1j * np.pi / 4)]], dtype=complex)  # T = ⁴√Z

# Identity
I = _I2.copy()


def Rx(theta: float) -> np.ndarray:
    """Rotation around X axis by angle theta. Rx(π) = -iX"""
    c, s = np.cos(theta / 2), np.sin(theta / 2)
    return np.array([[c,    -1j * s],
                     [-1j * s,   c]], dtype=complex)


def Ry(theta: float) -> np.ndarray:
    """Rotation around Y axis by angle theta."""
    c, s = np.cos(theta / 2), np.sin(theta / 2)
    return np.array([[c, -s],
                     [s,  c]], dtype=complex)


def Rz(theta: float) -> np.ndarray:
    """
    Rotation around Z axis by angle theta.
    This is the most common gate in real hardware —
    implemented as a virtual phase shift (zero cost, zero error).
    """
    return np.array([[np.exp(-1j * theta / 2), 0],
                     [0, np.exp(1j * theta / 2)]], dtype=complex)


def Phase(phi: float) -> np.ndarray:
    """General phase gate P(φ): |1⟩ → e^(iφ)|1⟩"""
    return np.array([[1, 0],
                     [0, np.exp(1j * phi)]], dtype=complex)


# ─── Two-qubit gates ──────────────────────────────────────────────────────────

# CNOT (CX) — flips target if control is |1⟩
# Basis order: |00⟩, |01⟩, |10⟩, |11⟩
CNOT = np.array([[1, 0, 0, 0],
                 [0, 1, 0, 0],
                 [0, 0, 0, 1],
                 [0, 0, 1, 0]], dtype=complex)

CX = CNOT  # alias

# CZ — phase-flip target if both qubits are |1⟩
CZ = np.array([[1, 0, 0,  0],
               [0, 1, 0,  0],
               [0, 0, 1,  0],
               [0, 0, 0, -1]], dtype=complex)

# SWAP — swaps two qubits
SWAP = np.array([[1, 0, 0, 0],
                 [0, 0, 1, 0],
                 [0, 1, 0, 0],
                 [0, 0, 0, 1]], dtype=complex)

# iSWAP — native gate in many superconducting architectures (Google)
# Swaps qubits and adds phase of i
iSWAP = np.array([[1,  0,  0, 0],
                  [0,  0, 1j, 0],
                  [0, 1j,  0, 0],
                  [0,  0,  0, 1]], dtype=complex)


def CRz(theta: float) -> np.ndarray:
    """Controlled-Rz rotation — building block for QFT."""
    return np.array([[1, 0, 0, 0],
                     [0, 1, 0, 0],
                     [0, 0, 1, 0],
                     [0, 0, 0, np.exp(1j * theta)]], dtype=complex)


def controlled(gate: np.ndarray) -> np.ndarray:
    """
    Construct controlled version of any single-qubit gate.
    |0⟩⟨0| ⊗ I + |1⟩⟨1| ⊗ U
    """
    if gate.shape != (2, 2):
        raise ValueError("Can only control single-qubit gates")
    result = np.eye(4, dtype=complex)
    result[2:, 2:] = gate
    return result


# ─── Three-qubit gates ────────────────────────────────────────────────────────

def toffoli() -> np.ndarray:
    """
    Toffoli gate (CCNOT) — 8x8 matrix.
    Flips target qubit if both control qubits are |1⟩.
    Universal classical gate — can implement any boolean function.

    Basis: |000⟩, |001⟩, |010⟩, |011⟩, |100⟩, |101⟩, |110⟩, |111⟩
    """
    gate = np.eye(8, dtype=complex)
    # Swap |110⟩ and |111⟩
    gate[6, 6] = 0
    gate[7, 7] = 0
    gate[6, 7] = 1
    gate[7, 6] = 1
    return gate


# ─── Utility functions ────────────────────────────────────────────────────────

def is_unitary(gate: np.ndarray, tol: float = 1e-10) -> bool:
    """Check if matrix is unitary: U†U = I"""
    product = gate.conj().T @ gate
    return np.allclose(product, np.eye(len(gate)), atol=tol)


def gate_fidelity(U: np.ndarray, V: np.ndarray) -> float:
    """
    Average gate fidelity between two unitaries.
    F = |Tr(U†V)|² / d²  where d is matrix dimension.
    """
    d = len(U)
    return float(np.abs(np.trace(U.conj().T @ V)) ** 2 / d ** 2)
