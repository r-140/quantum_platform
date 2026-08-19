# Messaging: why the platform uses RabbitMQ and Kafka

The platform has two different messaging problems. Using one broker for both
would obscure their different delivery and consumption semantics.

| Concern | RabbitMQ | Kafka |
|---|---|---|
| Primary role | Commands and results | Telemetry and derived event streams |
| Typical message | “Run experiment X once” | “Iteration N took 1.27 s” |
| Consumption | Competing worker consumes a task | Independent consumer groups replay a log |
| Completion | Explicit ack removes task from active queue | Offset records consumer progress |
| Retention | Until acknowledged/expired | Retained by topic policy |
| Main value | Work distribution and redelivery | Fan-out, replay, stream processing |

## RabbitMQ command path

```text
POST /experiments
       |
       v
experiments queue
       |
       v
orchestrator worker
       |
       v
experiment-results queue
       |
       v
API result consumer -> ExperimentStore
```

The API first stores a `queued` experiment and then publishes an
`ExperimentTask`. The orchestrator executes it and publishes an
`ExperimentResultMessage`. The API consumes that result and changes the stored
status to `completed` or `failed`.

RabbitMQ is appropriate because a task should be handled by one worker and
acknowledged only after a result message has been produced. Malformed messages
go directly to `experiments.dlq`. Worker-level redelivery is bounded; an
algorithm or backend failure is instead a valid failed result and is not an
infinite task retry.

## Kafka telemetry path

Kafka carries facts that may have several consumers and remain useful after
their first processing:

- `calibration-results` — backend calibration observations;
- `calibration-alerts` — derived alert-state changes;
- `vqe-iteration-metrics` — raw optimizer-iteration telemetry;
- `vqe-window-metrics` — Faust-derived window aggregates.

The hand-written analytics consumer, Faust, TimescaleDB sink, diagnostic tools,
and future result-interpreter service can use different consumer groups and
read the same event independently.

## Planned experiment-completed event

The AI interpretation feature should not consume the RabbitMQ result queue.
That queue is an API state-update command path, so adding a competing consumer
could steal messages from the API. Instead, the API or orchestrator will emit a
separate Kafka event after completion:

```json
{
  "schema_version": 1,
  "experiment_id": "...",
  "algorithm": "vqe",
  "completed_at": "...",
  "result": {"molecule": "h2", "total_energy": -1.14}
}
```

An `experiment-completed` topic gives the interpreter replayability and keeps
experiment execution independent of the availability or latency of an LLM.
Interpretation is an eventually consistent enrichment: a completed experiment
must remain completed even if the LLM provider is unavailable.

## Message contracts

RabbitMQ envelopes live in `quantum_core/tasks.py` as framework-neutral
dataclasses. Kafka events use explicit JSON fields. New durable topics should
include `schema_version` from their first release; optional additive fields are
preferred over silent semantic changes.

For VQE, molecule identity, geometry/mapping identifier, qubit count, and the
energy-history schema are part of the interpretation contract. Without them,
an agent cannot judge whether an energy is physically reasonable.

## Delivery guarantees and idempotency

The system should assume at-least-once delivery:

- RabbitMQ can redeliver after a worker crash;
- Kafka consumers can replay after an offset rollback or a failure between a
  database write and offset commit;
- Faust table updates are not automatically atomic with unrelated external
  database writes.

Consumers therefore use stable keys and idempotent writes. The planned
interpretation record should be unique on `(experiment_id, interpreter_version)`
and written with an upsert. Reprocessing an event must update the same record,
not create duplicate summaries or vectors.

## Operational separation

RabbitMQ queue depth indicates unprocessed work. Kafka consumer lag indicates
how far a stream processor is behind the retained log. They are not
interchangeable health signals and are exposed separately through Prometheus
and Grafana.
