"""
Demonstrates Quantum Phase Estimation: recovers the eigenphase of a known
phase gate P(theta) using its |1> eigenstate.

theta is chosen so phi=5/8 is exactly representable with 3 counting
qubits -- QPE should return '101' (=5) with probability close to 1.
Pass --inexact to see the more realistic case where phi does NOT land
exactly on a 3-bit fraction: the result spreads across neighboring values
instead of a single sharp peak, which is expected QPE behavior, not a bug.

Run with:  python demo_qpe.py
       or: python demo_qpe.py --inexact

       Как читать результат
phi_est = k / 2^t, где k — измеренная битовая строка, переведённая в число, t=3 — число counting-кубит (отсюда разрешение 1/2³ = 1/8, то есть QPE в принципе не может различить значения φ, отличающиеся меньше чем на 0.125).

Точный случай (φ=5/8): 5/8 ровно попадает в сетку из 8 возможных значений (k/8 для k=0..7), поэтому результат детерминированный — 101=5, 5/8=0.625 ✓, 100% вероятность.
Неточный случай (φ=0.3): 0.3 не попадает ни в одно k/8 точно. Ближайшие соседи — k=2→0.25 (расстояние 0.05) и k=3→0.375 (расстояние 0.075). Именно поэтому вероятность распределилась в основном между ними (573+279=852 из 1024, то есть 83%), а не сконцентрировалась в одной точке. Чем ближе истинное φ к узлу сетки k/8, тем острее пик; чем дальше — тем сильнее размазывание. Здесь φ=0.3 ближе к 0.25, поэтому 010 доминирует над 011.

Что можно прогнать дальше (правь константы в demo_qpe.py)

Увеличить точность — добавь counting-кубитов:

pythont = 5   # разрешение 1/32 вместо 1/8
С φ=0.3 результат станет заметно резче — она ближе к k=10/32=0.3125, чем к соседям на сетке 1/8.

Фаза точно на новой, более мелкой сетке:

pythont = 5
true_phi = 7 / 32   # точно представимо в 5 битах -> снова 100% детерминированный результат

Фаза ровно посередине между двумя соседями — самый "жёсткий" случай для QPE, интересно увидеть максимальное размазывание:

pythontrue_phi = 0.3125 + 1/16   # т.е. 0.375 + 1/16 = между k=3 и k=4 при t=3...
Проще: true_phi = (5/8 + 6/8) / 2 = 0.6875 при t=3 — результат должен распределиться примерно поровну между 101 (5) и 110 (6).

Другой унитарный оператор — сейчас всегда PhaseGate. Можно попробовать, например, RZGate(theta) — там собственные состояния тоже |0⟩/|1⟩, но с другой (со сдвигом на глобальную фазу) структурой — хороший повод убедиться, что controlled_power_gate работает не только для PhaseGate:

pythonfrom qiskit.circuit.library import RZGate
unitary = RZGate(theta)
(здесь придётся аккуратно проверить, какое из двух базисных состояний — собственное с нужной фазой, у RZ обе |0⟩ и |1⟩ — собственные состояния, но с разным знаком фазы).

"Плохое" (неточное) собственное состояние — специально нарушить условие eigenstate_prep, чтобы увидеть деградацию:

pythoneigenstate_prep = QuantumCircuit(1)
eigenstate_prep.ry(0.3, 0)   # вместо чистого |1>, суперпозиция |0>/|1>
Ожидай результат размазанным между двумя пиками — вокруг фазы для |1⟩-компоненты и вокруг 0 (фаза для |0⟩-компоненты, которая для PhaseGate равна 0). Это хорошая демонстрация того, что "мусор на входе — мусор на выходе": QPE так же чувствителен к качеству подготовки собственного состояния, как VQE — к качеству ansatz.
"""

from __future__ import annotations

import asyncio
import math
import sys

from qiskit import QuantumCircuit
from qiskit.circuit.library import PhaseGate

from quantum_core.algorithms.qpe import build_qpe_circuit
from quantum_core.backends.aer_backend import AerBackend
from quantum_core.backends.base import Circuit
from quantum_core.sync.polling import PollingConfig, wait_for_result


async def main(true_phi: float) -> None:
    t = 3
    theta = 2 * math.pi * true_phi

    unitary = PhaseGate(theta)
    eigenstate_prep = QuantumCircuit(1)
    eigenstate_prep.x(0)  # |1> is an exact eigenstate of any diagonal phase gate

    qc = build_qpe_circuit(unitary, num_counting_qubits=t, eigenstate_prep=eigenstate_prep)

    backend = AerBackend(seed_simulator=3)
    circuit = Circuit(name="qpe-phase-gate", num_qubits=qc.num_qubits, payload=qc, shots=1024)
    handle = await backend.submit(circuit)
    result = await wait_for_result(backend, handle, config=PollingConfig(timeout_s=15.0))

    print(f"true phi = {true_phi} (theta={theta:.4f}), {t} counting qubits -> resolution 1/{2**t}")
    assert result.counts is not None
    for bitstring, count in sorted(result.counts.items(), key=lambda kv: -kv[1]):
        k = int(bitstring, 2)
        phi_est = k / (2 ** t)
        marker = " <-- exact match" if abs(phi_est - true_phi) < 1e-9 else ""
        print(f"  {bitstring} (phi_est={phi_est:.4f}): {count}{marker}")


if __name__ == "__main__":
    phi = 0.3 if "--inexact" in sys.argv else 5 / 8
    asyncio.run(main(phi))