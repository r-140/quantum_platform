# Grover's algorithm

## The problem

Given N = 2ⁿ unindexed records (e.g. table rows), and a way to check
whether a single candidate matches a search criterion — that's the
"oracle." Classically, this takes N/2 checks on average. Grover finds
the target record in O(√N) oracle calls — a quadratic speedup.

This isn't a replacement for a SQL index in today's real databases (there
the oracle *is* the index — O(log N) with no quantum mechanics needed).
Grover's value shows up where checking a candidate is expensive and
doesn't lend itself to indexing: SAT solvers, cryptanalysis (brute-force
key search), constraint-satisfaction problems.

## How it works (intuitively)

1. **Superposition**: `H` on every qubit — equal amplitude across all N
   states.
2. **Oracle**: flips the sign of the amplitude for "marked" states (the
   ones satisfying the search criterion). The amplitude's magnitude
   stays the same — visually, nothing changes if you measure right after
   this step.
3. **Diffuser** (inversion about the mean): reflects all amplitudes
   about the mean value. Since the marked states now have a negative
   sign, after the reflection their amplitude becomes noticeably larger
   than average — the probability of measuring them grows.
4. Steps 2-3 repeat `⌊(π/4)·√(N/M)⌋` times (M being the number of marked
   states). This is the optimum: **more iterations isn't better** —
   past the optimum, the amplitude "overshoots" back down and the
   success probability drops. A common implementation mistake is
   hardcoding a large iteration count instead of computing the optimum
   for the specific N.

## Oracle/diffuser construction in the code

Both steps are implemented via a multi-controlled Z (`_apply_mcz` in
`grover.py`), built as `H` – `mcx` – `H` on the last qubit, rather than
via Qiskit's ready-made helpers (`PhaseOracle`/`MCMT`) — this avoids an
optional dependency on `tweedledum`, which isn't needed for a
construction this simple.

The oracle for a specific marked state: `X` gates on the qubits where the
target string has a `0` (so the target state turns into "all ones"),
then MCZ, then the same `X`s again to undo them.

## Correctness verification

Before porting to Qiskit, the math itself (oracle + diffuser as
matrices) was independently verified via direct matrix multiplication in
numpy — no Qiskit — for n=3, one marked state, `101`:

```
optimal_iterations = 2
probability on '101': 94.5%   (random guessing would give 12.5%)
```

This confirms the algorithm's logic is correct independently of whether
the specific Qiskit API calls are correct.

## Complexity

| n (qubits) | N = 2ⁿ | Grover iterations | Classical checks (average) |
|---|---|---|---|
| 3  | 8     | 2   | 4       |
| 10 | 1024  | 25  | 512     |
| 20 | ~1M   | ~805 | ~524288 |

## Usage in this project

`quantum_core/algorithms/grover.py`:
- `GroverProblem(num_qubits, marked_states)` — describes the problem;
- `optimal_iterations(num_qubits, num_marked)` — computes the optimum;
- `build_grover_circuit(problem, iterations=None)` — returns a
  `qiskit.QuantumCircuit`, ready to pass into `Circuit.payload` for
  `AerBackend` (see `demo_grover.py`).

Not tied to a specific backend — can run on `AerBackend` today and, in
the future, on real hardware through the same `QuantumBackend`
interface.

⚠️ **Important caveat about this version**: here the oracle is built
using X gates that *already know* the target state (`marked_states` is
passed into the constructor). This is a great demonstration of amplitude
amplification mechanics, but it's **not a search** in the strict sense —
the programmer already knows the answer. Think of this file as the
"hello world" for Grover. The real search is in `sat_search.py` below.

## The SAT version: genuine criterion-based search

`quantum_core/algorithms/sat_search.py` implements a more realistic
case: instead of a known answer, we have a **checking criterion** — a
boolean expression over named variables (e.g.
`"(x0 | x1) & (~x1 | x2) & (x0 | ~x3)"`, the standard form for SAT/3-SAT
conditions). The oracle is built via Qiskit's `PhaseOracleGate` — it
parses this kind of expression and assembles the phase circuit itself,
with no manual X gates.

This is much closer to why Grover matters in practice: SAT solvers,
brute-forcing cryptographic keys, constraint-satisfaction problems —
anywhere it's easy to *check* a candidate but you don't know which one
fits, or even how many fit at all.

### The general oracle-construction method — and its limits

