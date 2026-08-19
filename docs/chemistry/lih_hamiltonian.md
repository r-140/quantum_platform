# LiH molecular Hamiltonian

This document defines the physics and reproducibility contract for extending
the VQE implementation from H₂ to lithium hydride. Numerical Pauli
coefficients are intentionally not listed yet: they must be generated from a
committed specification and verified before `lih` is enabled in the API.

## 1. Electronic structure

Neutral LiH contains four electrons. In the Born–Oppenheimer approximation,

\[
\hat H_\mathrm{elec}=
\sum_i\left(-\frac12\nabla_i^2
-\frac{3}{|\mathbf r_i-\mathbf R_\mathrm{Li}|}
-\frac{1}{|\mathbf r_i-\mathbf R_\mathrm H|}\right)
+\sum_{i<j}\frac1{r_{ij}},
\]

and

\[
E_\mathrm{nuc}=\frac{3}{R_\mathrm{LiH}}.
\]

Compared with H₂, LiH introduces a chemically inert, tightly bound Li 1s core
pair and valence orbitals with unequal atomic energies. The bond is polar; the
ground state has appreciable ionic character, often described schematically as
\(\mathrm{Li}^{\delta+}\mathrm H^{\delta-}\).

## 2. Basis and active space

In STO-3G, Li contributes basis functions corresponding roughly to 1s, 2s,
and 2p character, while H contributes 1s. Without reduction this produces
more spin orbitals—and therefore more qubits—than the two-qubit H₂ model.

The natural first reduction is frozen core:

- keep the doubly occupied Li 1s orbital frozen;
- add its mean-field interaction with active electrons to the effective
  one-electron integrals;
- remove two electrons and one spatial orbital from the correlated problem.

An additional active-space selection may retain only the valence orbitals most
important for bond formation and low-energy correlation. This is a physical
model choice, not merely a circuit optimization. Different active spaces give
different Hamiltonians and different reference energies.

## 3. Second-quantized Hamiltonian

After a restricted Hartree–Fock calculation and transformation from atomic to
molecular orbitals,

\[
\hat H_\mathrm{active}=
E_\mathrm{inactive}
+\sum_{pq\in A}\tilde h_{pq}a_p^\dagger a_q
+\frac12\sum_{pqrs\in A}h_{pqrs}
a_p^\dagger a_q^\dagger a_s a_r,
\]

where (A\) is the selected active orbital set. (E_\mathrm{inactive}\)
contains the nuclear repulsion and frozen-core contribution according to the
chosen library convention. The implementation must record whether this scalar
is already included in the qubit operator to prevent double counting.

## 4. Qubit mapping

The fermionic operator is mapped to

\[
\hat H_q=\sum_j c_jP_j,
\qquad P_j\in\{I,X,Y,Z\}^{\otimes n}.
\]

The first implementation should use one explicitly versioned pipeline—for
example parity or Bravyi–Kitaev mapping followed by (\mathbb Z_2\) symmetry
tapering. Particle-number parity and spin parity can often remove qubits, but
the tapering eigenvalues must be derived from the Hartree–Fock reference state,
not guessed.

LiH therefore must not be represented by a hand-labelled `num_qubits` plus a
coefficient list with unspecified provenance. The geometry, active space,
mapper, orbital ordering, tapering sector, and library versions together define
the operator.

## 5. Proposed reproducible model

The following is a starting proposal and must be confirmed by the generated
artifact before becoming a project constant:

| Setting | Proposed value |
|---|---|
| Geometry | Li–H, (R=1.6\,\text{Å}) |
| Charge / multiplicity | 0 / singlet |
| Orbital basis | STO-3G |
| Mean field | RHF |
| Frozen core | Li 1s pair |
| Active space | explicitly selected valence orbitals; record indices |
| Mapping | parity or Bravyi–Kitaev; choose one and freeze it |
| Reduction | (\mathbb Z_2\) tapering with recorded sector |

The bond length is a convenient near-equilibrium demonstration point, not a
claim that every LiH reference uses exactly this geometry.

## 6. Ansatz implications

The generic two-RY-layer ansatz now scales its parameter count as (2n\), but
syntactic scaling does not prove chemical expressibility. LiH verification
must compare at least:

- exact diagonalization of the reduced qubit Hamiltonian;
- the hardware-efficient ansatz under noiseless statevector expectation;
- shot-based evaluation through the real measurement pipeline;
- optionally a particle-number-preserving ansatz such as UCCSD or a reduced
  excitation ansatz.

Starting from all-zero parameters also starts from
\(|0\ldots0\rangle\), not necessarily the Hartree–Fock occupation state. The
larger-molecule implementation should explicitly prepare the tapered
Hartree–Fock reference before applying variational layers.

## 7. Verification gates

Before enabling `molecule="lih"`:

1. Commit a generator script using fixed PySCF/Qiskit Nature versions.
2. Serialize ordered Pauli labels and full-precision coefficients.
3. Store geometry, basis, active orbital indices, mapping, tapering sector,
   scalar shifts, and a content hash.
4. Build the dense/sparse matrix independently from the serialized Pauli list.
5. Compare its lowest eigenvalue with an independent classical eigensolver.
6. Verify every measurement basis and Qiskit bit-order convention.
7. Establish an expected VQE tolerance for both noiseless and shot-based runs.

## Sources

- A. Kandala et al., [Hardware-efficient variational quantum eigensolver for small molecules and quantum magnets](https://doi.org/10.1038/nature23879), Nature 549, 242–246 (2017).
- [PySCF molecular input, basis, and integral documentation](https://pyscf.org/user/gto.html).
- [Qiskit Nature electronic-structure tutorials](https://qiskit-community.github.io/qiskit-nature/tutorials/01_electronic_structure.html).
- J. Tilly et al., [The Variational Quantum Eigensolver: a review of methods and best practices](https://doi.org/10.1016/j.physrep.2022.08.003), Physics Reports 986, 1–128 (2022).
