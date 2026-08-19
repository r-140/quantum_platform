from __future__ import annotations

import pytest

from quantum_core.algorithms.vqe import DEFAULT_ANSATZ
from quantum_core.chemistry.molecules import H2, MolecularHamiltonian, PauliTerm, get_molecule


def test_h2_registry_preserves_existing_model() -> None:
    assert get_molecule("h2") is H2
    assert H2.num_qubits == 2
    assert len(H2.terms) == 6
    assert DEFAULT_ANSATZ.parameter_count(H2.num_qubits) == 4


def test_generic_ansatz_scales_with_qubit_count() -> None:
    circuit = DEFAULT_ANSATZ.build(4, [0.0] * 8)

    assert circuit.num_qubits == 4
    assert circuit.count_ops()["ry"] == 8
    assert circuit.count_ops()["cx"] == 3


def test_molecule_rejects_term_outside_register() -> None:
    with pytest.raises(ValueError, match="has only 2 qubits"):
        MolecularHamiltonian(
            name="invalid",
            geometry="n/a",
            mapping="n/a",
            num_qubits=2,
            terms=(PauliTerm(1.0, {2: "Z"}),),
            nuclear_repulsion=0.0,
        )


def test_unknown_molecule_fails_clearly() -> None:
    with pytest.raises(ValueError, match="unknown molecule"):
        get_molecule("water")


@pytest.mark.parametrize(
    ("name", "expected_qubits"),
    [("lih", 4), ("beh2", 6)],
)
def test_generated_molecule_is_finite_and_self_consistent(
    name: str, expected_qubits: int
) -> None:
    molecule = get_molecule(name)

    assert molecule.num_qubits == expected_qubits
    assert len(molecule.terms) > 6
    assert len(molecule.initial_state) == expected_qubits
    assert molecule.reference_ground_energy is not None
    assert any(not term.qubits for term in molecule.terms)
    assert molecule.total_energy_offset == pytest.approx(
        molecule.nuclear_repulsion + molecule.inactive_energy
    )
