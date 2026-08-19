# H₂ molecular Hamiltonian

This document explains how the non-relativistic molecular problem becomes the
two-qubit Pauli Hamiltonian used by this project. Energies are in Hartree and
the internuclear distance is $R=0.75\,\text{Å}$, unless stated otherwise.

## 1. Molecular Schrödinger equation

For two nuclei and two electrons, neglecting relativistic and radiative
effects, the laboratory-frame Hamiltonian is

$$
\hat H =
-\sum_A \frac{\nabla_A^2}{2M_A}
-\sum_i \frac{\nabla_i^2}{2}
-\sum_{iA}\frac{Z_A}{|\mathbf r_i-\mathbf R_A|}
+\sum_{i \lt j}\frac{1}{r_{ij}}
+\sum_{A \lt B}\frac{Z_AZ_B}{R_{AB}}.
$$

The Born–Oppenheimer approximation fixes the nuclei at positions
$\mathbf R_A$. Their kinetic term disappears, and the nuclear repulsion is a
geometry-dependent scalar:

$$
E_\mathrm{nuc}(R)=\frac{Z_1Z_2}{R}.
$$

The electronic eigenproblem is therefore

$$
\hat H_\mathrm{elec}(R)|\Psi_k(R)\rangle
=E_{k,\mathrm{elec}}(R)|\Psi_k(R)\rangle,
\qquad
E_k(R)=E_{k,\mathrm{elec}}(R)+E_\mathrm{nuc}(R).
$$

For the model used here, $E_\mathrm{nuc}=0.7055696146$ Ha. Keeping this
constant separate is important: the measured qubit Hamiltonian estimates the
electronic energy, while the reported molecular energy adds nuclear repulsion.

## 2. Finite one-particle basis

The exact electronic wavefunction lives in an infinite-dimensional Hilbert
space. A quantum-chemistry calculation first chooses spatial basis functions
and constructs molecular spin orbitals. In a minimal basis, H₂ has two spatial
molecular orbitals, bonding $\sigma_g$ and antibonding $\sigma_u^*$, each
with spin $\alpha$ or $\beta$: four spin orbitals in total.

After choosing orthonormal spin orbitals $\{\chi_p\}$, the electronic
Hamiltonian is written in second quantization:

$$
\hat H_\mathrm{elec}
=\sum_{pq}h_{pq}a_p^\dagger a_q
+\frac12\sum_{pqrs}h_{pqrs}a_p^\dagger a_q^\dagger a_s a_r.
$$

The one-electron integrals contain kinetic energy and electron–nuclear
attraction,

$$
h_{pq}=\int \chi_p^*(x)
\left(-\frac12\nabla^2-\sum_A\frac{Z_A}{r_A}\right)
\chi_q(x)\,dx,
$$

and the two-electron integrals contain Coulomb repulsion,

$$
h_{pqrs}=\iint
\frac{\chi_p^*(x_1)\chi_q^*(x_2)
\chi_r(x_1)\chi_s(x_2)}{r_{12}}\,dx_1dx_2.
$$

This basis truncation is the first major approximation. A VQE can solve the
chosen finite-basis Hamiltonian accurately while still differing from the
complete-basis physical energy.

## 3. Fermions to qubits

Occupation of a spin orbital is binary, but fermionic creation/annihilation
operators anticommute. A fermion-to-qubit mapping represents both occupation
and parity information with Pauli operators. The coefficients used here come
from the Bravyi–Kitaev mapping followed by symmetry reduction.

Particle-number and spin/parity symmetries restrict physical states to a small
sector. Starting from four spin orbitals, two qubits can be tapered away for
this H₂ problem, leaving an effective two-qubit Hamiltonian.

Mapping and tapering are exact within the chosen orbital basis and symmetry
sector; basis truncation and rounded coefficients are not.

## 4. Pauli Hamiltonian used by the project

The reduced electronic Hamiltonian is

$$
\hat H_\mathrm{elec}=
g_0 I+g_1Z_0+g_2Z_1+g_3Z_0Z_1+g_4Y_0Y_1+g_5X_0X_1,
$$

with

