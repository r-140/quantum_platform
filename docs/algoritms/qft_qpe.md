# QFT / Quantum Phase Estimation (QPE)

## The problem

Given a unitary operator U and one of its eigenstates |ψ⟩, such that
U|ψ⟩ = e^(2πiφ)|ψ⟩. QPE estimates φ to t bits of precision, using t
extra ("counting") qubits.

Why this matters in practice: φ directly encodes an eigenvalue of U. If
you take U = e^(-iHt) for a molecule's Hamiltonian H (after
time-discretization via Trotterization), QPE turns "find the ground-
state energy" into "find the phase." This is an exact but expensive
method in terms of qubit count and circuit depth — usually out of reach
for real molecules on today's NISQ hardware, which is why VQE is
interesting as a "cheaper" alternative (the project's next step).
Hamiltonian simulation itself (turning H₂ into U = e^(-iHt)) isn't part
of this module yet — that's a separate substantial piece of work, to be
added when moving to the VQE comparison.

## How it works

1. t qubits in state |0...0⟩ (the counting register) + a register
   holding |ψ⟩.
2. `H` on all counting qubits → equal superposition.
3. For each counting qubit j, apply controlled-U^(2^j), controlled by
   that qubit, onto the |ψ⟩ register. Since |ψ⟩ is an eigenstate, this
   doesn't change |ψ⟩ — it "injects" the phase e^(2πiφ·2^j) into the
   amplitude of the superposition branch where qubit j is 1 (phase
   kickback again, the same mechanism as in Grover's SAT oracle).
4. After all j, the counting register's state is exactly the QFT of the
   number k = φ·2ᵗ (when φ·2ᵗ is an integer, which isn't always the
   case).
5. Apply the **inverse QFT** — this "decodes" k back into the
   computational basis. Measurement gives k with high probability (100%
   if φ is exactly representable in t bits; otherwise, a peak near the
   nearest k).

## QFT: definition, and why the construction isn't trivial

The QFT maps a basis state |x⟩ to:

```
QFT|x⟩ = (1/√N) · Σ_y exp(2πi·x·y/N) |y⟩
```

The formula is simple, but translating it into a specific gate sequence
(the order in which qubits are processed, the sign of the
controlled-phase angle, and — where exactly to place the final swaps:
before or after the Hadamard/controlled-phase cycle) is a common source
of silent bugs: an incorrect version *looks* like a QFT (it uses the
same gates!) but computes the wrong thing, and the result comes out
"almost right," with the wrong probability distribution.

### How this was verified

Before writing `qft.py`, an independent numpy implementation (no
Qiskit) was written, implementing the QFT by explicitly applying
`H`/controlled-phase/`swap` to the state vector bit by bit. The first
attempt (based on intuition from generic textbook patterns) **did not
match** the direct DFT definition — up to 65% amplitude error. Rather
than keep guessing, all eight combinations were enumerated (qubit
processing order: forward/reverse; angle sign: +/-; swap position:
start/end/none) against direct computation of the DFT from the formula
above, as the reference. Four working combinations were found (really 2
independent solutions, each in two structurally equivalent forms).

The convention chosen and ported to `qft.py`:
- **Forward QFT**: swap at the start, then qubits are processed in order
  0..t-1, each getting an `H`, followed by controlled-phase with a
  **positive** angle π/2^d from later qubits.
- **Inverse QFT**: qubits processed in reverse order t-1..0, angle
  **negative**, swap at the end.

Both versions were checked against direct DFT with ~1e-15 error (machine
precision) across all 8 basis states for 3 qubits.

## Verifying QPE as a whole

Once the correct QFT/inverse-QFT construction was locked in, a full
numpy simulation of QPE (H on the counting register → controlled-phase →
inverse QFT → measurement) was run for two cases:

**φ = 5/8, exactly representable in 3 bits:**
```
k=5 (φ_est=0.625): probability = 1.0000   (all other k: 0)
```
A deterministic result, as expected in this special case.

**φ = 0.3, NOT exactly representable in 3 bits:**
```
k=2 (φ_est=0.25): probability = 0.5775   <- peak
k=3 (φ_est=0.375): probability = 0.2593
... (the rest spread across other k)
```
A peak near the true value, rather than an exact hit — this is expected
QPE behavior under limited precision (t bits give resolution 1/2ᵗ), and
**not a bug**. A developer seeing this result for the first time could
easily mistake the spread-out distribution for a malfunction — worth
calling out this distinction from the "deterministic" case explicitly.

## ⚠️ Degree of verification before porting to Qiskit

As with previous files, my working environment has no network access to
install Qiskit, so the final Qiskit code (`qft.py`, `qpe.py`,
`demo_qpe.py`) hasn't been run directly. What's verified and what isn't:

- **Independently verified**: the QFT/inverse-QFT/QPE math itself
  (numpy, bit-for-bit match against direct DFT; both the deterministic
  and spread-out QPE cases behave exactly as theory predicts).
- **Not verified directly**: the specific Qiskit API calls — `qc.cp()`,
  `Operator(unitary).power()`, `UnitaryGate(...).control(1)`,
  `qc.compose()`, `qc.append(gate, qubits)` with partial measurement
  into a t-sized classical register (rather than `measure_all()`, as in
  earlier demos). Particularly worth a close look: the qubit ordering in
  `qc.append(cu, [counting[j], *target])` — if the result ends up not
  focused on the expected k, this is the most likely culprit, not the
  math (which has already been confirmed separately).

Run `demo_qpe.py` and `demo_qpe.py --inexact` first and send me the
result.

## Usage in this project

`quantum_core/algorithms/qft.py`:
- `build_qft_circuit(num_qubits, inverse=False)` — a reusable primitive,
  independent of QPE.

`quantum_core/algorithms/qpe.py`:
- `controlled_power_gate(unitary, power)` — controlled-U^power via exact
  matrix exponentiation (fine for a simulator; real hardware would need
  a different approach);
- `build_qpe_circuit(unitary, num_counting_qubits, eigenstate_prep)` —
  assembles the full circuit, measures only the counting register.

Both modules are backend-agnostic — like `grover.py`/`sat_search.py`,
they only build a `qiskit.QuantumCircuit` and know nothing about how or
where it will be executed.
