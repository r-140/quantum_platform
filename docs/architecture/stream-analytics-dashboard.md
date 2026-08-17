# stream-analytics dashboard

A second custom frontend in this project, alongside the experiments
dashboard (`docs/architecture/dashboard.md`) — this one visualizes the
Faust alerting/drift-detection topology from
`docs/architecture/kafka.md` rather than experiment records.

## Why not Grafana

`docs/architecture/observability.md` already draws this line for
operational telemetry generally: Grafana is the right tool once metrics
land somewhere it can query (Prometheus, Postgres, TimescaleDB). Today,
`calibration-alerts`/`calibration-drift-alerts` only exist in Kafka —
there's no datasource Grafana can read them through, unlike raw
calibration events (which `timescale_sink.py` persists specifically so
Grafana *can* see them). Piping alert/drift events into TimescaleDB the
same way remains a reasonable future option; this dashboard instead
reads the live Faust `Table` state directly, with no intermediate store.

## Why React, not another vanilla-JS file

The experiments dashboard is deliberately a single hand-rolled HTML/JS
file with no build step. This dashboard keeps the "no build step"
property (loaded via CDN, JSX transformed in-browser by Babel Standalone
— no npm, no bundler, no `node_modules`) but uses real React components
rather than growing a second hand-rolled vanilla-JS file — specifically
so new per-metric widgets can be added as new components as this
project's Faust analytics grow, rather than each new metric adding more
manual DOM manipulation to an already-dense script.

**Stated trade-off**: loading React/Babel from `unpkg.com` means this
page needs network access at load time — unlike the rest of this
project's frontend code. A fully offline-capable version would need a
real bundled build (Vite, etc.), deliberately not done here to preserve
the no-build-step property for what's internal tooling, not a public
product. Worth revisiting if this ever needs to run somewhere
network-isolated.

## Where it lives

Served from the **Faust worker's own built-in web server** (the same
one that binds port 6066 by default) — not a new service, not the `api`
service either. Same-origin with its own API, so no CORS setup is
needed, mirroring the reasoning in `dashboard.md` for why the
experiments dashboard is served from `api` itself.

- `GET /api/state` — a JSON snapshot: per-backend `window_avg`,
  threshold/drift alert levels and streak counters, baseline
  mean/stddev, plus a bounded (50-item) recent-events feed;
- `GET /dashboard/` — the React page itself
  (`static/dashboard/index.html`), which polls `/api/state` every 3
  seconds — the same polling interval and pattern the experiments
  dashboard uses, for consistency.

Both routes use Faust's `@app.page()` decorator — the same mechanism
Faust's own documentation uses for exposing table state via HTTP (e.g.
its word-count tutorial). `alert_state` (a Faust `Table`, written
unconditionally on every processed event) doubles as the backend
registry for `/api/state` — there's no separate "known backends" list
maintained anywhere else.

## Visual design

Deliberately reuses the experiments dashboard's existing token system
(`docs/architecture/dashboard.md`: background `#0B0E14`, panels
`#12161F`, accent `#4FD1E8`, JetBrains Mono for data, Space Grotesk for
UI chrome) rather than inventing a new identity — this is the same
product's telemetry page, not a separate brief. The one new element
specific to this page: a small pulsing live-indicator dot per backend
card (red when either the threshold or drift alert is active), since
this page is about *streaming* state in a way the experiments dashboard
(a static table of finished records) isn't — and a left-border-colored
scrolling transitions feed, evoking the same "control room" register.

## ⚠️ Degree of verification

The Python side (`@app.page` routes, reading `alert_state.items()` as
the backend registry, computing `window_avg` via
`error_rate_sum[key].now()`/`sample_count[key].now()` from inside a web
handler rather than the agent's own stream context) is **unverified**
against real Faust — no `faust-streaming` install in my environment.
`Table.items()` for a non-windowed table, and calling `.now()` on a
windowed table's per-key view from a web handler, are both standard,
documented Faust patterns, but neither has been run here. If
`/api/state` 404s, hangs, or comes back empty despite the worker log
showing processed events, this route is the first place to look — not
the alerting/drift logic itself, which is independently verified
separately (see `docs/architecture/kafka.md`).

The frontend (`static/dashboard/index.html`) was checked by hand —
every JSX conditional/`.map()`/component closes correctly on manual
review — but **not run through an actual Babel compile or a real
browser**: no network access to install `@babel/core` locally to
compile-check it, and no browser in this environment either. Open
`http://localhost:6066/dashboard/` yourself and check the browser
console for any JSX/runtime errors before trusting it fully.

## How to run it

Comes up automatically with the Faust worker — no separate step:

```bash
cd services/stream-analytics
source .venv/bin/activate
faust -A app.faust_app worker -l info
```

Then open `http://localhost:6066/dashboard/` (or whatever host:port the
worker's startup banner shows under "web"). Expect an empty state
until orchestrator's calibration cycle publishes at least one event —
every 5 minutes by default (`CALIBRATION_INTERVAL_S`), or sooner if
you've lowered it for testing (see `docs/architecture/kafka.md`).
