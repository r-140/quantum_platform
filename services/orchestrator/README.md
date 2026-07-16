# orchestrator

Воркер, читающий задачи из RabbitMQ (`experiments`), исполняющий их через
`quantum_core`, и публикующий результат обратно (`experiment-results`) —
чтобы `api`-сервис мог обновить статус эксперимента. Плюс периодический
calibration-цикл, проверяющий здоровье backend'а.

Подробности архитектуры (почему так, семантика ack/reject, побочные
эффекты для API) — в `docs/architecture/orchestration.md`.

## Структура

```
orchestrator/
├── requirements.txt
└── app/
    ├── worker.py             # тонкий shell: RabbitMQ-соединение + consume loop
    ├── retry_policy.py       # ограниченные повторы + dead-letter queue при крэше воркера
    └── tasks/
        ├── run_experiment.py  # диспетчеризация по task.algorithm -> quantum_core.execution
        └── calibration.py     # периодическая проверка fidelity backend'а
```

### `app/tasks/run_experiment.py`
`execute_task()` — диспетчеризация по `task.algorithm`, вызывает
`quantum_core.execution` (тот же общий модуль, которым раньше пользовался
`api`, пока не переехал на очередь). Ранее жил прямо в `worker.py`;
вынесен отдельно, чтобы `worker.py` оставался тонким, и чтобы диспетчеризацию
можно было тестировать без единого обращения к RabbitMQ (эта функция вообще
не знает про `aio-pika` — только про `ExperimentTask`, обычный dataclass).

### `app/tasks/calibration.py`
Периодическая проверка backend'а: гоняет Bell-состояние (тот же `H`+`CX`,
что в `demo_aer.py`, уже подтверждённый рабочим), считает `error_rate` —
долю shots, не согласующихся с идеальной запутанностью (`01`/`10` вместо
ожидаемых только `00`/`11`).

⚠️ **Честное ограничение**: `AerBackend` — noiseless-симулятор, поэтому
`error_rate` сегодня всегда читается около `0.0` — дрейфа калибровки
физически неоткуда взяться. Модуль всё равно ценен как реальный health-check
(backend отвечает, схемы ведут себя как ожидается), и это естественное
место для подключения noise-модели Aer или реального железа в будущем —
логика подсчёта `error_rate` от этого не изменится.

Результаты сейчас публикуются в RabbitMQ-очередь `calibration-results` —
это временная замена Kafka-потока телеметрии, который обсуждался в самом
первом архитектурном разговоре этого проекта (RabbitMQ — для task queue,
Kafka — для time-series телеметрии; см. `docs/architecture/orchestration.md`).
Переход на Kafka не должен потребовать менять `run_calibration()` — только
`publish_calibration_result()`.

Можно запустить разово вручную (без RabbitMQ, печатает результат в stdout):
```bash
python3 -m app.tasks.calibration
```

### `app/retry_policy.py`
Отдельная от `quantum_core.sync.polling` политика — та отвечает за retry
отдельных вызовов backend'а *внутри* одной задачи; эта — за то, что
происходит, когда воркер падает **до** ack/reject сообщения целиком
(обрыв соединения, необработанное исключение). Без этой политики RabbitMQ
передоставлял бы такое сообщение **бесконечно**, если оно стабильно роняет
воркера ("poison message").

- `handle_redelivery()` — вызывается первым для каждого сообщения; если
  `message.redelivered=True` (RabbitMQ уже пыталось доставить это
  сообщение и не получило ack/reject), решает: повторить с exponential
  backoff (до `MAX_RETRIES=3`, задержки 2s/4s/8s) или отправить в
  `experiments.dlq` (dead-letter очередь для ручного разбора);
  - счётчик повторов — в заголовке сообщения `x-retry-count`, который
    ведёт сам код (не полагается на встроенный механизм RabbitMQ
    `x-death`/TTL+DLX — так проще проверить логику без живого брокера).
- Malformed-сообщения (не парсится JSON) тоже попадают в `experiments.dlq`,
  а не пропадают молча, как было в первой версии `worker.py`.

### `app/worker.py`
Теперь только: подключение к RabbitMQ, запуск фонового
`run_calibration_loop()` (по умолчанию раз в 5 минут, настраивается через
`CALIBRATION_INTERVAL_S`), и главный consume-loop с `prefetch_count=1`
(одна задача одновременно на воркер — для параллелизма запускай несколько
процессов `worker.py`, а не поднимай `prefetch_count`, пока не будет
измерено, что это узкое место).

VQE (единственный синхронный алгоритм в `quantum_core.execution`)
оффлоадится через `asyncio.get_running_loop().run_in_executor()` — тот же
приём, что `run_in_threadpool` на стороне API, только без Starlette.

## Как запустить

Требуется работающий RabbitMQ (`docker compose up -d` из корня репозитория).

```bash
cd services/orchestrator
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python3 -m app.worker
```

⚠️ Именно `python3 -m app.worker`, не `python3 app/worker.py` — второе
падает с `ModuleNotFoundError: No module named 'app'`, потому что модули
здесь используют абсолютные импорты (`from app import retry_policy`),
которым нужен `services/orchestrator/` (родитель `app/`) на `sys.path`.
`-m` добавляет его туда автоматически; прямой запуск файла — нет.

Переменные окружения: `RABBITMQ_URL` (по умолчанию
`amqp://guest:guest@localhost/`), `CALIBRATION_INTERVAL_S` (по умолчанию
`300` — раз в 5 минут).

## ⚠️ Степень проверки

**Ничего здесь не запускалось** — нет ни `aio-pika`, ни Docker, ни сети в
моей среде. Код написан по официальной документации `aio-pika` (проверил
актуальный API через веб-поиск) — включая один конкретный момент, который
специально перепроверил и из-за которого переписал код: явный `bind()` на
default exchange RabbitMQ **запрещён** (`ACCESS_REFUSED`), т.к. очередь
уже автоматически доступна через default exchange по своему имени.

Чистую логику backoff/retry-count в `retry_policy.py` и арифметику
`error_rate` в `calibration.py` проверил отдельно, без `aio-pika`.

**Обязательно прогони end-to-end сценарий** (см.
`docs/architecture/orchestration.md`, раздел "Как запустить целиком") и
пришли результат — включая логи самого воркера: там должны появиться
строки вида `processing experiment_id=... algorithm=grover`,
`experiment_id=... -> completed`, и (если подождать 5 минут, либо
временно понизить `CALIBRATION_INTERVAL_S` для проверки) строка вида
`calibration cycle: error_rate=0.0000 shots=1024`.