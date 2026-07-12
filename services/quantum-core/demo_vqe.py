"""
Runs the full VQE feedback loop against AerBackend to find the ground-state
energy of H2 (bond length 0.75 Angstrom). Each optimizer iteration does a
real classical-quantum round trip: build circuit -> submit to backend ->
wait_for_result (with the project's standard retry/backoff/circuit-breaker
machinery) -> measure -> compute expectation -> feed back to COBYLA.

Called as a plain synchronous script deliberately -- see
quantum_core/loops/vqe_loop.py's module docstring for why `run_vqe` must
NOT be wrapped in `asyncio.run(main())` the way other demos in this project
are.

Run with:  python demo_vqe.py
"""

from __future__ import annotations

from quantum_core.algorithms.vqe import H2_LITERATURE_GROUND_ENERGY, H2_NUCLEAR_REPULSION
from quantum_core.backends.aer_backend import AerBackend
from quantum_core.loops.vqe_loop import run_vqe


def main() -> None:
    backend = AerBackend(seed_simulator=42)

    print("Running VQE for H2 (R=0.75 A)...")
    print("Each iteration submits up to 5 circuits (one per non-identity Hamiltonian term)")
    print("through the full hw/sw synchronization loop -- this will take a little while.\n")

    result = run_vqe(backend, shots=8192, max_iterations=80)

    print(f"iterations run: {len(result.history)}")
    print(f"optimal parameters: {[round(p, 4) for p in result.optimal_params]}")
    print(f"\nelectronic energy:  {result.electronic_energy:.6f} Hartree")
    print(f"+ nuclear repulsion ({H2_NUCLEAR_REPULSION:.6f}):")
    print(f"total ground energy: {result.total_energy:.6f} Hartree")
    print(f"\nliterature reference (approx.): {H2_LITERATURE_GROUND_ENERGY} Hartree")
    print(f"difference: {abs(result.total_energy - H2_LITERATURE_GROUND_ENERGY):.4f} Hartree")

    print("\nconvergence (every 10th iteration):")
    for i, log in enumerate(result.history):
        if i % 10 == 0 or i == len(result.history) - 1:
            print(f"  iter {i:3d}: energy={log.energy:.6f} Hartree")


if __name__ == "__main__":
    main()