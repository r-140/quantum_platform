"""Add pgvector-backed semantic index for experiment results.

Revision ID: 0003
Revises: 0002
"""

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute(
        """
        CREATE TABLE experiment_embeddings (
            experiment_id TEXT PRIMARY KEY
                REFERENCES experiments(id) ON DELETE CASCADE,
            algorithm TEXT NOT NULL,
            molecule TEXT,
            content TEXT NOT NULL,
            embedding vector(384) NOT NULL,
            embedding_model TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.create_index(
        "ix_experiment_embeddings_algorithm",
        "experiment_embeddings",
        ["algorithm"],
    )
    op.execute(
        """
        CREATE INDEX ix_experiment_embeddings_cosine
        ON experiment_embeddings USING hnsw (embedding vector_cosine_ops)
        """
    )


def downgrade() -> None:
    op.drop_table("experiment_embeddings")
    # Keep the extension: another schema may also depend on it.
