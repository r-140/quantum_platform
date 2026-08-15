# VQE: ground-state energy of H₂

## The problem

Find the ground-state energy of the hydrogen molecule (H₂) at an
interatomic distance of 0.75 Å — the classic "first" quantum-chemistry
problem on a quantum computer, and a natural follow-on to QPE
(`qft_qpe.md`): the same goal (find an eigenvalue of the Hamiltonian),
but a different tool.

## Why VQE, not QPE, for this problem

QPE gives an exact answer, but requires implementing U = e^(-iHt) as a
circuit (Hamiltonian simulation via Trotterization) — a deep,
qubit-hungry construction that's often simply out of reach for real
molecules on today's NISQ hardware. VQE instead offloads most of the
complexity onto a classical optimizer: the quantum part stays shallow (a
handful of gates), and the "search" happens classically, through
iterative tuning of the circuit's parameters. This is exactly the
tradeoff that makes VQE a usable tool today, not just in theory.

## The Hamiltonian

Taken from O'Malley et al., *Scalable Quantum Simulation of Molecular
Energies*, Phys. Rev. X 6, 031007 (2016), Table 1 — H₂ at a distance of
0.75 Å, after Bravyi-Kitaev mapping and symmetry reduction down to 2
qubits:

```
H = g0·I + g1·Z0 + g2·Z1 + g3·Z0Z1 + g4·Y0Y1 + g5·X0X1

g0 = -0.4804   g1 = +0.3435   g2 = -0.4347
g3 = +0.5716   g4 = +0.0910   g5 = +0.0910
```
plus a nuclear-repulsion energy of 0.7055696146 Hartree, added to the
result separately (not part of the electronic Hamiltonian above).

### Verifying the coefficients

Before building anything in Qiskit, the coefficients were verified via
direct diagonalization of the 4×4 matrix in numpy — independent of
Qiskit and of the citation source:

```
Eigenvalues of H (electronic part): -1.851199, -0.252801, 0.0, 0.182400
+ nuclear repulsion: -1.145630 Hartree
Known literature value: ~-1.137 Hartree
```

A match within 0.01 Hartree — enough to trust the source of the
coefficients (the small residual difference is likely from rounding
the coefficients to 4 digits and the exact bond length used).

## Ansatz

Uses a generic hardware-efficient ansatz with 4 parameters, rather than
the "chemically motivated" UCC single-excitation ansatz sometimes cited
for this particular Hamiltonian in the literature:

```
RY(θ0) on q0        RY(θ1) on q1
CX(control=q1, target=q0)
RY(θ2) on q0        RY(θ3) on q1
```

This is a deliberate choice, not a simplification born of ignorance:
hardware-efficient ansätze are exactly what's used on real NISQ hardware
today, because "proper" chemical UCC circuits are often too deep to run
reliably. The price of that simplicity: the ansatz carries no chemical
interpretation (unlike UCC), only empirical expressiveness.

### Verifying the ansatz's expressiveness

Before using this ansatz, an independent noiseless optimization was run
in numpy/scipy (`COBYLA`, 20 random starting points): the best result
found matched the exact eigenvalue of the Hamiltonian to within
**machine precision** (a difference of 6.66×10⁻¹⁶) — meaning the ansatz
really is capable of exactly reaching the true ground state, not just
approximating it.

Separately, a mix-up in the `CX` convention was found (and documented in
the code with an explicit comment): the first matrix written down was
labeled "control=q0, target=q1", but explicit checking showed it was
actually `CX(control=q1, target=q0)` — i.e. the comment was wrong, even
though the circuit itself worked correctly. This is exactly the kind of
bug that would otherwise silently end up in the Qiskit code with
incorrect documentation.

## Measurement-based pipeline: from Hamiltonian to measurements

Unlike the numpy check (where `⟨ψ|H|ψ⟩` is computed exactly via matrix
multiplication), real hardware gives no access to the wavefunction —
only to measurement outcomes. For every non-identity term of the
Hamiltonian:

