#!/usr/bin/env python3
"""End-to-end smoke test for the pgvector experiment similarity pipeline.

Uses only Python's standard library. The full local stack must already be
running. Grover is the fast default; SAT-Grover, QPE and VQE can be selected
with ``--algorithm``.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from typing import Any


class ValidationError(RuntimeError):
    pass


def experiment_payloads(args: argparse.Namespace) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return one duplicate payload and one deliberately different payload."""
    if args.algorithm == "grover":
        common = {"algorithm": "grover", "shots": args.shots}
        return (
            {**common, "marked_states": ["101"]},
            {**common, "marked_states": ["010"]},
        )
    if args.algorithm == "sat_grover":
        common = {
            "algorithm": "sat_grover",
            "variables": ["x0", "x1"],
            "shots": args.shots,
        }
        return (
            {**common, "expression": "x0 & x1"},
            {**common, "expression": "x0 | x1"},
        )
    if args.algorithm == "qpe":
        common = {
            "algorithm": "qpe",
            "num_counting_qubits": args.qpe_counting_qubits,
            "shots": args.shots,
        }
        return ({**common, "phi": 0.125}, {**common, "phi": 0.375})
    if args.algorithm == "vqe":
        # The third run uses a different molecule, while the first two use
        # identical physical and optimizer parameters.
        distinct_molecule = "lih" if args.vqe_molecule == "h2" else "h2"
        common = {
            "algorithm": "vqe",
            "shots": args.shots,
            "max_iterations": args.vqe_max_iterations,
        }
        return (
            {**common, "molecule": args.vqe_molecule},
            {**common, "molecule": distinct_molecule},
        )
    raise ValidationError(f"unsupported algorithm: {args.algorithm}")


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


def submit(api_url: str, payload: dict[str, Any]) -> str:
    status, response = request_json(
        f"{api_url}/experiments", method="POST", payload=payload
    )
    if status != 202:
        raise ValidationError(f"submission returned HTTP {status}: {response}")
    experiment_id = response.get("id")
    if not experiment_id:
        raise ValidationError(f"submission response has no experiment id: {response}")
    print(f"  submitted {experiment_id}")
    return experiment_id


def wait_for_completion(
    api_url: str,
    experiment_ids: list[str],
    *,
    deadline: float,
    poll_interval: float,
) -> None:
    pending = set(experiment_ids)
    while pending and time.monotonic() < deadline:
        for experiment_id in list(pending):
            status, experiment = request_json(
                f"{api_url}/experiments/{experiment_id}"
            )
            if status != 200:
                raise ValidationError(
                    f"reading {experiment_id} returned HTTP {status}: {experiment}"
                )
            state = experiment.get("status")
            if state == "failed":
                raise ValidationError(
                    f"experiment {experiment_id} failed: {experiment.get('error')}"
                )
            if state == "completed":
                print(f"  completed {experiment_id}")
                pending.remove(experiment_id)
        if pending:
            time.sleep(poll_interval)
    if pending:
        raise ValidationError(
            f"timed out waiting for experiments: {', '.join(sorted(pending))}"
        )


def wait_for_duplicate_neighbour(
    api_url: str,
    *,
    source_id: str,
    duplicate_id: str,
    deadline: float,
    poll_interval: float,
    minimum_similarity: float,
) -> dict[str, Any]:
    url = f"{api_url}/experiments/{source_id}/similar?limit=10"
    last_response: Any = None
    while time.monotonic() < deadline:
        status, response = request_json(url)
        last_response = response
        if status == 200:
            if not isinstance(response, list):
                raise ValidationError(f"similarity response is not a list: {response}")
            if any(item.get("experiment_id") == source_id for item in response):
                raise ValidationError("similarity endpoint returned the source itself")
            match = next(
                (
                    item
                    for item in response
                    if item.get("experiment_id") == duplicate_id
                ),
                None,
            )
            if match is not None:
                similarity = match.get("similarity")
                if not isinstance(similarity, (int, float)):
                    raise ValidationError(f"similarity is not numeric: {match}")
                if not -1.0 <= similarity <= 1.0:
                    raise ValidationError(f"similarity is outside [-1, 1]: {match}")
                if similarity < minimum_similarity:
                    raise ValidationError(
                        f"duplicate similarity {similarity:.6f} is below "
                        f"required {minimum_similarity:.6f}"
                    )
                if not match.get("content"):
                    raise ValidationError(f"neighbour has no canonical content: {match}")
                return match
        elif status not in {404, 503}:
            raise ValidationError(
                f"similarity lookup returned HTTP {status}: {response}"
            )
        time.sleep(poll_interval)
    raise ValidationError(
        "timed out waiting for vector indexing; last response was "
        f"{last_response}. Check .dev-logs/result-indexer.log"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", default="http://localhost:8000")
    parser.add_argument(
        "--algorithm",
        choices=("grover", "sat_grover", "qpe", "vqe"),
        default="grover",
    )
    parser.add_argument("--shots", type=int, default=512)
    parser.add_argument("--qpe-counting-qubits", type=int, default=3)
    parser.add_argument(
        "--vqe-molecule", choices=("h2", "lih", "beh2"), default="h2"
    )
    parser.add_argument("--vqe-max-iterations", type=int, default=3)
    parser.add_argument(
        "--timeout",
        type=float,
        default=None,
        help="total timeout; defaults to 600s for VQE and 120s otherwise",
    )
    parser.add_argument("--poll-interval", type=float, default=1.0)
    parser.add_argument(
        "--minimum-similarity",
        type=float,
        default=None,
        help="defaults to 0.75 for noisy VQE runs and 0.90 otherwise",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    api_url = args.api_url.rstrip("/")
    timeout = args.timeout if args.timeout is not None else (600.0 if args.algorithm == "vqe" else 120.0)
    minimum_similarity = (
        args.minimum_similarity
        if args.minimum_similarity is not None
        else (0.75 if args.algorithm == "vqe" else 0.90)
    )
    deadline = time.monotonic() + timeout

    try:
        print("[1/4] Checking API health")
        status, health = request_json(f"{api_url}/health")
        if status != 200 or health.get("status") != "ok":
            raise ValidationError(f"health check returned HTTP {status}: {health}")

        duplicate_payload, distinct_payload = experiment_payloads(args)

        print(
            "[2/4] Submitting two equivalent and one distinct "
            f"{args.algorithm} experiment"
        )
        source_id = submit(api_url, duplicate_payload)
        duplicate_id = submit(api_url, duplicate_payload)
        distinct_id = submit(api_url, distinct_payload)

        print("[3/4] Waiting for orchestrator completion")
        wait_for_completion(
            api_url,
            [source_id, duplicate_id, distinct_id],
            deadline=deadline,
            poll_interval=args.poll_interval,
        )

        print("[4/4] Waiting for Kafka indexing and pgvector search")
        match = wait_for_duplicate_neighbour(
            api_url,
            source_id=source_id,
            duplicate_id=duplicate_id,
            deadline=deadline,
            poll_interval=args.poll_interval,
            minimum_similarity=minimum_similarity,
        )
        print(
            "PASS: duplicate experiment returned with cosine similarity "
            f"{match['similarity']:.6f}"
        )
        return 0
    except ValidationError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
