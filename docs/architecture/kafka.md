# Kafka: телеметрия калибровки

## Что изменилось

Из самого первого архитектурного разговора этого проекта: RabbitMQ — для
task queue ("выполни это один раз"), Kafka — для потока телеметрии
("много событий, real-time агрегация"). До этого момента
`calibration.py` публиковал `error_rate` в RabbitMQ-очередь
`calibration-results` как явно задокументированную **временную замену**.
Теперь это настоящий Kafka-топик, и появился новый сервис —
`stream-analytics` — real-time consumer, считающий rolling average
`error_rate` по backend'ам.

Как и обещали в доке при первой реализации calibration: переход не
потребовал трогать `run_calibration()` — только `publish_calibration_result()`
(теперь `AIOKafkaProducer.send_and_wait()` вместо публикации в очередь
RabbitMQ). Вся математика error_rate, сам Bell-circuit health-check —
не изменились.

## Kafka в Docker: KRaft-режим, без Zookeeper

Добавлен один брокер Kafka в `docker-compose.yml`, в режиме **KRaft**
(`broker,controller` в одном контейнере) — начиная с Kafka 3.x/Confluent
Platform 7.5+ Zookeeper считается legacy-подходом, а KRaft — стандартный
способ поднять Kafka локально сегодня. Проверил актуальность через
веб-поиск (несколько независимых источников, датированных 2026 годом,
подтверждают KRaft как текущий стандарт) — не полагался на память,
учитывая, как давно и радикально менялась архитектура Kafka-деплоя.

`CLUSTER_ID` — фиксированная строка, широко используемый example ID из
документации Confluent (не секрет, не специфичен для окружения) — любой
валидный base64-encoded 16-byte UUID подошёл бы.

## `stream-analytics`: почему не Kafka Streams/Faust

Изначально в первом наброске структуры проекта `stream-analytics`
подразумевал Kafka Streams или Faust. Вместо этого — простой consumer-loop
(`AIOKafkaConsumer`) с ручной агрегацией в памяти (`RollingErrorRate`,
`collections.deque` с `maxlen`). Это осознанный, а не вынужденный выбор:
- реальный объём событий в этом проекте — один calibration-цикл на
  экземпляр `orchestrator` раз в 5 минут — крошечный по меркам, для
  которых существуют Kafka Streams/Faust;