1. Rotate the measurement basis before measuring:
   - **X** → `H`
   - **Y** → `Sdg`, then `H`
   - **Z** → no rotation
2. Measure in the computational basis.
3. Recover `⟨P⟩` via: for each shot, the product of `(-1)^bit` over the
   qubits involved in the term (other qubits don't affect the sign).

### Verifying the whole pipeline

Both steps (basis rotations + the sign formula) were verified
independently:

- **The Y rotation rule** was checked explicitly: `H · Sdg` maps
  `|+i⟩ → |0⟩` and `|−i⟩ → |1⟩` — confirmed numerically, not taken from
  memory.
- **The full measurement-based pipeline** (basis rotations → exact
  probabilities → sign formula) was checked against direct `⟨ψ|H|ψ⟩`
  for random ansatz parameters — matched to 1e-9 (machine precision).
- **VQE with statistical noise** (8192 shots per term, as in the demo):
  the energy found differs from the exact value by just **0.0015
  Hartree** — below the "chemical accuracy" threshold (~0.0016 Hartree)
  commonly used as a benchmark in quantum chemistry.

## ⚠️ Degree of verification before porting to Qiskit

As with all previous files: the math itself (Hamiltonian, ansatz, basis
rotations, sign formula, full measurement-based pipeline, optimizer
convergence) was verified independently via numpy/scipy — fairly
rigorously, since this has more moving parts than Grover or QPE. **Not
verified**: the specific Qiskit calls (`qc.ry`, `qc.cx(1,0)`, `qc.sdg`,
`qc.measure_all`) and the full integration with
`AerBackend`/`wait_for_result`/`QuantumBackend`. Run `demo_vqe.py`
first.

Worth keeping an eye on **performance** separately: `demo_vqe.py` sends
up to 5 circuits per optimizer iteration (one per non-identity term of
the Hamiltonian), and COBYLA can take dozens of iterations — so hundreds
of round trips through `wait_for_result` in total. On `AerBackend` this
should be fast (simulation is instant, the only latency comes from
`run_in_executor`), but don't be surprised if the first run takes
noticeably longer than the previous demos.

## The sync/async bridge

`scipy.optimize.minimize` has a synchronous API, while all of
`QuantumBackend` is asynchronous. The bridge is implemented via
`asyncio.run()` on each COBYLA iteration (`run_vqe` in `vqe_loop.py`).
This was specifically checked against the real (not stubbed)
`MockHardwareBackend`/`wait_for_result` from this project before using
the pattern for real — otherwise it's easy to hit
`RuntimeError: asyncio.run() cannot be called from a running event loop`
if `run_vqe` gets called from inside an already-running event loop (for
example, wrapped in `asyncio.run(main())` the way the rest of this
project's demos are). That's exactly why `demo_vqe.py` is the one demo
where `main()` is **not** wrapped in `asyncio.run()`.

This is a deliberate tradeoff for a pet project, not a production-ready
solution — a real orchestrator would instead hold one persistent event
loop and bridge via `run_coroutine_threadsafe`.

## Usage in this project

`quantum_core/algorithms/vqe.py`:
- `H2_HAMILTONIAN` — the list of Hamiltonian terms;
- `build_ansatz(params)` — the 4-parameter hardware-efficient ansatz;
- `build_measurement_circuit(params, term)` — ansatz + basis rotation +
  measurement for a specific term;
- `pauli_expectation_from_counts(counts, term)` — recovers `⟨P⟩` from
  measurement counts.

`quantum_core/loops/vqe_loop.py`:
- `evaluate_energy(backend, params, shots)` — one full classical-quantum
  round trip (all Hamiltonian terms);
- `run_vqe(backend, ...)` — the full feedback loop with COBYLA. A
  **synchronous** function — call it directly, don't wrap it in
  `asyncio.run()`.
