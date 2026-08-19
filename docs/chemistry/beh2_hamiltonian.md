# BeH₂ molecular Hamiltonian

This document defines the physics and reproducibility contract for the BeH₂
VQE extension. Numerical Pauli coefficients remain deliberately absent until a
generator and independent verification are committed.

## 1. Electronic structure and geometry

Neutral BeH₂ contains six electrons. Near equilibrium its gas-phase ground
state is linear, so a convenient geometry is

```text
H ---- Be ---- H
```

with equal Be–H distances. In the Born–Oppenheimer approximation,

$$
\hat H_\mathrm{elec}=
\sum_i\left(-\frac12\nabla_i^2
-\frac4{|\mathbf r_i-\mathbf R_\mathrm{Be}|}
-\sum_{A=1}^{2}\frac1{|\mathbf r_i-\mathbf R_{\mathrm H_A}|}\right)
+\sum_{i \lt j}\frac1{r_{ij}},
$$

and the nuclear scalar is

$$
E_\mathrm{nuc}=
\frac4{R_{\mathrm{BeH}_1}}+
\frac4{R_{\mathrm{BeH}_2}}+
\frac1{R_{\mathrm H_1\mathrm H_2}}.
$$

For a symmetric linear geometry with Be–H distance $R$, the H–H distance is
$2R$, hence $E_\mathrm{nuc}=8/R+1/(2R)$ in atomic units.

## 2. Orbitals and frozen core

In a minimal basis, Be contributes core 1s and valence 2s/2p character; the
two hydrogen atoms each contribute a 1s function. The resulting spin-orbital
space is already too large for the direct style of the two-qubit H₂ example.

The Be 1s pair is normally frozen. Four valence electrons remain, distributed
over bonding, non-bonding/near-degenerate, and antibonding molecular orbitals.
An active-space truncation is then required for a compact demonstration.

BeH₂ is more demanding than LiH because several valence configurations can
contribute and the Hamiltonian contains many more Pauli strings. The 2017
hardware-efficient VQE experiment treated molecular problems up to a
six-qubit BeH₂ Hamiltonian with more than one hundred Pauli terms. “Six qubits”
does not uniquely identify that Hamiltonian; it is the result of a particular
compact encoding and symmetry reduction.

## 3. From integrals to Pauli strings

After RHF and active-space transformation,

$$
\hat H_\mathrm{active}=
E_0+\sum_{pq\in A}\tilde h_{pq}a_p^\dagger a_q
+\frac12\sum_{pqrs\in A}h_{pqrs}
a_p^\dagger a_q^\dagger a_s a_r.
$$

A fermion-to-qubit mapping produces

$$
\hat H_q=\sum_{j=1}^{N_P}c_jP_j.
$$

For BeH₂, $N_P$ is operationally important. The current VQE evaluator
submits one circuit for every non-identity Pauli term on every optimizer
evaluation. A hundred terms and eighty optimizer evaluations imply roughly
eight thousand circuit submissions before retries. Commuting-term grouping is
therefore a likely required optimization, not only a theoretical refinement.

## 4. Proposed reproducible model

The initial project model should be intentionally small but fully specified:

| Setting | Proposed value |
|---|---|
| Geometry | linear H–Be–H, equal $R_\mathrm{BeH}=1.3\,\text{Å}$ |
| Charge / multiplicity | 0 / singlet |
| Orbital basis | STO-3G |
| Mean field | RHF |
| Frozen core | Be 1s pair |
| Active space | four valence electrons in explicitly recorded orbitals |
| Mapping | parity or Bravyi–Kitaev; fixed project-wide |
| Reduction | $\mathbb Z_2$ tapering with recorded sector |

The distance is a proposed demonstration geometry. It must be treated as part
of the model identifier, not as a universal equilibrium constant.

## 5. Symmetry and reference state

At linear symmetric geometry, spatial symmetry is physically useful, but the
first implementation need not encode the full molecular point group into the
quantum circuit. It must at least preserve or track:

- total electron number;
- spin projection/parity;
- the $\mathbb Z_2$ eigenvalues used for tapering;
- orbital ordering before and after tapering.

The Hartree–Fock bitstring should be generated from the same electronic-
structure problem and transformed by the same mapper. Hard-coding an initial
computational basis state independently of the mapping is error-prone.

## 6. Ansatz and measurement scaling

A qubit-count-driven hardware-efficient ansatz makes a BeH₂ circuit possible,
but it does not guarantee useful convergence. Verification should compare
several depths and a chemistry-informed reference. Useful measurements include:

- best and final energy error relative to exact diagonalization;
- variance $\langle H^2\rangle-\langle H\rangle^2$, when affordable;
- sensitivity to initial parameters;
- circuit depth and two-qubit gate count;
- number of Pauli measurement groups rather than only raw term count.

The AI interpreter should not label a run “converged” from a flat optimizer
curve alone. A flat curve far above the exact reduced-Hamiltonian energy may be
an expressibility failure or local minimum.

## 7. Verification gates

Before enabling `molecule="beh2"`:

1. Generate the operator reproducibly with pinned PySCF/Qiskit Nature inputs.
2. Record all scalar shifts and whether nuclear repulsion is included.
3. Persist the full-precision Pauli list and content hash.
4. Independently reconstruct and diagonalize the matrix.
5. Compare with a classical active-space reference calculation.
6. Verify the tapered Hartree–Fock state and Pauli bit ordering.
7. Benchmark the generic ansatz for expressibility.
8. Add commuting-term grouping or document the expected circuit count.
9. Define molecule-specific convergence/error thresholds for interpretation.

## Sources

- A. Kandala et al., [Hardware-efficient variational quantum eigensolver for small molecules and quantum magnets](https://doi.org/10.1038/nature23879), Nature 549, 242–246 (2017).
- [PySCF molecular input, basis, and integral documentation](https://pyscf.org/user/gto.html).
- [Qiskit Nature electronic-structure tutorials](https://qiskit-community.github.io/qiskit-nature/tutorials/01_electronic_structure.html).
- J. Tilly et al., [The Variational Quantum Eigensolver: a review of methods and best practices](https://doi.org/10.1016/j.physrep.2022.08.003), Physics Reports 986, 1–128 (2022).
