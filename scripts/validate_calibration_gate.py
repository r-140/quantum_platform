#!/usr/bin/env python3
"""End-to-end validation of stale-calibration VQE waiting and resumption."""

from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request

API = "http://localhost:8000"
BACKEND = "aer-simulator"


def request(path: str, *, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        API + path,
        data=data,
        method="POST" if data is not None else "GET",
        headers={"Content-Type": "application/json"} if data else {},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:
        return exc.code, json.load(exc)


def main() -> int:
    try:
        print("[1/5] Waiting for an initial calibration observation")
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            status, observation = request(f"/backends/{BACKEND}/calibration")
            if status == 200:
                break
            time.sleep(1)
        else:
            raise RuntimeError("no calibration observation after 60s")
        if observation["error_rate"] >= 0.10:
            raise RuntimeError(
                f"backend is already unhealthy: error_rate={observation['error_rate']}"
            )

        print("[2/5] Making the persisted observation deliberately stale")
        subprocess.run(
            [
                "docker", "compose", "exec", "-T", "postgres",
                "psql", "-U", "quantum", "-d", "quantum_platform",
                "-v", "ON_ERROR_STOP=1", "-c",
                "UPDATE backend_calibration_state "
                "SET observed_at=now()-interval '1 day' "
                "WHERE backend_name='aer-simulator'",
            ],
            check=True,
        )

        print("[3/5] Submitting a minimal H2 VQE experiment")
        status, experiment = request(
            "/experiments",
            payload={
                "algorithm": "vqe",
                "molecule": "h2",
                "shots": 128,
                "max_iterations": 1,
            },
        )
        if status != 202:
            raise RuntimeError(f"submission returned HTTP {status}: {experiment}")
        experiment_id = experiment["id"]
        print(f"  submitted {experiment_id}")

        print("[4/5] Observing waiting_for_calibration and automatic resumption")
        saw_waiting = False
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            status, experiment = request(f"/experiments/{experiment_id}")
            if status != 200:
                raise RuntimeError(f"experiment lookup returned HTTP {status}")
            state = experiment["status"]
            if state == "waiting_for_calibration" and not saw_waiting:
                saw_waiting = True
                print("  observed waiting_for_calibration")
            if state == "failed":
                raise RuntimeError(f"experiment failed: {experiment.get('error')}")
            if state == "completed":
                break
            time.sleep(0.5)
        else:
            raise RuntimeError("experiment did not complete within 300s")
        if not saw_waiting:
            raise RuntimeError("experiment completed without exposing the waiting state")

        print("[5/5] Verifying that the triggered probe refreshed the snapshot")
        status, refreshed = request(f"/backends/{BACKEND}/calibration")
        if status != 200 or refreshed["observed_at"] == observation["observed_at"]:
            raise RuntimeError(f"calibration snapshot was not refreshed: {refreshed}")
        print("PASS: stale VQE waited, triggered calibration, resumed, and completed")
        return 0
    except (RuntimeError, OSError, subprocess.CalledProcessError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
