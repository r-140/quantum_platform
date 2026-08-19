"""Validated molecular Hamiltonian data and molecule lookup.

Molecules are immutable data objects rather than subclasses containing only
different constants.  Algorithm behavior (the ansatz) is kept behind its own
strategy boundary in :mod:`quantum_core.algorithms.vqe`.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
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


def get_molecule(name: str | MoleculeName) -> MolecularHamiltonian:
    """Return a registered molecule, with a clear error for planned models."""

    try:
        molecule_name = MoleculeName(name)
    except ValueError as exc:
        supported = ", ".join(item.value for item in MoleculeName)
        raise ValueError(f"unknown molecule {name!r}; expected one of: {supported}") from exc

    try:
        return _MOLECULES[molecule_name]
    except KeyError as exc:
        raise ValueError(
            f"molecule {molecule_name.value!r} is recognized but its verified "
            "Hamiltonian has not been registered yet"
        ) from exc
