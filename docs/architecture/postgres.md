# Postgres: персистентность экспериментов

## Что изменилось

`ExperimentStore` был in-memory (`threading.Lock` + dict), не переживал
рестарт процесса и не работал бы корректно с несколькими воркерами
uvicorn. Теперь это абстракция (по аналогии с `QuantumBackend` в
`quantum_core`) с двумя реализациями:

- **`InMemoryExperimentStore`** — как раньше, но теперь `asyncio.Lock`
  вместо `threading.Lock` (обоснование ниже), используется по умолчанию и
  в тестах;
- **`PostgresExperimentStore`** — настоящая персистентность через
  SQLAlchemy 2.0 async + `asyncpg`, включается через переменную
  окружения `DATABASE_URL`.

Роутеры и `main.py` зависят только от абстрактного `ExperimentStore`
(`app/store/base.py`), никогда от конкретной реализации напрямую —
`app/deps.get_store()` решает, какую вернуть, в зависимости от
`DATABASE_URL`.

## Почему `asyncio.Lock`, а не `threading.Lock`, в in-memory store

Раньше `threading.Lock` был осознанным выбором — VQE-эндпоинт исполнялся
в потоке threadpool'а (`run_in_threadpool`), поэтому доступ к store был
по-настоящему многопоточным. После перехода на очередь (RabbitMQ) API
вообще ничего не исполняет — `run_in_threadpool` для VQE больше не нужен
(см. `docs/architecture/orchestration.md`). Значит, все обращения к store
идут из корутин на одном event loop, и `asyncio.Lock` — более идиоматичный
выбор для чисто async-кода.

## Схема

Одна таблица `experiments` (`migrations/versions/0001_create_experiments_table.py`):

| колонка | тип | примечание |
|---|---|---|
| `id` | `String` | PK; строка `uuid.uuid4()`, не нативный Postgres `UUID` — см. обоснование в `models.py` |
| `algorithm` | `String` | |
| `status` | `String` | `queued`/`completed`/`failed` |
| `submitted_at` | `TIMESTAMPTZ` | индексирован — под `list_all()`/будущие "recent experiments" запросы |
| `completed_at` | `TIMESTAMPTZ`, nullable | |
| `result` | `JSONB`, nullable | нативная поддержка индексации/запросов в Postgres, хотя пока ничего не запрашивает *внутрь* JSON |
| `error` | `Text`, nullable | |

`save()` — не "проверить, есть ли запись, потом insert/update", а один
`INSERT ... ON CONFLICT (id) DO UPDATE` (SQLAlchemy:
`postgresql.insert(...).on_conflict_do_update(...)`) — без гонки между
проверкой существования и записью.

## Как это проверялось

Как и с самой первой Grover-математикой в этом проекте: перед тем как
писать Postgres-специфичный код, отдельно проверил **логику** SQL (не
сам Postgres) через `sqlite3` (stdlib, доступен без сети) — INSERT,
`ON CONFLICT DO UPDATE`, SELECT по id, ORDER BY. Все 5 сценариев прошли,
включая то, что `submitted_at` не перезаписывается при апдейте через
upsert.

⚠️ Честная оговорка: sqlite ≠ Postgres. Не покрыто этой проверкой:
- `JSONB` vs `TEXT` (в sqlite JSON хранился через ручной `json.dumps`;
  в Postgres `JSONB`-колонка сериализует/десериализует `dict` нативно,
  без ручного вызова `json.dumps` в коде — SQLAlchemy делает это
  прозрачно через диалект);
- `UUID`/`TIMESTAMPTZ` типы;
- поведение асинхронного драйвера (`asyncpg`) — на sqlite это в принципе
  не проверяется.

**Подтверждение этой оговорки на практике**: первый же реальный запуск
против настоящего Postgres упал с
`TypeError: can't subtract offset-naive and offset-aware datetimes`.
Причина — рассинхрон между миграцией (`sa.DateTime(timezone=True)`,
создаёт `TIMESTAMPTZ`) и ORM-моделью (`Mapped[datetime]` без явного
`DateTime(timezone=True)`, что SQLAlchemy по умолчанию сопоставляет с
**naive** `DateTime`). В сгенерированном SQL это было видно напрямую:
`$4::TIMESTAMP WITHOUT TIME ZONE` — каст по типу из модели, а не по
реальной схеме БД в таблице. `datetime.now(timezone.utc)` (offset-aware)
не лез в этот каст. Именно такой класс ошибки sqlite не мог поймать в
принципе — там нет различия aware/naive timestamp вообще. Исправлено
добавлением явного `DateTime(timezone=True)` в `models.py` для обеих
колонок (`submitted_at`, `completed_at`); сама таблица в БД была создана
верно ещё до этого — миграцию перекатывать не потребовалось, только
перезапустить `api` с исправленной моделью.

