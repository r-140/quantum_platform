"""Semantic nearest-neighbour lookup over indexed experiment results."""

from __future__ import annotations

import os

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import text

from app.db import get_sessionmaker
from app.schemas.similarity import SimilarExperiment

router = APIRouter(prefix="/experiments", tags=["experiments"])


@router.get("/{experiment_id}/similar", response_model=list[SimilarExperiment])
async def similar_experiments(
    experiment_id: str,
    limit: int = Query(default=10, ge=1, le=50),
    same_algorithm: bool = True,
) -> list[SimilarExperiment]:
    if not os.environ.get("DATABASE_URL"):
        raise HTTPException(503, "semantic search requires PostgreSQL/pgvector")

    algorithm_filter = (
        "AND candidate.algorithm = source.algorithm" if same_algorithm else ""
    )
    stmt = text(
        f"""
        SELECT candidate.experiment_id, candidate.algorithm, candidate.molecule,
               1 - (candidate.embedding <=> source.embedding) AS similarity,
               candidate.content
        FROM experiment_embeddings AS source
        JOIN experiment_embeddings AS candidate
          ON candidate.experiment_id <> source.experiment_id
         {algorithm_filter}
        WHERE source.experiment_id = :experiment_id
        ORDER BY candidate.embedding <=> source.embedding
        LIMIT :limit
        """
    )
    async with get_sessionmaker()() as session:
        result = await session.execute(
            stmt, {"experiment_id": experiment_id, "limit": limit}
        )
        rows = result.mappings().all()
        if not rows:
            exists = await session.execute(
                text("SELECT 1 FROM experiment_embeddings WHERE experiment_id=:id"),
                {"id": experiment_id},
            )
            if exists.scalar_one_or_none() is None:
                raise HTTPException(404, "experiment has not been indexed")
        return [SimilarExperiment(**row) for row in rows]
