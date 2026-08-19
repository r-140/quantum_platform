"""Validated molecular Hamiltonian data and molecule lookup.

Molecules are immutable data objects rather than subclasses containing only
different constants.  Algorithm behavior (the ansatz) is kept behind its own
strategy boundary in :mod:`quantum_core.algorithms.vqe`.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from functools import lru_cache
from types import MappingProxyType
from typing import Mapping


@dataclass(frozen=True)
class PauliTerm:
    """One coefficient times a tensor product of Pauli operators."""

    coefficient: float
    qubits: Mapping[int, str]

    def __post_init__(self) -> None:
        normalized = dict(self.qubits)
        if any(qubit < 0 for qubit in normalized):
            raise ValueError("qubit indexes must be non-negative")
        unknown = set(normalized.values()) - {"X", "Y", "Z"}
        if unknown:
            raise ValueError(f"unknown Pauli operators: {sorted(unknown)}")
        object.__setattr__(self, "qubits", MappingProxyType(normalized))


@dataclass(frozen=True)
class MolecularHamiltonian:
    """All stable inputs needed to execute and interpret a molecular VQE."""

    name: str
    geometry: str
    mapping: str
    num_qubits: int
    terms: tuple[PauliTerm, ...]
    nuclear_repulsion: float
    inactive_energy: float = 0.0
    initial_state: tuple[bool, ...] = ()
    reference_ground_energy: float | None = None
    source: str | None = None

    def __post_init__(self) -> None:
        if self.num_qubits < 1:
            raise ValueError("num_qubits must be positive")
        if not self.terms:
            raise ValueError("a molecular Hamiltonian needs at least one term")
        for term in self.terms:
            if term.qubits and max(term.qubits) >= self.num_qubits:
                raise ValueError(
                    f"term references qubit {max(term.qubits)}, but {self.name} "
                    f"has only {self.num_qubits} qubits"
                )
        if self.initial_state and len(self.initial_state) != self.num_qubits:
            raise ValueError("initial_state length must equal num_qubits")

    @property
    def total_energy_offset(self) -> float:
        """Scalar excluded from the measured active-space qubit operator."""

        return self.nuclear_repulsion + self.inactive_energy


class MoleculeName(str, Enum):
    H2 = "h2"
    LIH = "lih"
    BEH2 = "beh2"


H2 = MolecularHamiltonian(
    name=MoleculeName.H2.value,
    geometry="H-H, R=0.75 Angstrom",
    mapping="Bravyi-Kitaev, symmetry-reduced",
    num_qubits=2,
    terms=(
        PauliTerm(-0.4804, {}),
        PauliTerm(0.3435, {0: "Z"}),
        PauliTerm(-0.4347, {1: "Z"}),
        PauliTerm(0.5716, {0: "Z", 1: "Z"}),
        PauliTerm(0.0910, {0: "Y", 1: "Y"}),
        PauliTerm(0.0910, {0: "X", 1: "X"}),
    ),
    nuclear_repulsion=0.7055696146,
    reference_ground_energy=-1.137,
    source="O'Malley et al., Phys. Rev. X 6, 031007 (2016), Table 1",
)

_MOLECULES: dict[MoleculeName, MolecularHamiltonian] = {MoleculeName.H2: H2}

_GENERATED_MODELS = {
    MoleculeName.LIH: {
        "atom": "Li 0 0 0; H 0 0 1.6",
        "geometry": "Li-H, R=1.6 Angstrom",
        "num_electrons": 2,
        "num_spatial_orbitals": 3,
    },
    MoleculeName.BEH2: {
        "atom": "H 0 0 -1.3; Be 0 0 0; H 0 0 1.3",
        "geometry": "linear H-Be-H, R(Be-H)=1.3 Angstrom",
        "num_electrons": 4,
        "num_spatial_orbitals": 4,
    },
}


@lru_cache(maxsize=None)
def _generate_molecule(name: MoleculeName) -> MolecularHamiltonian:
    """Generate and cache a compact active-space Hamiltonian.

    PySCF performs RHF/integral generation. Qiskit Nature applies the
    active-space transform and parity mapping with its two-qubit reduction.
    Imports are local so H2 remains usable without the optional chemistry
    toolchain.
    """

    try:
        import numpy as np
        from qiskit_nature.second_q.circuit.library.initial_states.hartree_fock import (
            hartree_fock_bitstring_mapped,
        )
        from qiskit_nature.second_q.drivers import PySCFDriver
        from qiskit_nature.second_q.mappers import ParityMapper
        from qiskit_nature.second_q.transformers import ActiveSpaceTransformer
    except ImportError as exc:
        raise RuntimeError(
            f"{name.value} requires qiskit-nature and pyscf; reinstall "
            "quantum-core dependencies before running this molecule"
        ) from exc

    config = _GENERATED_MODELS[name]
    full_problem = PySCFDriver(
        atom=config["atom"], basis="sto3g", charge=0, spin=0
    ).run()
    problem = ActiveSpaceTransformer(
        num_electrons=config["num_electrons"],
        num_spatial_orbitals=config["num_spatial_orbitals"],
    ).transform(full_problem)
    mapper = ParityMapper(num_particles=problem.num_particles)
    qubit_operator = mapper.map(problem.hamiltonian.second_q_op()).simplify()

    terms: list[PauliTerm] = []
    for label, raw_coefficient in qubit_operator.label_iter():
        coefficient = complex(raw_coefficient)
        if abs(coefficient.imag) > 1e-10:
            raise ValueError(f"non-real coefficient for {label}: {coefficient}")
        qubits = {
            qubit_operator.num_qubits - 1 - index: pauli
            for index, pauli in enumerate(label)
            if pauli != "I"
        }
        terms.append(PauliTerm(float(coefficient.real), qubits))

    nuclear_repulsion = float(problem.nuclear_repulsion_energy or 0.0)
    inactive_energy = float(
        sum(
            value
            for key, value in problem.hamiltonian.constants.items()
            if key != "nuclear_repulsion_energy"
        )
    )
    reference_ground_energy = float(
        np.linalg.eigvalsh(qubit_operator.to_matrix())[0].real
        + nuclear_repulsion
        + inactive_energy
    )
    initial_state = tuple(
        bool(bit)
        for bit in hartree_fock_bitstring_mapped(
            problem.num_spatial_orbitals, problem.num_particles, mapper
        )
    )

    return MolecularHamiltonian(
        name=name.value,
        geometry=config["geometry"],
        mapping="STO-3G/RHF active space; parity mapping with two-qubit reduction",
        num_qubits=qubit_operator.num_qubits,
        terms=tuple(terms),
        nuclear_repulsion=nuclear_repulsion,
        inactive_energy=inactive_energy,
        initial_state=initial_state,
        reference_ground_energy=reference_ground_energy,
        source="Generated by pinned PySCF and Qiskit Nature configuration",
    )


def get_molecule(name: str | MoleculeName) -> MolecularHamiltonian:
    """Return a registered molecule, with a clear error for planned models."""

    try:
        molecule_name = MoleculeName(name)
    except ValueError as exc:
        supported = ", ".join(item.value for item in MoleculeName)
        raise ValueError(f"unknown molecule {name!r}; expected one of: {supported}") from exc

    if molecule_name in _MOLECULES:
        return _MOLECULES[molecule_name]
    return _generate_molecule(molecule_name)