Отдельно проверил актуальный API SQLAlchemy 2.0 async (`create_async_engine`,
`async_sessionmaker`, `AsyncSession`) и upsert-синтаксис
(`sqlalchemy.dialects.postgresql.insert(...).on_conflict_do_update(...)`)
через веб-поиск документации — не полагался на память, учитывая, как
часто API меняется между мажорными версиями.

**Логику самого `InMemoryExperimentStore` и `apply_result_message`**
(включая upsert-паттерн, сохранение `submitted_at` при апдейте,
конкурентный доступ через `asyncio.gather`) — прогнал напрямую в
песочнице, подменив Pydantic-зависимые импорты (`ExperimentResponse`)
"утиными" заглушками с нужной семантикой (`model_copy`), поскольку
самого `pydantic` в моей среде нет. Это не полная замена реального
прогона, но ловит логические ошибки (например, в самой формуле upsert
или в порядке аргументов) до того, как код увидит настоящий Postgres.

⚠️ **Не проверено вообще**: сам `asyncpg`/`SQLAlchemy` код против
реального Postgres, Alembic-миграция (`alembic upgrade head`), весь
async-мост в `migrations/env.py`. У меня нет ни `sqlalchemy`, ни
`asyncpg`, ни `alembic`, ни Docker, ни сети.

## Alembic: async-мост

Alembic по умолчанию генерирует `env.py`, рассчитанный на синхронный
движок — `engine_from_config()` не работает с `asyncpg`. Использован
задокументированный паттерн SQLAlchemy: сама миграция (`context.run_migrations()`)
остаётся синхронным кодом, но вызывается через `AsyncConnection.run_sync(...)`
изнутри async-контекста движка (`migrations/env.py`).

`DATABASE_URL` читается из переменной окружения, а не из `alembic.ini` —
чтобы не коммитить connection string и не редактировать `.ini` при смене
окружения.

⚠️ **Второй раз в этом проекте**: `migrations/env.py` делает
`from app.store.models import Base` — абсолютный импорт, которому нужен
`services/api/` (родитель `app/`) на `sys.path`. Консольный скрипт
`alembic upgrade head` этого не даёт (та же причина, по которой
`python3 app/worker.py` в `orchestrator` падал с `ModuleNotFoundError: No
module named 'app'` — см. `docs/architecture/orchestration.md`). Фикс тот
же: запускать через `python3 -m alembic upgrade head`, а не голый
`alembic upgrade head` — `-m` добавляет текущую директорию в `sys.path`
первым элементом (подтверждено официальной документацией Python и
воспроизведено на минимальном примере перед тем, как чинить `dev.sh`).
Уже встроено в `./dev.sh`.

## Как запустить

Уже встроено в `./dev.sh` (поднимает Postgres, ждёт healthcheck, гоняет
`alembic upgrade head`, запускает `api` с `DATABASE_URL` в окружении).

Вручную:

```bash
docker compose up -d postgres
cd services/api
source .venv/bin/activate
pip install -r requirements.txt

export DATABASE_URL="postgresql+asyncpg://quantum:quantum@localhost:5432/quantum_platform"
python3 -m alembic upgrade head

uvicorn app.main:app --reload --port 8000
```

Без `DATABASE_URL` в окружении API по-прежнему стартует и работает —
просто на in-memory store (лог при старте предупредит об этом явно, не
молча).

## Пока не реализовано

- Connection pooling под нагрузкой не тюнился (дефолтные настройки
  `create_async_engine`) — нет ни одного бенчмарка, который бы это
  оправдывал на данном этапе;
- `orchestrator` пока ничего не знает про Postgres — сам он не хранит
  состояние, только читает/публикует в RabbitMQ; если позже понадобится
  хранить историю calibration-результатов (а не только последний снимок
  в очереди), это тоже вероятный кандидат на отдельную таблицу.