# Оркестрация: API → RabbitMQ → orchestrator

## Что изменилось

До этого шага `POST /experiments` выполнял эксперимент **синхронно
внутри процесса API** — клиент ждал ответа всё время исполнения (для
VQE — ~55 секунд). Теперь:

1. `POST /experiments` публикует задачу в очередь `experiments` и
   немедленно возвращает `202 Accepted` со статусом `queued`.
2. `orchestrator` (отдельный процесс/сервис) читает очередь, исполняет
   эксперимент через `quantum_core`, публикует результат в очередь
   `experiment-results`.
3. API в фоне слушает `experiment-results` и обновляет свой in-memory
   store. `GET /experiments/{id}` отдаёт актуальный статус — `queued`,
   пока не пришёл результат, затем `completed`/`failed`.

## Почему RabbitMQ, а не Kafka — для этой конкретной части

Напомним ADR с самого начала проекта: для *задач* (task queue —
"выполни этот эксперимент один раз") RabbitMQ подходит лучше — есть
нормальный per-message ack/retry из коробки, и семантика "одна задача —
одна обработка" ему соответствует естественно. Kafka остаётся
кандидатом для *телеметрии* (calibration metrics, real-time аналитика) —
это отдельный, ещё не реализованный поток, см. `docs/decisions/`.

## Общий код между API и orchestrator

Бизнес-логика запуска алгоритмов (`run_grover`, `run_sat_grover`,
`run_qpe`, `run_vqe_sync`) переехала в `quantum_core/execution.py` —
общий модуль с простыми Python-типами (без Pydantic), которым пользуются
оба сервиса:

- `services/api/app/routers/experiments.py` — теперь публикует задачу и
  **не** вызывает `quantum_core.execution` напрямую вообще;
- `services/orchestrator/app/worker.py` — вызывает `quantum_core.execution`
  напрямую, разбирая `params` из сообщения очереди.

Формат сообщений — `quantum_core/tasks.py` (`ExperimentTask`,
`ExperimentResultMessage`) — простые dataclasses + JSON, без Pydantic,
чтобы не тащить HTTP-фреймворк в `quantum_core`.

## Побочный эффект: API стал значительно тоньше

Раньше `POST /experiments` для VQE требовал `run_in_threadpool` (чтобы не
блокировать event loop FastAPI синхронным `run_vqe`). Теперь API вообще
не исполняет алгоритмы — просто публикует JSON в очередь. Вся забота о
sync/async-мосте для VQE (`run_vqe` синхронна, использует `asyncio.run()`
внутри) переехала в `orchestrator`, который решает её через
`loop.run_in_executor()` — прямой asyncio-эквивалент того же
`run_in_threadpool`.

Более неожиданный побочный эффект: раз `api/app/execution.py` удалён, а
роутер больше не импортирует `quantum_core.algorithms.*` напрямую — **весь
API-сервис теперь тестируется без установленного Qiskit**. Единственное
место, где Qiskit вообще упоминается в API — ленивый импорт в
`get_backend()` (`app/deps.py`), который сейчас не используется роутером
экспериментов вовсе.

## Семантика ack/reject/retry в orchestrator

Три разных случая обрабатываются по-разному:

1. **Некорректное сообщение** (не парсится JSON, неизвестный алгоритм) —
   отправляется в dead-letter очередь `experiments.dlq`
   (`retry_policy.send_to_dead_letter_queue`), исходное — `ack()`.
   Повторная обработка того же сообщения даст тот же результат — смысла в
   retry нет, но и терять его молча тоже не стоит (в первой версии
   `worker.py` такие сообщения именно терялись — это было исправлено).
