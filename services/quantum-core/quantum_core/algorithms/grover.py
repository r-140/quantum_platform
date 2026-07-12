"""
Grover's algorithm: quantum search over an unstructured space.

Practical framing: given N = 2^n possible entries (e.g. rows in a table with
no index), and a way to check whether a candidate matches some criterion
(the oracle), find the matching entry. Classically this takes O(N) checks on
average. Grover's algorithm finds a marked entry with high probability in
O(sqrt(N)) oracle calls -- a quadratic speedup.

This module only builds the circuit; it says nothing about *how* it gets
executed. Execution is the job of a QuantumBackend implementation (mock or
Aer), kept separate so the algorithm is backend-agnostic and independently
testable -- in line with the hardware/software abstraction used elsewhere in
this project.

The oracle/diffuser construction here was verified independently against a
plain numpy state-vector simulation before being translated into Qiskit gate
calls (see docs/algorithms/grover.md for the derivation).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from qiskit import QuantumCircuit


@dataclass(frozen=True)
class GroverProblem:
    """A concrete search problem: which bitstrings count as a match.

    Bitstrings use the same convention as Qiskit's measurement counts:
    leftmost character is the highest-index qubit, rightmost is qubit 0.
    """

    num_qubits: int
    marked_states: list[str]

    def __post_init__(self) -> None:
        if not self.marked_states:
            raise ValueError("marked_states must contain at least one target bitstring")
        for s in self.marked_states:
            if len(s) != self.num_qubits:
                raise ValueError(
                    f"marked state {s!r} has {len(s)} bits, expected {self.num_qubits}"
                )
            if any(c not in "01" for c in s):
                raise ValueError(f"marked state {s!r} must contain only '0'/'1'")


def optimal_iterations(num_qubits: int, num_marked: int) -> int:
    """Number of Grover iterations that maximizes the probability of
    measuring a marked state: floor(pi/4 * sqrt(N/M)).

    Overshooting this count *decreases* the success probability again (the
    amplitude rotates past the marked states) -- this isn't "more iterations
    = better", which is a common misconception worth calling out.
    """
    if num_marked <= 0:
        raise ValueError("num_marked must be >= 1")
    n = 2 ** num_qubits
    return max(1, math.floor((math.pi / 4) * math.sqrt(n / num_marked)))


def _apply_oracle(qc: QuantumCircuit, marked_states: list[str], num_qubits: int) -> None:
    """Flips the phase of each marked state via the standard construction:
    X-gate any qubit that should read 0 in the target state (so the target
    becomes all-ones), apply a multi-controlled Z, then undo the X-gates.
    """
    for state in marked_states:
        zero_bits = [i for i, bit in enumerate(reversed(state)) if bit == "0"]
        for q in zero_bits:
            qc.x(q)

        _apply_mcz(qc, num_qubits)

        for q in zero_bits:
            qc.x(q)


def _apply_mcz(qc: QuantumCircuit, num_qubits: int) -> None:
    """Multi-controlled Z across all `num_qubits` qubits, built from
    multi-controlled X (mcx) sandwiched with Hadamards on the target qubit
    -- avoids depending on qiskit's higher-level MCMT/PhaseOracle helpers,
    which pull in optional dependencies (e.g. tweedledum) not needed here.
    """
    if num_qubits == 1:
        qc.z(0)
        return
    target = num_qubits - 1
    controls = list(range(num_qubits - 1))
    qc.h(target)
    qc.mcx(controls, target)
    qc.h(target)


def _apply_diffuser(qc: QuantumCircuit, num_qubits: int) -> None:
    """Inverts every amplitude about the average (amplitude amplification),
    boosting whatever the oracle just phase-flipped.
    """
    qc.h(range(num_qubits))
    qc.x(range(num_qubits))
    _apply_mcz(qc, num_qubits)
    qc.x(range(num_qubits))
    qc.h(range(num_qubits))


def build_grover_circuit(problem: GroverProblem, *, iterations: int | None = None) -> QuantumCircuit:
    """Builds the full Grover circuit: uniform superposition, then
    `iterations` rounds of oracle + diffuser, then measurement.

    If `iterations` is None, uses `optimal_iterations()` for the given
    problem size.
    """
    n = problem.num_qubits
    if iterations is None:
        iterations = optimal_iterations(n, len(problem.marked_states))

    qc = QuantumCircuit(n, name="grover")
    qc.h(range(n))

    for _ in range(iterations):
        _apply_oracle(qc, problem.marked_states, n)
        _apply_diffuser(qc, n)

    qc.measure_all()
    return qc