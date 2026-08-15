# Debug/ops stack: Grafana, Prometheus, Kafka UI, Adminer

## Why separate from the experiments dashboard

See `docs/architecture/dashboard.md` — it covers why viewing experiment
records (business/research data) isn't a good fit for Grafana. This
document covers the opposite case: operational visibility (queue depth,
consumer lag, error rates over time) — exactly what Grafana was intended
for in this project from the start (see the very first architecture
conversation).

But Grafana alone isn't enough here either — it visualizes metrics, not
raw messages or SQL tables. So the stack is four tools, each with its
own role:

| Tool | Role | Port |
|---|---|---|
| **Prometheus** | Time-series metrics store, HTTP scrape | 9090 |
| **Grafana** | Metrics visualization + direct SQL queries against Postgres/TimescaleDB | 3000 |
| **Kafbat UI** | Browse Kafka topics/messages/consumer groups | 8090 |
| **Adminer** | Ad-hoc SQL browser for Postgres/TimescaleDB | 8091 |

## Metric sources

- **RabbitMQ** — the built-in `rabbitmq_prometheus` plugin, port 15692.
  Enabled via a mounted `infra/rabbitmq/enabled_plugins`
  (`[rabbitmq_management,rabbitmq_prometheus].`) — **not** via the
  `RABBITMQ_SERVER_ADDITIONAL_ERL_ARGS` environment variable some blogs
  suggest: that's just an Erlang VM argument and doesn't actually enable
  the plugin (checked against RabbitMQ's official docs before picking
  the right approach).
- **Kafka** — `kafka_exporter` (danielqsj/kafka-exporter), talks the
  regular broker protocol (`--kafka.server=kafka:9092`), doesn't depend
  on Zookeeper/KRaft — compatible with our single-broker KRaft setup
  with no caveats.
- **Postgres** (experiment metadata) and **TimescaleDB** (telemetry) —
  two separate `postgres_exporter` instances, each pointed at its own
  DB (`DATA_SOURCE_NAME`) — these are different containers, different
  datasets, not one server with two databases.

## Kafka UI: an important image choice

Used `ghcr.io/kafbat/kafka-ui`, **not** `provectuslabs/kafka-ui` (the
original project). Provectus paused development in September 2023 and
left an unpatched RCE vulnerability sitting for roughly six months
before the community forked it as `kafbat/kafka-ui` — that's now the
actively maintained continuation. Same pattern as `faust` →
`faust-streaming` elsewhere in this project: checked currency/support
before taking on a dependency, rather than going with whatever memory
suggested first.

## Grafana: what's automated, what's manual

**Automated** (via `infra/grafana/provisioning/`):
- 3 datasources: Prometheus, Postgres (experiments), TimescaleDB
  (telemetry) — connected and ready to query immediately after startup,
  with zero clicks in the UI.

**Manual** (deliberately not automated):
- The dashboards themselves (panels/graphs) — provisioning for
  dashboards is set up (`infra/grafana/provisioning/dashboards/`, folder
  currently empty), but a ready-made dashboard JSON is a version-
  sensitive thing I can't test in my environment (no Grafana). Rather
  than risk a broken hand-crafted JSON, it's simpler to import a
  ready-made community dashboard through the UI:
  - **Kafka Exporter Overview** — Grafana Dashboard ID `7589`
    (Import → By ID → `7589` → pick the `Prometheus` datasource);
  - For RabbitMQ and Postgres/TimescaleDB — search Grafana's "Import
    dashboard" by exporter name; there are dozens of ready community
    dashboards with varying degrees of currency — pick one with a
    recent update.
- Custom panels for `calibration_events` (e.g. "error_rate over the
  last hour") — fastest to build via Grafana Explore (datasource
  `TimescaleDB (telemetry)`), then save as a panel once it looks right.

## How to run it

Everything comes up together with the rest of the stack:
```bash
./dev.sh
```

⚠️ Grafana/Prometheus/the exporters don't have an exec-based healthcheck
I've verified (unlike `pg_isready`/`rabbitmq-diagnostics`, which are
already used and confirmed) — `dev.sh` doesn't strictly wait for them to
be ready, it just prints the addresses at the end. They usually come up
within a few seconds after the rest of the stack is already ready.