2. **Ошибка исполнения алгоритма** (упавшая схема, таймаут backend'а) —
   задача считается обработанной: результат зафиксирован как `failed` и
   отправлен в `experiment-results`, исходное сообщение — `ack()`. Это
   осознанный результат, а не сбой очереди — сюда `retry_policy` не
   применяется вообще.
3. **Сбой самого воркера** (обрыв соединения, необработанное исключение
   до вызова ack/reject) — RabbitMQ автоматически передоставит сообщение
   (`message.redelivered=True`). Без политики это могло бы продолжаться
   **бесконечно**, если конкретное сообщение стабильно роняет воркера.
   `retry_policy.handle_redelivery()` ограничивает это тремя попытками
   с exponential backoff (2s/4s/8s, счётчик — в заголовке
   `x-retry-count`), после чего сообщение тоже уходит в `experiments.dlq`.

Это отдельная политика от retry/backoff в `quantum_core/sync/polling.py`
— тот отвечает за повтор отдельных вызовов backend'а *внутри* одной
задачи (submit/poll/fetch на нестабильном, но исправно работающем
QuantumBackend); `retry_policy.py` — за случай, когда падает сам процесс
воркера, что backend-level retry исправить не может в принципе.

## Структура `orchestrator`: tasks/ и calibration

После первой версии `worker.py` содержал всю логику (подключение к
RabbitMQ, диспетчеризацию по алгоритму, обработку сообщений) в одном
файле. Вынесено отдельно:

- `app/tasks/run_experiment.py` — диспетчеризация `task.algorithm` →
  `quantum_core.execution`. Не знает про `aio-pika` вообще (принимает
  только `ExperimentTask`, обычный dataclass) — тестируется без
  RabbitMQ.
- `app/tasks/calibration.py` — периодическая проверка backend'а через
  Bell-состояние (`error_rate` = доля shots с `01`/`10` вместо ожидаемых
  `00`/`11`). Публикует в очередь `calibration-results` — временная
  замена Kafka-потока телеметрии из самого первого архитектурного
  разговора этого проекта. Честная оговорка: `AerBackend` — noiseless,
  так что `error_rate` пока всегда ~0 — это здоровый health-check
  ("backend отвечает"), но не источник сигнала о реальном дрейфе, пока
  не подключена noise-модель или настоящее железо.
- `app/worker.py` — теперь только RabbitMQ-соединение, запуск
  calibration-цикла фоновой задачей, и главный consume-loop.

## ⚠️ Степень проверки

Как и с API-слоем — **ничего из RabbitMQ-кода не запускалось**: у меня
нет ни `aio-pika`, ни Docker, ни сети. Отдельно стоит отметить конкретную
вещь, которую я перепроверил веб-поиском перед тем как писать код (а не
понадеялся на память) — попытка сделать `queue.bind()` на default
exchange в RabbitMQ **завершается ошибкой** `ACCESS_REFUSED`
("operation not permitted on the default exchange"): default exchange
уже автоматически роутит по имени очереди, явный bind не нужен и не
разрешён. Первая версия `worker.py` эту ошибку содержала — исправлена
до того, как код был показан.

`quantum_core/tasks.py` (сериализация задач/результатов) — проверен
и прогнан локально (round-trip JSON, включая failure-случай) без единого
внешнего зависимого пакета, чистый stdlib. Арифметика `error_rate` в
`calibration.py` — тоже проверена отдельно (тривиальная, но для
консистентности с остальным проектом).

## Как запустить целиком

```bash
# из корня репозитория
docker compose up -d          # поднимает RabbitMQ, UI на localhost:15672 (guest/guest)

# терминал 1 — API
cd services/api
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000

# терминал 2 — orchestrator
cd services/orchestrator
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python3 -m app.worker

# терминал 3 — запрос
curl -X POST http://localhost:8000/experiments \
  -H "Content-Type: application/json" \
  -d '{"algorithm": "grover", "marked_states": ["101"]}'
# ответ сразу: {"status": "queued", "id": "...", ...}

curl http://localhost:8000/experiments/<id>
# через мгновение: {"status": "completed", "result": {...}}
```

## Пока не реализовано

- Persistence (Postgres) для store экспериментов — сейчас всё ещё
  in-memory на стороне API, не переживает рестарт;
- Несколько воркеров orchestrator для параллельной обработки (сейчас
  `prefetch_count=1`, один воркер = одна задача единовременно);
- `retry_policy.py` держит задержку между повторами в event loop воркера
  (`asyncio.sleep`) — при нескольких воркерах это не даёт освободить
  сообщение для подхвата другим воркером на время задержки; приемлемо
  для одного воркера, стоит пересмотреть при масштабировании;
- Noise-модель для `AerBackend` — без неё `calibration.py` не увидит
  реального дрейфа (`error_rate` всегда ~0), только подтверждает, что
  backend вообще отвечает;
- Kafka-поток телеметрии — `calibration-results` пока просто ещё одна
  RabbitMQ-очередь, а не настоящий time-series поток, как обсуждалось в
  первом ADR этого проекта.