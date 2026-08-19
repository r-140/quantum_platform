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


def test_unverified_planned_molecule_fails_clearly() -> None:
    with pytest.raises(ValueError, match="not been registered"):
        get_molecule("lih")