An oracle isn't built "automatically from any function" — but there is a
general **method**, going back to Bennett's 1973 construction: any
classically computable boolean function can be mechanically turned into
a reversible quantum circuit — replace every classical gate with its
Toffoli equivalent, "uncompute" all the intermediate garbage after the
computation, and apply phase kickback through an ancilla qubit in state
`|−⟩`. `PhaseOracleGate` is a ready-made implementation of exactly this
method for boolean expressions (AND/OR/NOT/XOR).

That does **not** mean any conceivable function fits equally easily into
an oracle:
- **Logical conditions** (SAT clauses) — translate almost mechanically.
- **Arithmetic conditions** ("x is divisible by 3") — require real
  reversible addition/multiplication circuits, which are noticeably
  bulkier.
- **Continuous/analytic criteria** (inflection points of a function,
  series expansion, residues of a complex function) — first require
  discretization (fixed-point representation), and even then a more
  fundamental question arises: such problems usually **have structure**
  (smoothness, periodicity, analyticity) that classical methods
  (Newton's method, FFT, contour integration) already exploit
  efficiently. Grover is an algorithm for *unstructured* search — it
  specifically ignores any structure in f. Forcing a structured problem
  into Grover means throwing that structure away, and it will almost
  always lose to specialized methods.

### Grover is not a miniature Shor's algorithm

It might look like the period-finding step in Shor's algorithm is a
special case of Grover ("same criterion: does x satisfy..."). That's not
the case, and the difference is fundamental:
- **Grover** — a geometric rotation in a 2D subspace of amplitudes
  (marked/unmarked), gives a **quadratic** O(√N) speedup, works for
  *any* black box with no structure at all.
- **QPE/Shor** — estimates the phase of a unitary operator's eigenvalue
  via constructive interference (QFT), gives an **exponential** speedup,
  but only because it exploits a specific algebraic structure of modular
  exponentiation (periodicity).

If period-finding really did reduce to Grover search over candidates
("check: a^r mod N == 1"), the speedup would be quadratic, not
exponential. That exponential gap is direct proof that these are
different families of algorithms (amplitude amplification vs.
Fourier/spectral methods), not one being a special case of the other.

### QRAM: a limitation for searching a "real database"

Grover is often thought of as "a quadratic speedup for database search"
— that too needs a caveat. Grover operates on *indices* (a superposition
over addresses `|i⟩`), not on record contents. If the criterion is a
computable function of the index (as in the SAT example above), this all
works honestly. But if you need to search by the **contents** of a
classical database, you need a mechanism that, given an index `|i⟩`,
efficiently (ideally O(log N), not O(N)) loads the corresponding data
`|data(i)⟩` into qubits — this is called **QRAM** (quantum random-access
memory). No practical QRAM exists today. This is one reason Grover's
quadratic speedup for "database search" often remains a theoretical
result rather than a practical one.

### Unknown number of solutions

`optimal_iterations()` needs to know the number of solutions M ahead of
time. `BooleanSearchProblem.count_solutions()` in `sat_search.py`
computes M by brute force — this only works honestly because the demo
problem is tiny (4 variables, 16 states). In a real case, brute-forcing
all combinations would defeat the entire point of using Grover.

The real solution to this problem is **adaptive search** (the
Boyer-Brassard-Høyer-Tapp technique, 1998): instead of a fixed iteration
count, take a random number of iterations from a growing range (1, then
random from [0,2), [0,4), [0,8)...), classically check the result after
each attempt (this is cheap — an O(1) predicate check), and if it
doesn't fit, grow the range. The expected number of oracle calls stays
O(√N/M) even without knowing M ahead of time. Adaptive search isn't
implemented in this project yet — left as a note for later, and one that
shouldn't sit in the backlog forever, since this is what makes Grover
truly "honest" without classical preprocessing.

## Usage in this project (SAT version)

`quantum_core/algorithms/sat_search.py`:
- `BooleanSearchProblem(variables, expression)` — describes the problem
  via a criterion, not a ready-made answer;
- `eval_boolean_expression()` — a classical evaluator (used both for
  brute-force counting of M and for checking the quantum result);
- `build_sat_grover_circuit(problem, iterations)` — builds the circuit
  via `PhaseOracleGate`, with no manual X gates.

⚠️ **Degree of verification**: the classical evaluator
(`eval_boolean_expression`, `count_solutions`) was verified against an
independent brute-force implementation — 7 solutions out of 16
combinations, match confirmed. The actual integration with
`PhaseOracleGate`, and the qubit/variable ordering, haven't been run
against real Qiskit (no network in my environment). Run
`demo_sat_grover.py` first and send me the result.
