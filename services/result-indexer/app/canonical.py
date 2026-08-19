"""Stable, algorithm-aware text projections used as embedding input."""

from __future__ import annotations

import json
from typing import Any


def _line(label: str, value: Any) -> str:
    if isinstance(value, (dict, list)):
        value = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return f"{label}: {value}"


def canonical_experiment_text(experiment: dict[str, Any]) -> str:
    algorithm = experiment["algorithm"]
    parameters = experiment.get("parameters") or {}
    result = experiment.get("result") or {}
    lines = [
        _line("Algorithm", algorithm),
        _line("Status", experiment.get("status", "unknown")),
    ]

    if algorithm == "vqe":
        molecule = parameters.get("molecule") or result.get("molecule") or "h2"
        lines.extend(
            [
                _line("Molecule", molecule),
                _line("Shots", parameters.get("shots")),
                _line("Maximum optimizer iterations", parameters.get("max_iterations")),
                _line("Energy (Ha)", result.get("energy")),
                _line("Optimizer iterations", result.get("iterations")),
                _line("Converged", result.get("converged")),
            ]
        )
        history = result.get("history") or []
        energies = [item.get("energy") for item in history if item.get("energy") is not None]
        if energies:
            lines.extend(
                [
                    _line("Initial energy (Ha)", energies[0]),
                    _line("Best energy (Ha)", min(energies)),
                    _line("Energy trajectory (Ha)", energies),
                ]
            )
    elif algorithm in {"grover", "sat_grover"}:
        lines.extend(
            [
                _line("Shots", parameters.get("shots")),
                _line("Marked states", parameters.get("marked_states")),
                _line("Boolean expression", parameters.get("expression")),
                _line("Measurement counts", result.get("counts")),
            ]
        )
    elif algorithm == "qpe":
        lines.extend(
            [
                _line("True phase", parameters.get("phi")),
                _line("Counting qubits", parameters.get("num_counting_qubits")),
                _line("Shots", parameters.get("shots")),
                _line("Estimated phase", result.get("estimated_phase")),
                _line("Measurement counts", result.get("counts")),
            ]
        )
    else:
        lines.extend([_line("Parameters", parameters), _line("Result", result)])

    if experiment.get("error"):
        lines.append(_line("Error", experiment["error"]))
    return "\n".join(line for line in lines if not line.endswith(": None"))
