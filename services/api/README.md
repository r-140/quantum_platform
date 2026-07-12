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

## ⚠️ Степень проверки

**Ничего в этом сервисе не запускалось** — в отличие от файлов на Qiskit
(где я мог хотя бы перепроверить математику независимо через numpy), у
меня в среде нет ни `fastapi`, ни `pydantic`, ни сети для их установки.
Единственное, что было проверено — конкретный синтаксис Pydantic
discriminated union с enum (через веб-поиск документации, см. оговорку
про `Literal["grover"]` выше). Всё остальное — код, написанный по
известному мне API FastAPI/Starlette/Pydantic, без единого реального
прогона.

**Обязательно сделай smoke-test перед тем как считать это готовым:**

```bash
cd services/api
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

Затем в другом терминале (с активным `.venv` из `services/quantum-core`,
либо просто `curl`):

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

Либо открой `http://localhost:8000/docs` — интерактивная Swagger-документация,
там же можно проверить, что схема discriminated union отрисовалась корректно
(это как раз то место, где проявился бы упомянутый выше баг с Enum-дискриминатором,
если бы я его не обошёл).

Ожидаемый результат последнего запроса — JSON с `status: "completed"` и
`result.counts`, где `"101"` доминирует — как в `demo_grover.py`.

Пришли, что получилось — если что-то не заведётся, разберём отдельно от
уже проверенной логики `quantum_core`.