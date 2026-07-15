# api

Тонкий FastAPI-слой, принимающий запросы на квантовые эксперименты
(Grover, SAT-Grover, QPE, VQE).

**С появлением RabbitMQ/orchestrator (см. `docs/architecture/orchestration.md`)
API больше не исполняет эксперименты сам.** `POST /experiments`
публикует задачу в очередь и сразу возвращает `202 Accepted` со статусом
`queued`. Исполняет эксперимент отдельный сервис `orchestrator`; когда
результат готов, API узнаёт об этом через очередь `experiment-results` и
обновляет свой store. `GET /experiments/{id}` показывает актуальный
статус.

## Структура

```
api/
├── requirements.txt
└── app/
    ├── main.py                  # точка входа, lifespan (RabbitMQ), consumer результатов
    ├── deps.py                  # store экспериментов + RabbitMQ publish_task
    ├── schemas/
    │   └── experiments.py       # Pydantic-модели: discriminated union по алгоритму
    └── routers/
        ├── experiments.py       # POST (публикует задачу) / GET /experiments
        └── backends.py          # GET /backends (информационный)
```

Обрати внимание: `app/execution.py` **удалён** — бизнес-логика запуска
алгоритмов переехала в `quantum_core/execution.py`, общий для `api` и
`orchestrator` модуль (простые Python-типы, без Pydantic). API теперь
вообще не импортирует `quantum_core.algorithms.*` напрямую.

### `app/schemas/experiments.py`
`ExperimentRequest` — discriminated union из 4 моделей
(`GroverRequest`/`SatGroverRequest`/`QPERequest`/`VQERequest`), различаемых
по полю `algorithm`.

⚠️ Дискриминатор — строковый литерал (`Literal["grover"]`), не член enum
(`Literal[Algorithm.GROVER]`) — у последнего есть задокументированный баг
генерации OpenAPI-схемы в Pydantic.

`ExperimentStatus` теперь включает `queued` (не только `completed`/`failed`)
— отражает то, что исполнение асинхронное.

### `app/deps.py`
- `ExperimentStore` — in-memory, потокобезопасный (`threading.Lock`).
  ⚠️ Временное упрощение: не переживает рестарт процесса, не работает
  корректно с несколькими воркерами uvicorn — это должен закрыть Postgres,
  когда дойдём до storage-слоя;
- `get_backend()` — оставлен (ленивый импорт `AerBackend`, не тянет Qiskit
  на уровне модуля), но сейчас не используется роутером — просто доступен
  на будущее (debug/sync-режим, тесты);
- `init_rabbitmq()`/`close_rabbitmq()`/`publish_task()`/`get_rabbitmq_channel()`
  — RabbitMQ-соединение как process-lifetime singleton, инициализируется
  через `lifespan` в `main.py`.

### `app/main.py`
`lifespan` подключается к RabbitMQ на старте, запускает фоновую задачу
`consume_results()` (слушает `experiment-results`, обновляет store через
`apply_result_message()`), корректно всё закрывает на shutdown.

### `app/routers/experiments.py`
`POST /experiments` сохраняет запись со статусом `queued` **до**
публикации в очередь (чтобы consumer результатов не потерял быстрый
ответ, если тот придёт раньше, чем функция допишет запись) — потом
публикует `ExperimentTask`. Ошибка публикации (например, RabbitMQ
недоступен) — тоже не 500, а `FAILED`-запись с понятной причиной.

## Юнит-тесты

```
tests/
├── conftest.py                # fixture client (TestClient) + fresh_store
├── test_store.py               # ExperimentStore напрямую
├── test_experiments_router.py  # dispatch (publish_task замокан), enqueue-ошибки, GET
├── test_results_consumer.py    # apply_result_message() -- обновление store по результату
└── test_validation.py          # граничные случаи Pydantic-схем
```

⚠️ **Важный нюанс**: `TestClient(app)` в `conftest.py` используется **без**
`with`-блока — это осознанно: без `with` Starlette **не запускает
`lifespan`**, а значит тесты не пытаются установить настоящее соединение с
RabbitMQ. Все тесты, которые касаются публикации задачи, подменяют
`app.deps.publish_task` напрямую — реальный RabbitMQ для юнит-тестов не
нужен вообще. Если когда-нибудь понадобится тест, использующий
lifespan-состояние — переключение на `with TestClient(app) as client:`
приведёт к попытке реального подключения, это стоит иметь в виду.

Ещё один результат рефакторинга: раз `execution.py` удалён и роутер не
трогает `quantum_core.algorithms.*` — **весь тестовый набор API теперь не
требует установленного Qiskit** (только `fastapi`/`pydantic`/`httpx`).

```bash
cd services/api
source .venv/bin/activate
pytest tests/ -v
```

## ⚠️ Степень проверки

Синхронная версия этого сервиса (до перехода на очередь) была
**подтверждена рабочей** — прогнана вручную через `curl` для всех четырёх
алгоритмов. Всё, что появилось при переходе на RabbitMQ (lifespan,
publish_task, consume_results, новые тесты) — **не запускалось**: у меня
нет ни `aio-pika`, ни Docker, ни сети. Прогони `pytest tests/ -v`, а затем
end-to-end сценарий целиком (см. `docs/architecture/orchestration.md`,
раздел "Как запустить целиком" — там нужен ещё и `orchestrator`).

Интерактивная документация — `http://localhost:8000/docs`.