- `deque(maxlen=N)` полностью выражает нужную семантику ("rolling average
  по последним N сэмплам") без стороннего framework'а;
- честно обозначено в докстринге `rolling.py`, когда стоит пересмотреть
  это решение: несколько продюсеров, заметно больший объём, или
  необходимость, чтобы состояние окна переживало рестарт процесса —
  именно для этого существуют RocksDB-backed state store в Kafka Streams.

## Проверка

Как и с математикой алгоритмов в `quantum_core`, логика `RollingErrorRate`
проверена **независимо от Kafka** — сначала как отдельный скрипт (без
pytest, без брокера), затем перенесена в реальные файлы
(`app/rolling.py` + `tests/test_rolling.py`) и **прогнана в этом виде**
(не черновик):

```
test_single_sample_average_equals_itself       PASSED
test_average_over_growing_window                PASSED
test_oldest_sample_evicted_once_window_full     PASSED
test_backends_tracked_independently             PASSED
test_unknown_backend_has_zero_samples           PASSED
```

Особо стоит отметить `test_oldest_sample_evicted_once_window_full` —
проверяет, что при заполнении окна самый старый сэмпл вытесняется, а не
окно растёт бесконечно; это самая содержательная часть логики, и её
стоило проверить явно, а не понадеяться, что `deque(maxlen=...)` "просто
работает" правильно в связке с расчётом среднего.

Отдельно проверил актуальный API `aiokafka`
(`AIOKafkaProducer`/`AIOKafkaConsumer`, `send_and_wait`, `async for msg in
consumer`) через веб-поиск официальной документации и PyPI — не полагался
на память.

⚠️ **Не проверено вообще**: сам `aiokafka`-код против реального Kafka,
Docker-конфигурация KRaft (healthcheck, `KAFKA_ADVERTISED_LISTENERS` и
т.д.) — у меня нет ни `aiokafka`, ни Docker, ни сети для полноценной
проверки. Учитывая, что уже дважды в этом проекте (RabbitMQ `bind()`,
Alembic `-m` импорт) реальный прогон выявлял то, что я не мог предвидеть
без брокера/БД под рукой — весьма вероятно, что и здесь при первом
реальном запуске всплывёт что-то похожее (конфигурация листенеров Kafka
особенно known for капризности при первом запуске, судя по найденным
источникам — "critical setting is KAFKA_ADVERTISED_LISTENERS; if it points
at the wrong host/port, clients connect once and then fail on broker
metadata").

## TimescaleDB: персистентность сырых calibration-событий

`RollingErrorRate` живёт только в памяти процесса `stream-analytics` —
рестарт обнуляет накопленную историю. `app/sinks/timescale_sink.py`
закрывает этот пробел: каждое сырое calibration-событие (не только
текущий rolling average) пишется в TimescaleDB, в таблицу-hypertable
`calibration_events`.

Отдельная база (`timescaledb`, порт 5433), не тот же Postgres, что уже
используется для метаданных экспериментов (`postgres`, порт 5432) —
осознанное разделение: разные назначения (transactional-метаданные
экспериментов vs time-series телеметрия), разные жизненные циклы, разные
паттерны доступа (upsert по id vs append-only вставки). Схема (одна
таблица + `create_hypertable`) создаётся автоматически при первом старте
контейнера через `docker-entrypoint-initdb.d`-конвенцию — Alembic для
одной таблицы такого рода был бы избыточен (в отличие от `api`, где
множество эволюционирующих полей и связей уже оправдывает миграции).

⚠️ **Версионный риск, найденный заранее**: TimescaleDB 2.13+ ввела новый
generalized API для `create_hypertable` — `by_range('time')` вместо
старой сигнатуры `create_hypertable('table', 'time')`. Нашёл открытый баг
(`timescale/timescaledb#6875`), где `by_range()` не резолвился в
зависимости от `search_path`/варианта образа. Использована **старая**
сигнатура — она задокументирована как поддерживаемая для обратной
совместимости и не фигурирует в найденном баг-репорте.

`asyncpg` используется напрямую (не через SQLAlchemy, как в `api`) — здесь
единственная потребность в БД — одна append-only вставка на событие,
полноценная ORM была бы чистым оверхедом.

**`counts` (сырая гистограмма измерений) намеренно не пишется** в
hypertable — таблица предназначена для агрегатной метрики во времени, не
для дублирования сырых данных, которые и так остаются в самом Kafka-логе.
Отдельный тест (`test_insert_calibration_event_does_not_forward_counts`)
специально проверяет, что это не изменится незаметно в будущем.

### Проверка

Логика `insert_calibration_event` (парсинг timestamp, порядок
связываемых параметров, то, что `counts` не попадает в INSERT) проверена
через рукописный `FakePool` — записывает вызовы `execute()` вместо
обращения к реальной БД. Прогнано на **реальных файлах**
(`timescale_sink.py` + `test_timescale_sink.py`), с временной заглушкой
самого пакета `asyncpg` (у меня его нет) — 2/2 теста прошли.

Отдельно проверил round-trip `datetime.isoformat()` →
`datetime.fromisoformat()` для timezone-aware значений — то самое место,
где уже один раз (см. `docs/architecture/postgres.md`) возникал баг с
naive/aware datetime.

⚠️ **Не проверено вообще**: сам SQL против реального TimescaleDB,
`docker-entrypoint-initdb.d`-инициализация, `asyncpg`-подключение к
контейнеру. У меня нет ни `asyncpg`, ни Docker.

## Faust: настоящий "Kafka Streams для Python"

`stream-analytics/app/consumer.py` (hand-rolled `aiokafka`-consumer) и
`stream-analytics/app/faust_app.py` (новый) решают **одну и ту же**
задачу — rolling average `error_rate` по backend — двумя разными
инструментами, специально для сравнения. "Kafka Streams" в строгом
смысле — Java/Scala-библиотека; Python-эквивалент с похожей семантикой
(tables, windowing, changelog-backed state) — `faust-streaming`, активно
поддерживаемый форк оригинального `faust` (Robinhood, заброшен с 2020).

Оба consumer'а могут работать **одновременно** против одного топика — у
каждого своя consumer group (`stream-analytics` для hand-rolled,
`stream-analytics-faust` для Faust), поэтому они не конкурируют за
партиции.

**Ключевое отличие от `RollingErrorRate`**: Faust'овская windowed
`Table` — changelog-backed. Каждое обновление таблицы дополнительно
пишется во внутренний Kafka-топик (changelog); при рестарте воркера Faust
реплеит этот changelog и восстанавливает состояние. У `RollingErrorRate`
(обычный `deque` в памяти) такого свойства нет — рестарт обнуляет его
полностью. TimescaleDB (см. выше) закрывает тот же пробел другим
способом — через внешнюю БД, а не встроенный механизм Kafka. Три разных
ответа на "как не терять состояние при рестарте" в одном небольшом
проекте — удобный повод сравнить их напрямую.

Использована tumbling-window агрегация (60 секунд, не совпадает с
интервалом calibration по умолчанию 300 секунд — специально, чтобы было
что понаблюдать в разумные сроки демо, большинство окон будут пустыми,
это ожидаемо) — две параллельные `Table` (сумма + счётчик) вместо одной
составной, по образцу из официальной документации Faust.

⚠️ **Версионный риск, проверенный заранее**: у `faust-streaming` были
проблемы совместимости с новыми версиями Python в прошлом (не
поддерживал 3.10 на момент issue #762 в 2022). Официальная поддержка
Python 3.12 добавлена в PR #587 — зафиксировал минимальную версию
(`faust-streaming>=0.10.21`) в `requirements.txt` явно, а не понадеялся,
что "последняя версия" подойдёт.

По умолчанию Faust хранит состояние таблиц в RocksDB (нативное C++
расширение) — сознательно переключил на `store="memory://"`, чтобы не
требовать сборки `rocksdb` для demo/learning-окружения. Это не меняет
changelog-backed свойство (оно от Kafka, не от локального хранилища) —
влияет только на то, где живут данные таблицы *между* обращениями внутри
одного запущенного процесса.

### Запуск

```bash
cd services/stream-analytics
source .venv/bin/activate
pip install -r requirements.txt

python3 -m app.faust_app worker -l info
# или, более идиоматично для Faust:
faust -A app.faust_app worker -l info
```

Полезные встроенные команды Faust CLI (когда воркер не запущен):
```bash
faust -A app.faust_app tables    # список таблиц
faust -A app.faust_app agents    # список агентов
```

## Как запустить

Уже встроено в `./dev.sh` — поднимает Kafka и TimescaleDB вместе с
RabbitMQ/Postgres, ждёт healthcheck обоих, запускает `stream-analytics`
третьим сервисом.

Вручную:

```bash
docker compose up -d kafka timescaledb

cd services/orchestrator
source .venv/bin/activate
pip install -r requirements.txt
python3 -m app.worker          # публикует calibration-results в Kafka каждые 5 минут

# в другом терминале
cd services/stream-analytics
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python3 -m app.consumer        # слушает, логирует rolling average, пишет в TimescaleDB
```

Проверка топика напрямую (без Python, через сам контейнер):
```bash
docker exec -it quantum-platform-kafka kafka-console-consumer \
  --bootstrap-server localhost:9092 --topic calibration-results --from-beginning
```

Проверка данных в TimescaleDB напрямую:
```bash
docker exec -it quantum-platform-timescaledb psql -U quantum -d telemetry \
  -c "SELECT * FROM calibration_events ORDER BY time DESC LIMIT 10;"
```

## Пока не реализовано

- `ALERT_THRESHOLD = 0.05` в `stream-analytics/app/consumer.py` — по сути
  плейсхолдер: без noise-модели на `AerBackend` реального распределения
  `error_rate`, под которое стоило бы калибровать порог, попросту нет;
- Множественные consumer'ы в одной consumer group (`stream-analytics`) для
  горизонтального масштабирования — пока не нужно при текущем объёме;
- Запросы к `calibration_events` (тренды, дашборды) пока никем не
  используются — таблица заполняется, но ничего пока её не читает,
  кроме ручной проверки через `psql` выше.