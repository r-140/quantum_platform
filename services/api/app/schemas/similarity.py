from __future__ import annotations

from pydantic import BaseModel, Field


class SimilarExperiment(BaseModel):
    experiment_id: str
    algorithm: str
    molecule: str | None = None
    similarity: float = Field(ge=-1.0, le=1.0)
    content: str
