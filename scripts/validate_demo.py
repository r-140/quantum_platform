#!/usr/bin/env python3
"""Validate the complete local quantum-platform demo with one command.

The full stack must already be running through ``./dev.sh``.  The validator
uses only Python's standard library plus the Docker CLI already required by
the project.  It exercises real service boundaries rather than importing
application internals.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Callable
from uuid import UUID


REPO_ROOT = Path(__file__).resolve().parent.parent
COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"
BACKEND = "aer-simulator"


class ValidationError(RuntimeError):
    """An expected part of the demo pipeline did not work."""


def request_json(
    url: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
) -> tuple[int, Any]:
    body = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        url,
        data=body,
        method=method,
        headers={"Content-Type": "application/json"} if body is not None else {},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:
        try:
            error_body = json.load(exc)
        except (json.JSONDecodeError, UnicodeDecodeError):
            error_body = exc.read().decode(errors="replace")
        return exc.code, error_body
    except urllib.error.URLError as exc:
        raise ValidationError(f"cannot reach {url}: {exc.reason}") from exc


def require_health(url: str, *, expected_status: str | None = None) -> Any:
    status, response = request_json(url)
    if status != 200:
        raise ValidationError(f"health request {url} returned HTTP {status}: {response}")
    if expected_status is not None and response.get("status") != expected_status:
        raise ValidationError(f"unexpected health response from {url}: {response}")
    return response


def submit(api_url: str, payload: dict[str, Any]) -> str:
    status, response = request_json(
        f"{api_url}/experiments", method="POST", payload=payload
    )
    if status != 202:
        raise ValidationError(f"submission returned HTTP {status}: {response}")
    experiment_id = response.get("id")
    try:
        UUID(experiment_id)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"invalid experiment id in response: {response}") from exc
    print(f"  submitted {payload['algorithm']} id={experiment_id}")
    return experiment_id


def get_experiment(api_url: str, experiment_id: str) -> dict[str, Any]:
    status, experiment = request_json(f"{api_url}/experiments/{experiment_id}")
    if status != 200:
        raise ValidationError(
            f"reading experiment {experiment_id} returned HTTP {status}: {experiment}"
        )
    return experiment


def wait_for_completion(
    api_url: str,
    experiment_ids: list[str],
    *,
    deadline: float,
    poll_interval: float,
) -> dict[str, dict[str, Any]]:
    pending = set(experiment_ids)
    completed: dict[str, dict[str, Any]] = {}
    while pending and time.monotonic() < deadline:
        for experiment_id in list(pending):
            experiment = get_experiment(api_url, experiment_id)
            state = experiment.get("status")
            if state == "failed":
                raise ValidationError(
                    f"experiment {experiment_id} failed: {experiment.get('error')}"
                )
            if state == "completed":
                completed[experiment_id] = experiment
                pending.remove(experiment_id)
                print(f"  completed {experiment['algorithm']} id={experiment_id}")
        if pending:
            time.sleep(poll_interval)
    if pending:
        raise ValidationError(
            "timed out waiting for experiments: " + ", ".join(sorted(pending))
        )
    return completed


def compose_psql(service: str, database: str, sql: str) -> str:
    command = [
        "docker",
        "compose",
        "-f",
        str(COMPOSE_FILE),
        "exec",
        "-T",
        service,
        "psql",
        "-U",
        "quantum",
        "-d",
        database,
        "-v",
        "ON_ERROR_STOP=1",
        "-Atqc",
        sql,
    ]
    try:
        result = subprocess.run(
            command, check=True, capture_output=True, text=True, cwd=REPO_ROOT
        )
    except FileNotFoundError as exc:
        raise ValidationError("docker CLI is not installed or not on PATH") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout).strip()
        raise ValidationError(f"database command failed: {detail}") from exc
    return result.stdout.strip()


def make_calibration_stale() -> None:
    updated = compose_psql(
        "postgres",
        "quantum_platform",
        "UPDATE backend_calibration_state "
        "SET observed_at=now()-interval '1 day' "
        f"WHERE backend_name='{BACKEND}'; "
        "SELECT count(*) FROM backend_calibration_state "
        f"WHERE backend_name='{BACKEND}';",
    )
    if not updated.splitlines() or updated.splitlines()[-1] != "1":
        raise ValidationError(f"no calibration row found for {BACKEND}")


def validate_calibration_gate(
    api_url: str,
    *,
    deadline: float,
    poll_interval: float,
) -> tuple[str, dict[str, Any]]:
    status, before = request_json(f"{api_url}/backends/{BACKEND}/calibration")
    if status != 200:
        raise ValidationError(f"no initial calibration observation: {before}")
    if before.get("error_rate", 1.0) >= 0.10:
        raise ValidationError(
            f"backend starts unhealthy: error_rate={before.get('error_rate')}"
        )

    make_calibration_stale()
    experiment_id = submit(
        api_url,
        {
            "algorithm": "vqe",
            "molecule": "h2",
            "shots": 128,
            "max_iterations": 1,
        },
    )

    saw_waiting = False
    experiment: dict[str, Any] = {}
    while time.monotonic() < deadline:
        experiment = get_experiment(api_url, experiment_id)
        state = experiment.get("status")
        if state == "waiting_for_calibration" and not saw_waiting:
            saw_waiting = True
            print("  observed waiting_for_calibration")
        if state == "failed":
            raise ValidationError(
                f"calibration-gated VQE failed: {experiment.get('error')}"
            )
        if state == "completed":
            break
        time.sleep(min(poll_interval, 0.25))
    else:
        raise ValidationError("calibration-gated VQE did not complete before timeout")

    if not saw_waiting:
        raise ValidationError("VQE completed without exposing waiting_for_calibration")
    status, after = request_json(f"{api_url}/backends/{BACKEND}/calibration")
    if status != 200 or after.get("observed_at") == before.get("observed_at"):
        raise ValidationError(f"calibration observation was not refreshed: {after}")
    print("  calibration refreshed and VQE resumed")
    return experiment_id, experiment


def require_algorithm_results(
    *,
    grover: dict[str, Any],
    qpe: dict[str, Any],
    vqe: dict[str, Any],
) -> None:
    grover_result = grover.get("result") or {}
    counts = grover_result.get("counts") or {}
    if not counts or max(counts, key=counts.get) != "101":
        raise ValidationError(f"Grover did not favor marked state 101: {counts}")

    qpe_result = qpe.get("result") or {}
    estimates = qpe_result.get("results") or {}
    if not estimates:
        raise ValidationError(f"QPE returned no phase estimates: {qpe_result}")
    dominant = max(estimates.values(), key=lambda item: item["count"])
    resolution = qpe_result.get("resolution", 0.0)
    if abs(dominant["phi_estimate"] - 0.125) > resolution:
        raise ValidationError(f"QPE dominant estimate is unexpected: {qpe_result}")

    vqe_result = vqe.get("result") or {}
    if vqe_result.get("molecule") != "h2" or not vqe_result.get("history"):
        raise ValidationError(f"VQE result is incomplete: {vqe_result}")
    print("  Grover marked state, QPE phase, and VQE history are valid")


def wait_until(
    description: str,
    check: Callable[[], Any],
    *,
    deadline: float,
    poll_interval: float,
) -> Any:
    last_value: Any = None
    while time.monotonic() < deadline:
        last_value = check()
        if last_value:
            return last_value
        time.sleep(poll_interval)
    raise ValidationError(f"timed out waiting for {description}; last value={last_value!r}")


def wait_for_duplicate(
    api_url: str,
    *,
    source_id: str,
    duplicate_id: str,
    minimum_similarity: float,
    deadline: float,
    poll_interval: float,
) -> float:
    def lookup() -> float | None:
        status, response = request_json(
            f"{api_url}/experiments/{source_id}/similar?limit=10"
        )
        if status in {404, 503}:
            return None
        if status != 200 or not isinstance(response, list):
            raise ValidationError(f"similarity lookup returned HTTP {status}: {response}")
        match = next(
            (item for item in response if item.get("experiment_id") == duplicate_id),
            None,
        )
        if match is None:
            return None
        similarity = match.get("similarity")
        if not isinstance(similarity, (int, float)):
            raise ValidationError(f"similarity is not numeric: {match}")
        if similarity < minimum_similarity:
            raise ValidationError(
                f"duplicate similarity {similarity:.6f} is below "
                f"required {minimum_similarity:.6f}"
            )
        return float(similarity)

    return wait_until(
        "pgvector duplicate indexing",
        lookup,
        deadline=deadline,
        poll_interval=poll_interval,
    )


def metric_count(table: str, experiment_id: str) -> int:
    if table not in {"vqe_iteration_metrics", "vqe_window_metrics"}:
        raise ValueError(f"unsupported metrics table: {table}")
    UUID(experiment_id)
    output = compose_psql(
        "timescaledb",
        "telemetry",
        f"SELECT count(*) FROM {table} WHERE experiment_id='{experiment_id}';",
    )
    try:
        return int(output.splitlines()[-1])
    except (IndexError, ValueError) as exc:
        raise ValidationError(f"unexpected count returned for {table}: {output!r}") from exc


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", default="http://localhost:8000")
    parser.add_argument("--faust-url", default="http://localhost:6066")
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--poll-interval", type=float, default=0.5)
    parser.add_argument("--minimum-similarity", type=float, default=0.90)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    api_url = args.api_url.rstrip("/")
    faust_url = args.faust_url.rstrip("/")
    deadline = time.monotonic() + args.timeout

    try:
        print("[1/8] Checking API and Faust health")
        require_health(f"{api_url}/health", expected_status="ok")
        require_health(f"{faust_url}/api/state")

        print("[2/8] Exercising stale-calibration VQE gating and resumption")
        vqe_id, vqe = validate_calibration_gate(
            api_url, deadline=deadline, poll_interval=args.poll_interval
        )

        print("[3/8] Submitting Grover duplicate pair and QPE")
        grover_payload = {
            "algorithm": "grover",
            "marked_states": ["101"],
            "shots": 512,
        }
        grover_id = submit(api_url, grover_payload)
        duplicate_id = submit(api_url, grover_payload)
        qpe_id = submit(
            api_url,
            {
                "algorithm": "qpe",
                "phi": 0.125,
                "num_counting_qubits": 3,
                "shots": 512,
            },
        )

        print("[4/8] Waiting for algorithms and validating their results")
        completed = wait_for_completion(
            api_url,
            [grover_id, duplicate_id, qpe_id],
            deadline=deadline,
            poll_interval=args.poll_interval,
        )
        require_algorithm_results(
            grover=completed[grover_id], qpe=completed[qpe_id], vqe=vqe
        )

        print("[5/8] Waiting for Kafka indexing and pgvector search")
        similarity = wait_for_duplicate(
            api_url,
            source_id=grover_id,
            duplicate_id=duplicate_id,
            minimum_similarity=args.minimum_similarity,
            deadline=deadline,
            poll_interval=args.poll_interval,
        )
        print(f"  duplicate cosine similarity={similarity:.6f}")

        print("[6/8] Checking raw VQE metrics in TimescaleDB")
        raw_count = wait_until(
            "raw VQE metrics",
            lambda: metric_count("vqe_iteration_metrics", vqe_id),
            deadline=deadline,
            poll_interval=args.poll_interval,
        )
        print(f"  raw iteration rows={raw_count}")

        print("[7/8] Checking Faust window metrics in TimescaleDB")
        window_count = wait_until(
            "Faust VQE window metrics",
            lambda: metric_count("vqe_window_metrics", vqe_id),
            deadline=deadline,
            poll_interval=args.poll_interval,
        )
        print(f"  window aggregate rows={window_count}")

        print("[8/8] Demo validation complete")
        print(
            "PASS: API, orchestration, calibration gate, Grover, QPE, VQE, "
            "pgvector, raw telemetry, and Faust window telemetry are healthy"
        )
        return 0
    except (ValidationError, OSError, ValueError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        print(
            "Check .dev-logs/orchestrator.log, stream-analytics.log, "
            "stream-analytics-faust.log, and result-indexer.log.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
