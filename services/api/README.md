# api

Тонкий FastAPI-слой, принимающий запросы на квантовые эксперименты
(Grover, SAT-Grover, QPE, VQE) и исполняющий их через `quantum_core`.

Пока исполнение **синхронное-в-процессе**: запрос выполняется полностью,
и ответ возвращается только когда эксперимент завершён (или упал). Очереди
пока нет — это следующий шаг проекта (RabbitMQ + `orchestrator`-сервис).
Когда он появится, `POST /experiments` будет сразу возвращать
`status=queued`, а фактическое исполнение переедет в воркер — но сама
функция-мост между запросом и `quantum_core` (`app/execution.py`) при
этом менять не придётся, только `routers/experiments.py`.

## Структура

```
api/
├── requirements.txt
└── app/
    ├── main.py                  # точка входа FastAPI
    ├── deps.py                  # общий AerBackend + in-memory store экспериментов
    ├── execution.py             # мост между схемами запросов и quantum_core
    ├── schemas/
    │   └── experiments.py       # Pydantic-модели: discriminated union по алгоритму
    └── routers/
        ├── experiments.py       # POST/GET /experiments
        └── backends.py          # GET /backends (пока информационный)
```

### `app/schemas/experiments.py`
`ExperimentRequest` — discriminated union из 4 моделей
(`GroverRequest`/`SatGroverRequest`/`QPERequest`/`VQERequest`), различаемых
по полю `algorithm`. FastAPI/Pydantic сами определяют нужную модель по
значению этого поля и валидируют форму запроса соответственно.

⚠️ Дискриминатор специально сделан строковым литералом (`Literal["grover"]`),
а не членом enum (`Literal[Algorithm.GROVER]`) — по этой конкретной
комбинации в Pydantic (Enum-дискриминатор в discriminated union) есть
задокументированный баг генерации OpenAPI-схемы (некорректный `mapping` в
`/docs`). Простые строки не подвержены этой проблеме и это стандартный
паттерн из документации Pydantic.

### `app/execution.py`
Функции `run_grover`/`run_sat_grover`/`run_qpe` — асинхронные (только
асинхронный I/O через `QuantumBackend`). `run_vqe_sync` — **синхронная**,
потому что `quantum_core.loops.vqe_loop.run_vqe` сама синхронная (мостит в
`asyncio.run()` на каждой итерации оптимизатора — см.
`docs/algorithms/vqe.md`).

### `app/routers/experiments.py`
⚠️ **Важный нюанс**: `run_vqe_sync` вызывается через
`starlette.concurrency.run_in_threadpool`, а не напрямую `await`. Прямой
вызов синхронной блокирующей функции из `async def`-обработчика заблокировал
бы весь event loop FastAPI на всё время работы VQE (десятки итераций,
несколько схем на каждую) — сервер перестал бы отвечать на любые другие
запросы, пока VQE не закончится. Это не стилевая придирка, а реальный баг,
который проявится только под конкурентной нагрузкой — то есть на одном
запросе за раз всё будет выглядеть нормально.

### `app/deps.py`
In-memory хранилище экспериментов (`ExperimentStore`) — с `threading.Lock`,
потому что VQE-эндпоинт исполняется в отдельном потоке threadpool'а
(`run_in_threadpool`), а не в основном event loop потоке, как остальные
эндпоинты — то есть доступ к store оттуда действительно конкурентный, в
отличие от остальных (однопоточных) путей.

⚠️ Это временное упрощение: хранилище не переживёт перезапуск процесса и не
будет работать корректно при нескольких воркерах uvicorn (`--workers N>1`)
— каждый получит свою копию. Именно этот пробел должен закрыть Postgres,
когда дойдём до storage-слоя.

## Юнит-тесты

```
tests/
├── conftest.py               # fixture client (TestClient) + fresh_store (изоляция между тестами)
├── test_store.py             # ExperimentStore напрямую -- без Qiskit благодаря ленивому импорту в deps.py
├── test_experiments_router.py # dispatch, ошибки как FAILED (не 500), threadpool для VQE, GET-эндпоинты
└── test_validation.py        # граничные случаи Pydantic-схем (422 при некорректных данных)
```

`app.execution`'s функции (`run_grover`, `run_sat_grover`, `run_qpe`,
`run_vqe_sync`) в `test_experiments_router.py` подменены (`monkeypatch`) на
мгновенные заглушки — тесты проверяют HTTP-слой (валидация, dispatch,
статус-коды, обработка ошибок, offload VQE в threadpool), а не физическую
корректность самих алгоритмов (это уже покрыто демками `quantum_core`).

⚠️ **Важный нюанс про `app/deps.py`**: импорт `AerBackend` сделан
ленивым (внутри `get_backend()`, а не на уровне модуля) — иначе любой тест
API, даже `test_store.py` (чистая Python-логика `ExperimentStore`, без
единого HTTP-запроса), тянул бы за собой `qiskit` просто через цепочку
импортов `app.deps → quantum_core.backends.aer_backend → qiskit_aer`.
`test_experiments_router.py`/`test_validation.py` всё равно требуют Qiskit
установленным (они импортируют `app.main`, а тот тянет `execution.py`,
которая напрямую строит реальные Qiskit-схемы в бизнес-логике) — но это
ожидаемо, так как Qiskit и так обязательная зависимость этого сервиса.

```bash
cd services/api
source .venv/bin/activate
pytest tests/ -v
```

## ⚠️ Степень проверки

Сам API (все 4 эндпоинта, discriminated union, sync/async мост для VQE
через `run_in_threadpool`) **подтверждён рабочим** — прогнан вручную через
`curl` для всех четырёх алгоритмов, включая проверку, что `/health`
отвечает мгновенно во время работы VQE в фоне (см. историю разработки).
Юнит-тесты выше — новый код, добавленный после этой проверки, и **сами
тесты** ещё не прогонялись ни разу (нет `pytest`/`fastapi`/`httpx` в моей
среде). Прогони `pytest tests/ -v` и пришли результат.

Если захочется повторить ручную проверку эндпоинтов:

```bash
cd services/api
source .venv/bin/activate
uvicorn app.main:app --reload --port 8000
```

```bash
curl http://localhost:8000/health

curl -X POST http://localhost:8000/experiments \
  -H "Content-Type: application/json" \
  -d '{"algorithm": "grover", "marked_states": ["101"]}'

# SAT-Grover
curl -X POST http://localhost:8000/experiments -H "Content-Type: application/json" \
  -d '{"algorithm": "sat_grover", "variables": ["x0","x1","x2","x3"], "expression": "(x0 | x1) & (~x1 | x2) & (x0 | ~x3)"}'

# QPE
curl -X POST http://localhost:8000/experiments -H "Content-Type: application/json" \
  -d '{"algorithm": "qpe", "phi": 0.625, "num_counting_qubits": 3}'

# VQE (самый долгий — проверит заодно run_in_threadpool)
curl -X POST http://localhost:8000/experiments -H "Content-Type: application/json" \
  -d '{"algorithm": "vqe"}'
```

Интерактивная документация — `http://localhost:8000/docs`.