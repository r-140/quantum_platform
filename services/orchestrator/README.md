# orchestrator

Воркер, читающий задачи из RabbitMQ (`experiments`), исполняющий их через
`quantum_core`, и публикующий результат обратно (`experiment-results`) —
чтобы `api`-сервис мог обновить статус эксперимента.

Подробности архитектуры (почему так, семантика ack/reject, побочные
эффекты для API) — в `docs/architecture/orchestration.md`.

## Структура

```
orchestrator/
├── requirements.txt
└── app/
    ├── worker.py         # вся логика: consume -> execute -> publish result
    └── retry_policy.py   # ограниченные повторы + dead-letter queue при крэше воркера
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
- Malformed-сообщения (не парсится JSON) теперь тоже попадают в
  `experiments.dlq`, а не пропадают молча, как было в первой версии
  `worker.py`.

### `app/worker.py`
- `execute_task()` — диспетчеризация по `task.algorithm`, вызывает
  `quantum_core.execution` (тот же общий модуль, которым раньше
  пользовался `api`);
- `handle_message()` — парсинг сообщения, вызов `execute_task`, публикация
  результата, ack/reject исходного сообщения;
- `main()` — подключение к RabbitMQ, `prefetch_count=1` (одна задача
  одновременно на воркер — простая, предсказуемая модель для первой
  версии; для параллелизма запускай несколько процессов `worker.py`, а не
  поднимай `prefetch_count`, пока не будет измерено, что это узкое место).

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
python3 app/worker.py
```

Переменная окружения `RABBITMQ_URL` (по умолчанию
`amqp://guest:guest@localhost/`) — если RabbitMQ не на localhost/не с
дефолтными кредами.

## ⚠️ Степень проверки

**Ничего здесь не запускалось** — нет ни `aio-pika`, ни Docker, ни сети в
моей среде. Код написан по официальной документации `aio-pika` (проверил
актуальный API через веб-поиск) — включая один конкретный момент, который
специально перепроверил и из-за которого переписал код: явный `bind()` на
default exchange RabbitMQ **запрещён** (`ACCESS_REFUSED`), т.к. очередь
уже автоматически доступна через default exchange по своему имени.

Чистую логику backoff/retry-count в `retry_policy.py` (расчёт задержки,
парсинг заголовка) проверил отдельно, без `aio-pika` — 2s/4s/8s для трёх
попыток, как и задумано.

**Обязательно прогони end-to-end сценарий** (см.
`docs/architecture/orchestration.md`, раздел "Как запустить целиком") и
пришли результат — включая логи самого воркера в терминале, где он
запущен: там должны появиться строки вида
`processing experiment_id=... algorithm=grover` и
`experiment_id=... -> completed`.