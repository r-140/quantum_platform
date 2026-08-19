from app.canonical import canonical_experiment_text


def test_vqe_projection_contains_semantic_fields_but_not_identity() -> None:
    text = canonical_experiment_text(
        {
            "id": "random-uuid-that-must-not-affect-similarity",
            "algorithm": "vqe",
            "status": "completed",
            "parameters": {"molecule": "lih", "shots": 256, "max_iterations": 3},
            "result": {
                "energy": -7.86,
                "iterations": 3,
                "history": [{"energy": -7.1}, {"energy": -7.8}, {"energy": -7.86}],
            },
        }
    )

    assert "Molecule: lih" in text
    assert "Best energy (Ha): -7.86" in text
    assert "Energy trajectory (Ha): [-7.1,-7.8,-7.86]" in text
    assert "random-uuid" not in text


def test_dictionary_output_is_stable() -> None:
    base = {
        "algorithm": "grover",
        "status": "completed",
        "parameters": {"shots": 100, "marked_states": ["11"]},
    }
    left = canonical_experiment_text({**base, "result": {"counts": {"11": 90, "00": 10}}})
    right = canonical_experiment_text({**base, "result": {"counts": {"00": 10, "11": 90}}})
    assert left == right
