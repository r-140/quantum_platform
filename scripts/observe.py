"""
Generates experiment load against the running API, polls status via GET,
tails the calibration-results Kafka topic, and periodically snapshots
Postgres (experiment counts by status/algorithm) and TimescaleDB
(calibration event stats) -- all four running concurrently in one process,
so you can watch the whole stack react to load in real time in a single
terminal.

This is a demo/observability tool, not a load-testing tool in the
rigorous sense (no latency percentiles, no ramp-up profiles) -- it exists
to make the system's behavior under varied load *visible*, which is what
actually matters when explaining this project's architecture to someone
else. "Varied load" here comes naturally from the algorithm mix: Grover/
SAT-Grover/QPE resolve in under a second, VQE takes much longer (dozens of
optimizer iterations, several circuits each) -- submitting a mix makes the
orchestrator's single-worker, one-task-at-a-time queue (see
docs/architecture/orchestration.md) visibly back up behind a VQE run,
which is worth seeing happen rather than just reading about.

Requires the full stack running (`./dev.sh` from the repo root) --
this script only talks to already-running services, it doesn't start any
of them.

Run with (from repo root, after `pip install -r scripts/requirements.txt`
in a venv):
    python3 scripts/observe.py
    python3 scripts/observe.py --rate 2.0 --duration 30 --vqe-weight 0.3
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import random
import time
from dataclasses import dataclass, field

import asyncpg
import httpx
from aiokafka import AIOKafkaConsumer

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger("observe")

# httpx (and its underlying httpcore) log every single request/response at
# INFO level by default -- with a poller hitting GET every second for
# every tracked experiment, this drowns out the actually useful
# [submit]/[status]/[kafka]/[postgres]/[timescale] lines. Silencing these
# specifically (not the root logger) keeps our own INFO logging intact.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

API_URL = os.environ.get("API_URL", "http://localhost:8000")
KAFKA_BOOTSTRAP_SERVERS = os.environ.get("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
POSTGRES_DSN = os.environ.get(
    "POSTGRES_DSN", "postgresql://quantum:quantum@localhost:5432/quantum_platform"
)
TIMESCALE_DSN = os.environ.get(
    "TIMESCALE_DSN", "postgresql://quantum:quantum@localhost:5433/telemetry"
)
CALIBRATION_TOPIC = "calibration-results"


def _random_bitstring(n: int) -> str:
    return "".join(random.choice("01") for _ in range(n))


def build_payload(algorithm: str) -> dict:
    """One payload builder per algorithm. VQE's `max_iterations` is
    deliberately lower than the API default (80) -- at 80 a single VQE
    submission takes roughly a minute, which would dominate this script's
    output and make the "varied load" story harder to see unfold; 20
    keeps it in the same conversation without changing what's being
    demonstrated (the orchestrator still runs a real classical-quantum
    feedback loop, just fewer iterations of it).
    """
    if algorithm == "grover":
        return {"algorithm": "grover", "marked_states": [_random_bitstring(3)]}
    if algorithm == "sat_grover":
        return {
            "algorithm": "sat_grover",
            "variables": ["x0", "x1", "x2"],
            "expression": "(x0 | x1) & ~x2",
        }
    if algorithm == "qpe":
        return {"algorithm": "qpe", "phi": round(random.random(), 3)}
    if algorithm == "vqe":
        return {"algorithm": "vqe", "max_iterations": 20}
    raise ValueError(f"unknown algorithm {algorithm!r}")


@dataclass
class TrackedExperiment:
    id: str
    algorithm: str
    submitted_at: float
    status: str = "queued"
    resolved_at: float | None = None


@dataclass
class SharedState:
    tracked: dict[str, TrackedExperiment] = field(default_factory=dict)
    stop: asyncio.Event = field(default_factory=asyncio.Event)


async def load_generator(
    client: httpx.AsyncClient,
    state: SharedState,
    *,
    rate: float,
    duration: float,
    weights: dict[str, float],
) -> None:
    """Submits experiments at roughly `rate` per second for `duration`
    seconds, picking an algorithm per submission according to `weights`.
    """
    algorithms = list(weights.keys())
    algorithm_weights = list(weights.values())
    interval = 1.0 / rate if rate > 0 else 1.0
    end_time = time.monotonic() + duration

    logger.info("load generator: rate=%.2f/s duration=%.0fs weights=%s", rate, duration, weights)

    while time.monotonic() < end_time and not state.stop.is_set():
        algorithm = random.choices(algorithms, weights=algorithm_weights, k=1)[0]
        payload = build_payload(algorithm)
        try:
            response = await client.post("/experiments", json=payload, timeout=10.0)
            response.raise_for_status()
            body = response.json()
            state.tracked[body["id"]] = TrackedExperiment(
                id=body["id"], algorithm=algorithm, submitted_at=time.monotonic()
            )
            logger.info("[submit] %s id=%s", algorithm, body["id"])
        except httpx.HTTPError as exc:
            logger.error("[submit] failed for %s: %s", algorithm, exc)

        await asyncio.sleep(interval)

    logger.info("load generator: done submitting")


async def status_poller(client: httpx.AsyncClient, state: SharedState, *, poll_interval: float = 1.0) -> None:
    """Polls GET /experiments/{id} for every tracked experiment not yet in
    a terminal state, printing a line the moment it transitions. Keeps
    running until explicitly stopped (state.stop) -- the caller decides
    when there's nothing more worth waiting for (see main()).
    """
    while not state.stop.is_set():
        pending = [t for t in state.tracked.values() if t.status == "queued"]
        for tracked in pending:
            try:
                response = await client.get(f"/experiments/{tracked.id}", timeout=10.0)
                response.raise_for_status()
                body = response.json()
            except httpx.HTTPError as exc:
                logger.error("[poll] failed for id=%s: %s", tracked.id, exc)
                continue

            if body["status"] != "queued":
                tracked.status = body["status"]
                tracked.resolved_at = time.monotonic()
                elapsed = tracked.resolved_at - tracked.submitted_at
                logger.info(
                    "[status] %s id=%s -> %s (%.2fs)",
                    tracked.algorithm,
                    tracked.id,
                    tracked.status,
                    elapsed,
                )

        await asyncio.sleep(poll_interval)


async def kafka_tailer(state: SharedState) -> None:
    consumer = AIOKafkaConsumer(
        CALIBRATION_TOPIC,
        bootstrap_servers=KAFKA_BOOTSTRAP_SERVERS,
        group_id=f"observe-{int(time.time())}",  # unique group -- always reads new messages, doesn't compete with stream-analytics's own consumer group
        auto_offset_reset="latest",
    )
    await consumer.start()
    logger.info("[kafka] tailing %r", CALIBRATION_TOPIC)
    try:
        while not state.stop.is_set():
            # getmany(timeout_ms=...) rather than getone() wrapped in
            # asyncio.wait_for() -- that combination has a known reported
            # issue (aio-libs/aiokafka#712, an infinite-loop hang under
            # some conditions). getmany's timeout is native to the method
            # itself, no wrapper needed, and it simply returns an empty
            # dict if nothing arrived within the timeout instead of
            # requiring timeout-exception handling.
            batches = await consumer.getmany(timeout_ms=1000)
            for records in batches.values():
                for message in records:
                    payload = json.loads(message.value.decode())
                    logger.info(
                        "[kafka] backend=%s error_rate=%.4f shots=%d",
                        payload["backend_name"],
                        payload["error_rate"],
                        payload["shots"],
                    )
    finally:
        await consumer.stop()


async def db_snapshot(state: SharedState, *, interval: float = 10.0) -> None:
    """Periodically prints a summary from both Postgres (experiment counts
    by status) and TimescaleDB (calibration event stats). Opens a fresh
    connection per snapshot rather than holding a pool -- simplest thing
    that works for a low-frequency, single-consumer script like this one.
    """
    while not state.stop.is_set():
        await asyncio.sleep(interval)
        if state.stop.is_set():
            break

        try:
            pg_conn = await asyncpg.connect(POSTGRES_DSN)
            try:
                rows = await pg_conn.fetch(
                    "SELECT algorithm, status, count(*) FROM experiments "
                    "GROUP BY algorithm, status ORDER BY algorithm, status"
                )
                summary = ", ".join(f"{r['algorithm']}/{r['status']}={r['count']}" for r in rows)
                logger.info("[postgres] %s", summary or "(no experiments yet)")
            finally:
                await pg_conn.close()
        except Exception as exc:  # noqa: BLE001 -- a snapshot failure shouldn't stop the script
            logger.error("[postgres] snapshot failed: %s", exc)

        try:
            ts_conn = await asyncpg.connect(TIMESCALE_DSN)
            try:
                row = await ts_conn.fetchrow(
                    "SELECT count(*) AS n, avg(error_rate) AS avg_error_rate "
                    "FROM calibration_events"
                )
                logger.info(
                    "[timescale] events=%d avg_error_rate=%s",
                    row["n"],
                    f"{row['avg_error_rate']:.4f}" if row["avg_error_rate"] is not None else "n/a",
                )
            finally:
                await ts_conn.close()
        except Exception as exc:  # noqa: BLE001
            logger.error("[timescale] snapshot failed: %s", exc)


async def wait_for_all_resolved(
    state: SharedState, *, submitters_done: asyncio.Event, timeout: float = 180.0
) -> None:
    """Stops the whole script once the load generator has finished
    submitting AND every submitted experiment has reached a terminal
    status (or `timeout` seconds have passed, in case something is stuck
    -- e.g. the orchestrator isn't running).

    Prints a heartbeat every 10s while waiting on a backlog -- without
    this, a run with several VQE experiments queued behind a
    single-worker orchestrator (prefetch_count=1, see
    docs/architecture/orchestration.md) looks indistinguishable from a
    genuinely stuck script: nothing else prints while the queue drains,
    and a VQE run alone can take a dozen-plus seconds even with the
    reduced `max_iterations=20` this script submits with.
    """
    await submitters_done.wait()
    deadline = time.monotonic() + timeout
    last_heartbeat = time.monotonic()

    while time.monotonic() < deadline:
        pending = [t for t in state.tracked.values() if t.status == "queued"]
        if not pending:
            break

        if time.monotonic() - last_heartbeat >= 10.0:
            remaining = deadline - time.monotonic()
            logger.info(
                "[main] still waiting: %d experiment(s) queued (timeout in %.0fs) -- %s",
                len(pending),
                remaining,
                ", ".join(f"{t.algorithm}" for t in pending[:5])
                + ("..." if len(pending) > 5 else ""),
            )
            last_heartbeat = time.monotonic()

        await asyncio.sleep(1.0)
    else:
        logger.warning("[main] timed out waiting for all experiments to resolve")

    logger.info(
        "[main] all done, stopping background tasks "
        "(kafka tail / db snapshots keep running a few more seconds)"
    )
    await asyncio.sleep(5.0)  # let the last db_snapshot/kafka message land before shutdown
    state.stop.set()


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rate", type=float, default=1.0, help="experiments submitted per second")
    parser.add_argument("--duration", type=float, default=20.0, help="how long to keep submitting, in seconds")
    parser.add_argument("--grover-weight", type=float, default=0.4)
    parser.add_argument("--sat-grover-weight", type=float, default=0.3)
    parser.add_argument("--qpe-weight", type=float, default=0.2)
    parser.add_argument("--vqe-weight", type=float, default=0.1)
    parser.add_argument(
        "--max-wait",
        type=float,
        default=180.0,
        help="max seconds to wait for all submitted experiments to resolve before giving up",
    )
    args = parser.parse_args()

    weights = {
        "grover": args.grover_weight,
        "sat_grover": args.sat_grover_weight,
        "qpe": args.qpe_weight,
        "vqe": args.vqe_weight,
    }

    state = SharedState()
    submitters_done = asyncio.Event()

    async def run_load_generator(client: httpx.AsyncClient) -> None:
        await load_generator(client, state, rate=args.rate, duration=args.duration, weights=weights)
        submitters_done.set()

    async with httpx.AsyncClient(base_url=API_URL) as client:
        await asyncio.gather(
            run_load_generator(client),
            status_poller(client, state),
            kafka_tailer(state),
            db_snapshot(state),
            wait_for_all_resolved(state, submitters_done=submitters_done, timeout=args.max_wait),
        )

    logger.info("[main] finished. Tracked %d experiments.", len(state.tracked))
    by_status: dict[str, int] = {}
    for t in state.tracked.values():
        by_status[t.status] = by_status.get(t.status, 0) + 1
    logger.info("[main] final status breakdown: %s", by_status)


if __name__ == "__main__":
    asyncio.run(main())