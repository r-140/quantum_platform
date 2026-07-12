"""
Solves a small 3-SAT-style boolean constraint with Grover search -- the
answer is not hard-coded anywhere; only the constraint (`EXPRESSION` below)
is known in advance, matching a realistic Grover use case.

Classical brute-force counting is used only to pick the optimal iteration
count for this small demo (4 variables => 16 states, cheap to enumerate).
See `BooleanSearchProblem.count_solutions` docstring for why that
classical step wouldn't scale to a real use case.

Run with:  python demo_sat_grover.py

Мы ищем все комбинации значений четырёх булевых переменных x0, x1, x2, x3 (каждая — 0 или 1), при которых выражение целиком становится True. Это классическая формулировка задачи SAT (satisfiability, "задача выполнимости") — стандартная форма для описания задач вроде "распределить ресурсы так, чтобы не было конфликтов" или "найти конфигурацию, удовлетворяющую набору ограничений".
Что означает конкретное выражение
(x0 | x1) & (~x1 | x2) & (x0 | ~x3)
Это конъюнкция (& = И) трёх "клозов" (| = ИЛИ внутри скобок, ~ = НЕ):

(x0 | x1) — хотя бы одна из x0, x1 должна быть 1
(~x1 | x2) — если x1=1, то обязательно x2=1 (логическая импликация x1→x2)
(x0 | ~x3) — если x3=1, то обязательно x0=1 (импликация x3→x0)

Это классическая CNF-запись (Conjunctive Normal Form) — именно так формулируются реальные SAT-инстансы (в криптоанализе, планировании, верификации схем).
Как интерпретировать результат
Измеренная строка, например 0110 — это не одно число, а присвоение всем четырём переменным сразу. Порядок фиксированный (совпадает с конвенцией Qiskit): крайний правый символ = x0, следующий = x1, следующий = x2, крайний левый = x3.
0110 → читаем справа налево: x0=0, x1=1, x2=1, x3=0.
Подставляем в выражение: (0|1) & (~1|1) & (0|~0) = 1 & 1 & 1 = True ✓ — совпадает с тем, что скрипт пометил как <-- valid solution.
Почему усиление скромное (88-105 против 40-45), а не как в первом демо (954 из 1024)
Потому что здесь 7 решений из 16 — почти половина пространства. Grover эффективнее всего, когда решений мало относительно N — тогда усиление резкое. При M≈N/2 амплификация слабая по конструкции формулы, это не баг.
Другие выражения, которые стоит прогнать
Поменяй EXPRESSION (и при необходимости VARIABLES) в начале demo_sat_grover.py:

Одно решение — самый эффектный результат, аналог первого демо, но без хардкода ответа:

pythonEXPRESSION = "x0 & x1 & x2 & x3"
Единственное решение 1111. Ожидай результат, близкий к первому демо (~90%+ на одном исходе).

XOR / чётность — ровно половина решений, интересно посмотреть на поведение при M=N/2:

pythonEXPRESSION = "x0 ^ x1 ^ x2 ^ x3"
(True когда нечётное число единиц — 8 решений из 16).

Почти всё пространство — решение (близко к вырожденному случаю, где Grover почти не помогает, т.к. M≈N):

pythonEXPRESSION = "x0 | x1 | x2 | x3"
15 из 16 (не решение только 0000). Полезно увидеть, что optimal_iterations в этом случае даст малое число итераций (или даже 0-1) — усиление тут не нужно, почти всё и так решение.

"Похоже на реальный запрос к БД" — совпадение по нескольким полям сразу:

pythonEXPRESSION = "(x0 & x1) & (~x2 & x3)"
Одно решение (x0=1,x1=1,x2=0,x3=1) — как будто ищем запись, где два флага включены, а один выключен.
"""

from __future__ import annotations

import asyncio

from quantum_core.algorithms.grover import optimal_iterations
from quantum_core.algorithms.sat_search import (
    BooleanSearchProblem,
    build_sat_grover_circuit,
    eval_boolean_expression,
)
from quantum_core.backends.aer_backend import AerBackend
from quantum_core.backends.base import Circuit
from quantum_core.sync.polling import PollingConfig, wait_for_result

VARIABLES = ["x0", "x1", "x2", "x3"]
EXPRESSION = "(x0 | x1) & (~x1 | x2) & (x0 | ~x3)"


async def main() -> None:
    problem = BooleanSearchProblem(variables=VARIABLES, expression=EXPRESSION)

    # Only done here because the demo's search space is tiny (16 states).
    # See BooleanSearchProblem.count_solutions() for why this isn't the
    # real-world approach.
    solutions = problem.count_solutions()
    print(f"expression: {EXPRESSION}")
    print(f"brute-force ground truth: {len(solutions)} solution(s): {sorted(solutions)}")

    iterations = optimal_iterations(problem.num_qubits, len(solutions))
    print(f"running Grover with {iterations} iteration(s)")

    qc = build_sat_grover_circuit(problem, iterations=iterations)

    backend = AerBackend(seed_simulator=11)
    circuit = Circuit(name="sat-grover", num_qubits=problem.num_qubits, payload=qc, shots=1024)

    handle = await backend.submit(circuit)
    result = await wait_for_result(backend, handle, config=PollingConfig(timeout_s=15.0))

    assert result.counts is not None
    print(f"status={result.status}")
    for state, count in sorted(result.counts.items(), key=lambda kv: -kv[1])[:10]:
        marker = " <-- valid solution" if state in solutions else ""
        print(f"  {state}: {count}{marker}")

    # Classically verify the top measured outcome against the original
    # expression -- this is the O(1) check Grover's whole premise relies on.
    top_state = max(result.counts, key=result.counts.get)
    assignment = dict(zip(VARIABLES, (bit == "1" for bit in reversed(top_state))))
    is_valid = eval_boolean_expression(EXPRESSION, assignment)
    print(f"\ntop outcome {top_state} classically verified as valid: {is_valid}")


if __name__ == "__main__":
    asyncio.run(main())