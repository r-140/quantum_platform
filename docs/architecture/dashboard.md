# Experiments dashboard

## What it is, and why not Grafana

Separately from the question of "how do we visualize
calibration/streaming telemetry" (that's Grafana, see `kafka.md`) —
viewing/sorting/filtering experiment records (research data, not
operational metrics) deserves its own tool. Grafana can technically draw
a table from Postgres, but:
- there's no proper drill-down into a single record with a readable JSON
  result;
- Grafana's sorting/filtering is rudimentary — built for time-series
  panels, not data-browsing UX;
- it's semantically odd to use an SRE tool to browse research results.

Instead: a lightweight custom frontend served directly from `api`
(`StaticFiles`, no build step, no npm, a single self-contained HTML
file). Same-origin with the API itself — no CORS needed.

## Design

Theme: "control room / oscilloscope" — a dark lab console, monospace
readouts for data (id, time, JSON), instead of the clichéd AI defaults
(cream+terracotta or black+acid green) and instead of the typical
"blue sidebar + white cards" admin panel.

Tokens: background `#0B0E14`, panels `#12161F`, text
`#E4E7EC`/`#7B8494`, accent — a coherent cyan `#4FD1E8`. Statuses:
`queued` `#E8B44F` (a pulsing indicator — the only animated element, the
experiment "in flight"), `completed` `#4FE8A0`, `failed` `#E85F5F`.
Fonts: Space Grotesk (headings/UI chrome), JetBrains Mono (all data
without exception — id, time, duration, JSON result).

## What's implemented

**Backend** (`services/api/app/store/`, `app/routers/experiments.py`):
- `ExperimentStore.list_all()` extended with optional
  `algorithm`/`status`/`sort_desc` — filtering and sorting happen at the
  storage layer (SQL `WHERE`/`ORDER BY` for Postgres, a plain list for
  in-memory), not on the frontend — the dashboard doesn't pull the
  entire experiment list just to show one algorithm;
- `ExperimentStore.stats()` — a new method, aggregation by
  `(algorithm, status)` with counts — feeds the dashboard's summary
  header without having to fetch and locally compute the entire list on
  the client;
- `GET /experiments?algorithm=&status=&sort=asc|desc` — query params on
  top of the existing listing;
- `GET /experiments/stats` — the aggregates.

⚠️ **Route-ordering trap**: `GET /experiments/stats` **must** be
registered **before** `GET /experiments/{experiment_id}` — otherwise
FastAPI/Starlette (which matches routes in registration order) will
intercept `stats` as `experiment_id="stats"`, and the stats route will
never be reached. There's a dedicated regression test
(`test_stats_route_not_shadowed_by_experiment_id_route`) for exactly
this case.

**Frontend** (`services/api/static/dashboard/index.html`):
- A table with filters (algorithm/status), click-to-sort, and
  client-side substring search on id;
- Live updates every 3 seconds (`setInterval` + `fetch`, no
  WebSocket/SSE — the minimal sufficient solution at this scale);
- Drill-down into a specific experiment — a slide-over panel with the
  full JSON result.

## How this was verified

As everywhere else in the project — whatever could be checked without a
real stack, was checked:
- Filter/sort/`stats()` logic — run against the **real** `in_memory.py`
  (not a draft) with duck-typed stubs standing in for Pydantic (which
  isn't available in my environment) — all filter combinations, asc/desc
  sorting, aggregation;
- Dashboard JS syntax — `node --check` on the extracted `<script>`
  block;
- Pure formatting functions (`fmtDuration`, `shortId`, `totals`
  aggregation) — checked against **real data** from `observe.py` runs
  (for instance, `fmtDuration` produced exactly `25.67s` — the same
  number that showed up in your log);
- Along the way I caught a typo of my own in a test assertion (not in
  the dashboard code itself) — double-checked it by hand and flagged it
  explicitly rather than staying quiet about it.

⚠️ **Not verified**: the actual rendering in a browser (layout,
animations, real `fetch` calls against a live API) — I don't have a
browser. Open `http://localhost:8000/dashboard/` and take a look
yourself, especially while `scripts/observe.py` is running — it'll be
interesting to see how lively the 3-second polling feels under load.

## How to run it

Via `./dev.sh` (see the root `dev.sh` — now supports profiles, see
below) — the dashboard comes up together with the rest of the stack at
`http://localhost:8000/dashboard/`.

### `dev.sh` profiles

Modeled on Maven's `-P`:
```bash
./dev.sh                    # profile=quick (default) — no tests
./dev.sh --profile=verify   # runs each service's pytest suite BEFORE starting
```

`--profile=verify` stops the script **before** starting any service if
even one test suite fails — similar to how `mvn verify` won't let you
`install`/deploy if the tests don't pass. Each service is dynamically
checked for `tests/*.py` (not a hardcoded list) — a service without
tests is simply skipped, nothing fails. `quantum-core` doesn't have its
own venv step in `dev.sh` (it's installed as an editable dependency into
the other services), so its tests run under the `verify` profile through
`api`'s already-set-up venv.

## Not yet implemented

- Pagination — with a large number of experiments, `GET /experiments`
  returns the entire list at once; not a problem at demo scale yet;
- Search by id — client-side only (over the already-loaded page), not a
  server-side substring search via SQL `LIKE`;
- WebSocket/SSE instead of polling — the 3-second `setInterval` is fine
  for a demo, but doesn't scale to many simultaneous dashboard clients.
