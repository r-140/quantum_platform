"""
POST /experiments and friends.

POST enqueues an ExperimentTask to RabbitMQ and returns immediately with
status=queued -- it no longer executes anything in-process. The
orchestrator service consumes the queue, runs the experiment against a
QuantumBackend, and publishes a result back to the results queue; a
background consumer in app/main.py picks that up and updates the store, so
GET /experiments/{id} eventually reports completed/failed.

This is a deliberate behavior change from the first version of this
endpoint (which executed synchronously and always returned status=completed
immediately) -- see docs/architecture/orchestration.md for the reasoning.
One side effect worth calling out: the API no longer needs
`run_in_threadpool` for VQE at all. That workaround existed because VQE ran
synchronously *inside this process*; now VQE (like everything else) is just
a message on a queue, and the orchestrator -- which already offloads VQE
via `run_in_executor` -- is the only place that concern still applies.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException

from app import deps
from app.deps import get_store, utcnow
from app.schemas.experiments import (
    ExperimentRequest,
    ExperimentResponse,
    ExperimentStatus,
)
from app.store.base import ExperimentStore
from quantum_core.tasks import ExperimentTask

router = APIRouter(prefix="/experiments", tags=["experiments"])


@router.post("", response_model=ExperimentResponse, status_code=202)
async def submit_experiment(
    request: ExperimentRequest,
    store: ExperimentStore = Depends(get_store),
) -> ExperimentResponse:
    experiment_id = str(uuid.uuid4())
    submitted_at = utcnow()

    # Save the QUEUED record *before* publishing -- so that even in the
    # unlikely case where the orchestrator processes the task and the
    # result arrives back before this function returns, the results
    # consumer finds an existing record to update rather than dropping the
    # result as "unknown experiment_id" (see app/main.py's consume_results).
    response = ExperimentResponse(
        id=experiment_id,
        algorithm=request.algorithm,
        status=ExperimentStatus.QUEUED,
        submitted_at=submitted_at,
    )
    await store.save(response)

    task = ExperimentTask(
        experiment_id=experiment_id,
        algorithm=request.algorithm,
        params=request.model_dump(exclude={"algorithm"}),
    )

    try:
        await deps.publish_task(task)
    except Exception as exc:  # noqa: BLE001 -- couldn't enqueue at all
        # Not the same failure mode as an experiment that ran and failed --
        # this one never even reached the orchestrator. Mark FAILED
        # immediately rather than leaving a QUEUED record nothing will ever
        # pick up.
        response = ExperimentResponse(
            id=experiment_id,
            algorithm=request.algorithm,
            status=ExperimentStatus.FAILED,
            submitted_at=submitted_at,
            completed_at=utcnow(),
            error=f"failed to enqueue: {exc}",
        )
        await store.save(response)

    return response


@router.get("/stats")
async def get_experiment_stats(store: ExperimentStore = Depends(get_store)) -> list[dict]:
    """Per (algorithm, status) counts -- powers the dashboard's summary
    header. Registered *before* GET /{experiment_id} below: FastAPI/
    Starlette match routes in registration order, and a path parameter
    route matches any string -- if this came after {experiment_id}, a
    request to /experiments/stats would be captured as
    experiment_id="stats" and never reach this handler.
    """
    return await store.stats()


@router.get("/{experiment_id}", response_model=ExperimentResponse)
async def get_experiment(
    experiment_id: str, store: ExperimentStore = Depends(get_store)
) -> ExperimentResponse:
    experiment = await store.get(experiment_id)
    if experiment is None:
        raise HTTPException(status_code=404, detail="experiment not found")
    return experiment


@router.get("", response_model=list[ExperimentResponse])
async def list_experiments(
    algorithm: str | None = None,
    status: str | None = None,
    sort: str = "desc",
    store: ExperimentStore = Depends(get_store),
) -> list[ExperimentResponse]:
    if sort not in ("asc", "desc"):
        raise HTTPException(status_code=422, detail="sort must be 'asc' or 'desc'")
    return await store.list_all(algorithm=algorithm, status=status, sort_desc=(sort == "desc"))