| coefficient | value (Ha) | operator |
|---|---:|---|
| $g_0$ | -0.4804 | $I$ |
| $g_1$ | +0.3435 | $Z_0$ |
| $g_2$ | -0.4347 | $Z_1$ |
| $g_3$ | +0.5716 | $Z_0Z_1$ |
| $g_4$ | +0.0910 | $Y_0Y_1$ |
| $g_5$ | +0.0910 | $X_0X_1$ |

The numbers are from O’Malley et al., *Scalable Quantum Simulation of
Molecular Energies*, Phys. Rev. X 6, 031007 (2016), Table I. They are rounded
to four decimal places in the source/project.

Using

$$
X=\begin{pmatrix}0&1\\1&0\end{pmatrix},\quad
Y=\begin{pmatrix}0&-i\\i&0\end{pmatrix},\quad
Z=\begin{pmatrix}1&0\\0&-1\end{pmatrix},
$$

the complete $4\times4$ matrix is formed with Kronecker products. Direct
diagonalization gives electronic eigenvalues approximately

```text
-1.851199, -0.252801, 0.000000, 0.182400 Ha
```

and the lowest total energy is

$$
-1.851199+0.7055696146=-1.145630\ \mathrm{Ha}.
$$

The cited physical value is about $-1.137$ Ha. The residual is consistent
with the deliberately low-precision coefficient table and small geometry/model
differences; this model is not a high-accuracy ab-initio calculation.

## 5. Why Pauli terms are measurable

For a trial state $|\psi(\theta)\rangle$, VQE evaluates

$$
E(\theta)=\sum_jg_j\langle\psi(\theta)|P_j|\psi(\theta)\rangle.
$$

Hardware measures in the computational, or Z, basis. Each Pauli product is
converted to that basis before measurement:

- $Z$: no rotation;
- $X$: apply $H$;
- $Y$: apply $S^\dagger$, then $H$.

For a measured bitstring, the eigenvalue of a Pauli product is the product of
$(-1)^{b_q}$ over qubits on which the term acts. Averaging this sign over
shots estimates $\langle P_j\rangle$. The identity contributes $g_0$
without a circuit.

The project verified the basis rotations and sign convention independently by
comparing measurement-derived expectations with direct
$\langle\psi|H|\psi\rangle$ matrix evaluation for random parameters.

## 6. Ansatz and variational space

The current hardware-efficient ansatz is

```text
RY(theta_0) q0       RY(theta_1) q1
CX(control=q1, target=q0)
RY(theta_2) q0       RY(theta_3) q1
```

It is not particle-number preserving and its parameters do not have the
cluster-amplitude interpretation of UCC. Its advantage is shallow depth. For
this two-qubit Hamiltonian it was independently shown to reach the exact matrix
ground state to numerical precision under noiseless classical optimization.

This empirical expressibility result does not automatically extend to LiH or
BeH₂. A two-layer hardware-efficient circuit scales syntactically with qubit
count, but may be too shallow or suffer barren-plateau/optimization problems.

## 7. Reproducibility metadata

A molecular energy has no unique meaning unless the result identifies:

- geometry and units;
- charge and spin multiplicity;
- orbital basis;
- frozen-core and active-space choices;
- fermion-to-qubit mapping and tapering sector;
- coefficient precision;
- whether nuclear repulsion is included.

The molecule model and VQE result payload should carry these fields, plus a
content hash of the ordered Pauli-term list.

## Sources

- P. J. J. O’Malley et al., [Scalable Quantum Simulation of Molecular Energies](https://doi.org/10.1103/PhysRevX.6.031007), Phys. Rev. X 6, 031007 (2016).
- S. Bravyi and A. Kitaev, [Fermionic quantum computation](https://doi.org/10.1006/aphy.2002.6254), Annals of Physics 298, 210–226 (2002).
- A. Kandala et al., [Hardware-efficient variational quantum eigensolver for small molecules and quantum magnets](https://doi.org/10.1038/nature23879), Nature 549, 242–246 (2017).
- [PySCF molecular structure and basis documentation](https://pyscf.org/user/gto.html).
