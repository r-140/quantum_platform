from __future__ import annotations

from typing import Sequence


def vector_literal(values: Sequence[float]) -> str:
    return "[" + ",".join(format(float(value), ".9g") for value in values) + "]"


async def upsert_embedding(
    pool,
    *,
    experiment: dict,
    content: str,
    embedding: Sequence[float],
    model_name: str,
) -> None:
    parameters = experiment.get("parameters") or {}
    result = experiment.get("result") or {}
    molecule = parameters.get("molecule") or result.get("molecule")
    await pool.execute(
        """
        INSERT INTO experiment_embeddings
            (experiment_id, algorithm, molecule, content, embedding, embedding_model)
        VALUES ($1, $2, $3, $4, $5::vector, $6)
        ON CONFLICT (experiment_id) DO UPDATE SET
            algorithm = EXCLUDED.algorithm,
            molecule = EXCLUDED.molecule,
            content = EXCLUDED.content,
            embedding = EXCLUDED.embedding,
            embedding_model = EXCLUDED.embedding_model,
            updated_at = now()
        """,
        experiment["id"],
        experiment["algorithm"],
        molecule,
        content,
        vector_literal(embedding),
        model_name,
    )
