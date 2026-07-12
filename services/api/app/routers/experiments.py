"""
POST /experiments and friends.

Dispatch note: grover/sat_grover/qpe are executed by directly `await`-ing
async helpers in app/execution.py -- they only ever do async I/O
(QuantumBackend.submit/wait_for_result), so awaiting them directly on
FastAPI's event loop is correct and doesn't block other requests.

VQE is different: `run_vqe_sync` is a *synchronous* function (it bridges
to asyncio internally via `asyncio.run()` per optimizer iteration -- see
quantum_core/loops/vqe_loop.py for why). Calling a blocking sync function
directly from an `async def` route handler would block FastAPI's entire
event loop for the whole VQE run (which can take a while -- dozens of
iterations, several circuits each), stalling every other request the
server is handling. Starlette's `run_in_threadpool` offloads it to a
worker thread instead, keeping the event loop free. This is a real
correctness issue, not a style preference -- it's exactly the kind of bug
that looks fine in a single-request demo and falls over under concurrent
load.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from starlette.concurrency import run_in_threadpool

from app import execution
from app.deps import ExperimentStore, get_backend, get_store, utcnow
from app.schemas.experiments import (
    ExperimentRequest,
    ExperimentResponse,
    ExperimentStatus,
    GroverRequest,
    QPERequest,
    SatGroverRequest,
    VQERequest,
)

router = APIRouter(prefix="/experiments", tags=["experiments"])


@router.post("", response_model=ExperimentResponse)
async def submit_experiment(
    request: ExperimentRequest,
    backend=Depends(get_backend),
    store: ExperimentStore = Depends(get_store),
) -> ExperimentResponse:
    experiment_id = str(uuid.uuid4())
    submitted_at = utcnow()

    try:
        if isinstance(request, GroverRequest):
            result = await execution.run_grover(backend, request)
        elif isinstance(request, SatGroverRequest):
            result = await execution.run_sat_grover(backend, request)
        elif isinstance(request, QPERequest):
            result = await execution.run_qpe(backend, request)
        elif isinstance(request, VQERequest):
            result = await run_in_threadpool(execution.run_vqe_sync, backend, request)
        else:  # pragma: no cover -- exhaustive per ExperimentRequest's discriminated union
            raise HTTPException(status_code=400, detail=f"unhandled algorithm {request.algorithm!r}")

        response = ExperimentResponse(
            id=experiment_id,
            algorithm=request.algorithm,
            status=ExperimentStatus.COMPLETED,
            submitted_at=submitted_at,
            completed_at=utcnow(),
            result=result,
        )
    except Exception as exc:  # noqa: BLE001 -- deliberately broad: any backend/circuit
        # error becomes a FAILED experiment record, not a raw 500. Once the
        # orchestrator/queue exists, this is also where retryable vs. fatal
        # errors would be distinguished (see quantum_core.sync.polling for
        # that distinction elsewhere in the project).
        response = ExperimentResponse(
            id=experiment_id,
            algorithm=request.algorithm,
            status=ExperimentStatus.FAILED,
            submitted_at=submitted_at,
            completed_at=utcnow(),
            error=str(exc),
        )

    store.save(response)
    return response


@router.get("/{experiment_id}", response_model=ExperimentResponse)
async def get_experiment(
    experiment_id: str, store: ExperimentStore = Depends(get_store)
) -> ExperimentResponse:
    experiment = store.get(experiment_id)
    if experiment is None:
        raise HTTPException(status_code=404, detail="experiment not found")
    return experiment


@router.get("", response_model=list[ExperimentResponse])
async def list_experiments(store: ExperimentStore = Depends(get_store)) -> list[ExperimentResponse]:
    return store.list_all()