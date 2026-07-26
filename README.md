# quantum-platform

Пет-проект: платформа для запуска квантовых алгоритмов (Grover, SAT-Grover,
QFT/QPE, VQE) с полноценной production-style архитектурой вокруг них —
API, очередь задач, оркестрация, персистентность, потоковая телеметрия,
дашборды. Изначальная цель — погружение в architecture/platform design для
quantum computing инфраструктуры, а не просто "запустить пару алгоритмов".

## Быстрый старт

Требуется Docker, Python 3.11+.

```bash
./dev.sh                    # поднимает всё: инфра + api + orchestrator + stream-analytics
./dev.sh --profile=verify   # то же самое, но сначала прогоняет тесты каждого сервиса
./dev.sh --help             # справка по флагам
```

Первый запуск создаст `.venv` в каждом сервисе и поставит зависимости
автоматически. Логи — в `.dev-logs/` (gitignored), выводятся в терминал
через `tail -f`. `Ctrl+C` останавливает `api`/`orchestrator`/
`stream-analytics`; Docker-контейнеры остаются жить — `docker compose down`
отдельно, если нужно всё погасить.

## Эндпоинты

| Сервис | URL | Назначение |
|---|---|---|
| API — Swagger | http://localhost:8000/docs | интерактивная документация REST API |
| **Дашборд экспериментов** | http://localhost:8000/dashboard/ | live-таблица, фильтры, drill-down в результат |
| RabbitMQ | http://localhost:15672 (guest/guest) | management UI очереди задач |
| **Grafana** | http://localhost:3000 (admin/admin) | метрики (Prometheus) + прямые SQL-запросы к БД |
| Prometheus | http://localhost:9090 | сырые метрики/таргеты |
| **Kafka UI (Kafbat)** | http://localhost:8090 | просмотр топиков/сообщений/consumer groups |
| **Adminer** | http://localhost:8091 | ad-hoc SQL-браузер |
| Postgres | `localhost:5432` (quantum/quantum, db=`quantum_platform`) | метаданные экспериментов |
| TimescaleDB | `localhost:5433` (quantum/quantum, db=`telemetry`) | история калибровки |
| Kafka | `localhost:9092` | брокер |

## Структура

```
quantum-platform/
├── dev.sh                     # запуск всего стека, профили quick/verify
├── docker-compose.yml         # RabbitMQ, Postgres, Kafka, TimescaleDB + debug/ops-стек
├── infra/                     # конфиги Prometheus/Grafana/RabbitMQ-плагинов
├── scripts/
│   └── observe.py             # генератор нагрузки + live-наблюдение за стеком
├── services/
│   ├── quantum-core/          # библиотека: алгоритмы, hw/sw абстракция, execution
│   ├── api/                   # FastAPI: приём запросов, дашборд, Postgres-стор
│   ├── orchestrator/          # RabbitMQ-воркер: исполнение + retry + calibration
│   └── stream-analytics/      # Kafka consumer'ы (hand-rolled + Faust) + TimescaleDB sink
└── docs/
    ├── algorithms/            # физика/математика алгоритмов, независимая проверка
    └── architecture/          # архитектурные решения, ADR-style
```

Каждый сервис — свой `README.md` с деталями реализации, степенью
проверки и инструкциями запуска по отдельности.

## Документация

### Алгоритмы (`docs/algorithms/`)
- [`grover.md`](docs/algorithms/grover.md) — Grover: hello-world версия,
  настоящий SAT-поиск через `PhaseOracleGate`, ограничения (QRAM,
  BBHT-адаптивный поиск), отличие от Шора
- [`qft_qpe.md`](docs/algorithms/qft_qpe.md) — QFT/QPE, независимая
  проверка через numpy (нашла и исправила реальный баг в конвенции QFT до
  переноса в Qiskit)
- [`vqe.md`](docs/algorithms/vqe.md) — VQE на молекуле H₂, hardware-efficient
  ansatz, полная проверка measurement-based pipeline

### Архитектура (`docs/architecture/`)
- [`orchestration.md`](docs/architecture/orchestration.md) — переход с
  синхронного исполнения на RabbitMQ, retry/dead-letter policy
- [`postgres.md`](docs/architecture/postgres.md) — персистентность
  экспериментов, storage-абстракция, Alembic
- [`kafka.md`](docs/architecture/kafka.md) — телеметрия калибровки,
  hand-rolled consumer vs Faust, TimescaleDB sink
- [`dashboard.md`](docs/architecture/dashboard.md) — дашборд экспериментов,
  почему не Grafana для этой части
- [`observability.md`](docs/architecture/observability.md) — debug/ops-стек:
  Grafana, Prometheus, Kafka UI, Adminer
- [`deferred-work.md`](docs/architecture/deferred-work.md) — отложенные
  куски из исходного наброска (fast-control, продвинутые Faust-топологии)

### Прочее
- [`testing.md`](docs/testing.md) — подход к тестированию, включая то,
  как тесты проверялись без доступа к pytest в рабочей среде

## Тесты

```bash
./dev.sh --profile=verify        # все сервисы разом, до старта стека
# или по отдельности:
cd services/quantum-core && pytest tests/ -v
cd services/api && pytest tests/ -v
cd services/stream-analytics && pytest tests/ -v
```

## Архитектура вкратце

```
                     ┌─────────────┐
  POST /experiments  │     api     │  GET /experiments (фильтры/сортировка/stats)
  ──────────────────▶│  (FastAPI)  │◀────────────────── дашборд (static/dashboard/)
                      └──────┬──────┘
                             │ publish task           ▲ apply result
                             ▼                         │
                      ┌─────────────┐           ┌──────┴──────┐
                      │  RabbitMQ   │──────────▶│ orchestrator │
                      │ (queue)     │  consume  │  (worker)    │
                      └─────────────┘           └──────┬──────┘
                                                         │ execute via quantum_core
                                                         ▼
                                                  ┌──────────────┐
                                                  │ QuantumBackend│ (Aer simulator)
                                                  └──────┬───────┘
                                                         │ calibration cycle
                                                         ▼
                                                  ┌─────────────┐
                                                  │    Kafka     │  calibration-results
                                                  └──────┬──────┘
                                       ┌─────────────────┴────────────────┐
                                       ▼                                  ▼
                              ┌────────────────┐                ┌─────────────────┐
                              │ stream-analytics│                │  stream-analytics│
                              │ (hand-rolled)   │                │     (Faust)      │
                              └────────┬────────┘                └─────────────────┘
                                       ▼
                                ┌─────────────┐
                                │ TimescaleDB │ ──▶ Grafana
                                └─────────────┘

Postgres (experiments metadata) ──▶ api (store) + Grafana + Adminer
```

## Честная оговорка про степень проверки

Этот проект собирался в паре с Claude, преимущественно в среде без
доступа к Docker/сети — многое (Qiskit-математика, чистая Python-логика)
проверялось независимо перед тем, как код попадал в репозиторий; многое
другое (RabbitMQ, Kafka, Postgres, Grafana) — нет, и было проверено уже
здесь, вручную, с несколькими найденными и исправленными по пути
реальными багами (несовпадение timezone в SQLAlchemy-модели, неверный
способ включения RabbitMQ-плагина, устаревший образ Kafka UI и другие —
подробности в соответствующих `docs/architecture/*.md`). Это осознанный
и честно задокументированный процесс, а не показатель низкого качества —
смотри пометки "⚠️ Степень проверки" в каждом архитектурном документе.