## Kafka: dual listener (a bug found and fixed)

The first version of the configuration used a single listener
(`PLAINTEXT://localhost:9092`) — this worked for this project's
host processes (`api`/`orchestrator`/`stream-analytics`/
`scripts/observe.py` always run on the host, never dockerized), but
**not** for `kafka-exporter`/`kafka-ui`, which are the first things in
this project to talk to each other over the Docker network between
containers. The failure mechanism: after the initial handshake, Kafka
tells the client an "advertised" address to use going forward; if that
address is `localhost:9092`, a container-side client (for which
`localhost` means itself) tries to reconnect to itself and fails right
after the handshake.

This is exactly the risk flagged ahead of time (before the real run) —
"critical setting is `KAFKA_ADVERTISED_LISTENERS`" — and it's now been
confirmed in practice. Fixed with two separate listeners:
`PLAINTEXT_INTERNAL` (advertised as `kafka:29092`, for containers) and
`PLAINTEXT_EXTERNAL` (advertised as `localhost:9092`, for host
processes, as before). `kafka-exporter`/`kafka-ui` were switched to
`kafka:29092`; nothing needed to change on the host processes — they
were already going through `localhost:9092`, which is unchanged.

## Troubleshooting if there's still no data

**Prometheus** (http://localhost:9090/targets) — check here first: it
shows the UP/DOWN status for every target plus the exact text of the
last scrape error, instead of guessing.

If a specific target is still DOWN:
- `docker compose logs <service>` — almost always has the exact cause;
- **RabbitMQ**: `docker compose logs rabbitmq | grep -i prometheus` —
  there should be a line about loading `rabbitmq_prometheus`. If not,
  the `infra/rabbitmq/enabled_plugins` file didn't get mounted (check
  that it actually exists as a file, not an empty directory that Docker
  created — this can happen if the host path didn't exist at
  `docker compose up` time). Check directly:
  `curl http://localhost:15692/metrics`;
- **postgres-exporter/timescaledb-exporter**: `DATA_SOURCE_NAME` doesn't
  need the dual-listener approach (the Postgres wire protocol doesn't do
  advertised-address redirects, unlike Kafka) — if it still doesn't
  work, the most likely cause is `depends_on: condition:
  service_healthy` not having been satisfied (check `docker compose ps`
  to confirm the DB is actually healthy, not just running).

**Grafana "no data"** isn't always the same thing as "Prometheus isn't
collecting metrics." Both can be true at once:
1. There's genuinely no data in Prometheus (see above) — then Grafana
   physically has nothing to show;
2. There's data in Prometheus, but Grafana simply **has no dashboard at
   all** — I deliberately didn't pre-provision dashboard JSON (see "what's
   manual" above). To check: Configuration → Data Sources → open each
   one → "Save & Test" button — a green checkmark means the datasource
   is connected and the issue is a missing dashboard, not connectivity.

## ⚠️ Degree of verification

The first version of this stack was written without Docker access in my
environment — whatever could be checked statically, was checked (all
YAML validated via `pyyaml`; three spots with a risk of copying a
non-working pattern from blog posts were separately verified before
writing the code):
1. How to enable `rabbitmq_prometheus` — the common blog ENV-variable
   trick doesn't work; used the officially documented approach (the
   `enabled_plugins` file);
2. `kafka_exporter` compatibility with KRaft — confirmed (works over the
   broker protocol, not tied to Zookeeper);
3. Currency of the Kafka UI image — the original
   `provectuslabs/kafka-ui` is abandoned with a known unpatched
   vulnerability; used the maintained `kafbat/kafka-ui` fork instead.

On the first real run (already against real Docker), a fourth, more
serious bug turned up — the Kafka dual-listener issue (see above) —
which couldn't have been caught by static checking in principle, only
by an actual run: this is the first code in the project where containers
genuinely talk to each other over the Docker network, rather than via
`localhost` from the host.  Fixed and documented above.

Run `docker compose up -d` (or `./dev.sh`) again with the fixed
`docker-compose.yml`, then open http://localhost:9090/targets — all 5
targets should show UP. If something is still DOWN, see the
troubleshooting section above.
