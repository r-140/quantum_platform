"""
Runs several fake experiments against MockHardwareBackend to demonstrate the
full hardware/software interaction loop: submit -> adaptive poll -> retry on
transient failure -> fetch result (or hit the circuit breaker / timeout).

Run with:  python demo_polling.py
"""

from __future__ import annotations

import asyncio
import logging

from quantum_core.backends.base import Circuit, JobStatus
from quantum_core.backends.mock_hw_backend import MockHardwareBackend
from quantum_core.sync.polling import (
    CircuitBreaker,
    PollingConfig,
    PollingTimeoutError,
    wait_for_result,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


async def run_one_experiment(backend: MockHardwareBackend, breaker: CircuitBreaker, i: int) -> None:
    circuit = Circuit(name=f"demo-circuit-{i}", num_qubits=2, payload=None, shots=1024)
    handle = await backend.submit(circuit)
    print(f"[{i}] submitted job_id={handle.job_id}")

    config = PollingConfig(initial_interval_s=0.1, max_interval_s=1.0, timeout_s=10.0)
    try:
        result = await wait_for_result(backend, handle, config=config, breaker=breaker)
        if result.status == JobStatus.COMPLETED:
            print(f"[{i}] COMPLETED counts={result.counts}")
        else:
            print(f"[{i}] FAILED (non-retryable) error={result.error}")
    except PollingTimeoutError:
        print(f"[{i}] TIMED OUT waiting for backend")
    except Exception as exc:  # noqa: BLE001 - demo script, want to see everything
        print(f"[{i}] GAVE UP after retries: {exc!r}")


async def main() -> None:
    backend = MockHardwareBackend(
        seed=42,
        transient_failure_rate=0.3,
        hard_failure_rate=0.05,
    )
    breaker = CircuitBreaker(failure_threshold=5, reset_after_s=5.0)

    # Run several experiments concurrently -- this is representative of an
    # orchestrator dispatching many jobs against the same backend at once.
    await asyncio.gather(*(run_one_experiment(backend, breaker, i) for i in range(8)))


if __name__ == "__main__":
    asyncio.run(main())
