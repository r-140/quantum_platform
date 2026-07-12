"""
Grover search over an arbitrary boolean expression (SAT-style constraint
satisfaction) -- a materially more realistic use case than `grover.py`'s
`GroverProblem`.

In `grover.py`, the search target is known in advance and hand-baked into
the oracle via X-gates around a multi-controlled-Z (useful to demonstrate
the amplitude-amplification mechanics cleanly, kept there as a "hello
world"). Here, only a *verification* criterion is known -- a boolean
expression over named variables -- and neither the answer(s) nor how many
solutions exist is assumed to be known in advance. This mirrors real Grover
use cases: small SAT instances, constraint satisfaction, or any problem
framed as "find x such that predicate(x) is true" where checking a
candidate is cheap but search space is not indexable.

The oracle itself is built by Qiskit's `PhaseOracleGate`, which parses a
Python-like boolean expression string (`&`, `|`, `~`, `^`) directly into a
phase-flip oracle circuit. This replaced the older `PhaseOracle`/
`classical_function` machinery, which depended on the external `tweedledum`
library (removed as of Qiskit 2.0). `PhaseOracleGate` needs no extra
ancilla qubit -- it's a genuine phase oracle, matching what the diffuser
in `grover.py` expects.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass

from qiskit import QuantumCircuit
from qiskit.circuit.library import PhaseOracleGate

from .grover import _apply_diffuser


class _Bit(int):
    """Thin int wrapper so `~` means logical NOT on a 0/1 value.

    Plain Python ints don't work here: `~1 == -2` (two's complement), not
    `0`. This class exists purely so `eval_boolean_expression` can reuse
    Python's `&`/`|`/`~`/`^` operators with the same semantics
    `PhaseOracleGate` uses to parse its expression strings.
    """

    def __invert__(self) -> "_Bit":
        return _Bit(1 - int(self))

    def __and__(self, other: int) -> "_Bit":
        return _Bit(int(self) & int(other))

    def __or__(self, other: int) -> "_Bit":
        return _Bit(int(self) | int(other))

    def __xor__(self, other: int) -> "_Bit":
        return _Bit(int(self) ^ int(other))


def eval_boolean_expression(expression: str, assignment: dict[str, bool]) -> bool:
    """Classically evaluates the same expression syntax `PhaseOracleGate`
    accepts, for a single variable assignment. Used to classically verify a
    candidate returned by the quantum search, and to brute-force count
    solutions for picking the Grover iteration count in this demo (see
    `BooleanSearchProblem.count_solutions` docstring for why that's only
    reasonable at small scale).
    """
    env = {name: _Bit(1 if val else 0) for name, val in assignment.items()}
    result = eval(expression, {"__builtins__": {}}, env)  # noqa: S307 - restricted env, no builtins
    return bool(int(result))


@dataclass(frozen=True)
class BooleanSearchProblem:
    """A Grover search problem defined by a verification criterion, not a
    pre-known answer.

    `variables` fixes the qubit order: `variables[0]` maps to qubit 0, etc.
    `expression` uses Qiskit's boolean-expression syntax: `&` (AND),
    `|` (OR), `~` (NOT), `^` (XOR).
    """

    variables: list[str]
    expression: str

    @property
    def num_qubits(self) -> int:
        return len(self.variables)

    def count_solutions(self) -> list[str]:
        """Brute-force enumerates all satisfying assignments.

        This is only used here to pick the theoretically optimal Grover
        iteration count for the demo -- doing this classically for a real
        problem would defeat the entire point of using Grover (it's exactly
        the O(N) enumeration Grover exists to avoid). Real applications
        either estimate the solution count M from problem structure ahead
        of time, or use an *adaptive* search that doesn't require knowing M
        (see docs/algorithms/grover.md, "unknown number of solutions").
        """
        solutions = []
        for bits in itertools.product([False, True], repeat=self.num_qubits):
            assignment = dict(zip(self.variables, bits))
            if eval_boolean_expression(self.expression, assignment):
                bitstring = "".join("1" if b else "0" for b in reversed(bits))
                solutions.append(bitstring)
        return solutions


def build_sat_grover_circuit(problem: BooleanSearchProblem, *, iterations: int) -> QuantumCircuit:
    """Builds the Grover circuit for a boolean-expression search problem.

    Unlike `build_grover_circuit` in `grover.py`, `iterations` has no
    default here -- picking it requires knowing (or estimating) the number
    of solutions, which is the caller's responsibility (see
    `BooleanSearchProblem.count_solutions` and `grover.optimal_iterations`).
    """
    n = problem.num_qubits
    oracle_gate = PhaseOracleGate(problem.expression, var_order=problem.variables)

    qc = QuantumCircuit(n, name="grover-sat")
    qc.h(range(n))

    for _ in range(iterations):
        qc.append(oracle_gate, range(n))
        _apply_diffuser(qc, n)

    qc.measure_all()
    return qc