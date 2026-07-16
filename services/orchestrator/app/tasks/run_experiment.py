"""
Dispatches an ExperimentTask to the right quantum_core.execution function
based on `task.algorithm`. Pulled out of worker.py into its own module so
`worker.py` stays a thin RabbitMQ connection/consume-loop shell, and so
this dispatch logic is testable without any aio-pika involvement (it
doesn't touch messages at all -- only ExperimentTask, a plain dataclass).

Mirrors services/api/app/execution.py's old dispatch (before that file was
deleted and the API stopped executing anything itself) -- both were thin
adapters unpacking a different request/message format into calls to the
same quantum_core.execution functions. This one unpacks `task.params`, a
plain dict, instead of a Pydantic request object.
"""

from __future__ import annotations

import asyncio

from quantum_core.backends.base import QuantumBackend
from quantum_core.execution import run_grover, run_qpe, run_sat_grover, run_vqe_sync
from quantum_core.tasks import ExperimentTask


async def execute_task(backend: QuantumBackend, task: ExperimentTask) -> dict:
    params = task.params

    if task.algorithm == "grover":
        return await run_grover(backend, params["marked_states"], shots=params.get("shots", 1024))

    if task.algorithm == "sat_grover":
        return await run_sat_grover(
            backend, params["variables"], params["expression"], shots=params.get("shots", 1024)
        )

    if task.algorithm == "qpe":
        return await run_qpe(
            backend,
            params["phi"],
            num_counting_qubits=params.get("num_counting_qubits", 3),
            shots=params.get("shots", 1024),
        )

    if task.algorithm == "vqe":
        # run_vqe_sync is synchronous by design (see quantum_core.execution
        # for why) -- offload to a thread via run_in_executor, the plain
        # asyncio equivalent of Starlette's run_in_threadpool used on the
        # API side for the same reason. Without this, a VQE task would
        # block this worker's event loop for its entire ~1 minute runtime,
        # stalling every other queued task behind it.
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None,
            run_vqe_sync,
            backend,
            params.get("shots", 8192),
            params.get("max_iterations", 80),
        )

    raise ValueError(f"unknown algorithm {task.algorithm!r}")