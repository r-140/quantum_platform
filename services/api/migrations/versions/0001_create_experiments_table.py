"""create experiments table

Revision ID: 0001
Revises:
Create Date: 2026-07-18

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "experiments",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("algorithm", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("result", postgresql.JSONB(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
    )
    # Supports GET /experiments (list_all, ordered by submission time) and
    # any future "recent experiments" query without a full table scan.
    op.create_index("ix_experiments_submitted_at", "experiments", ["submitted_at"])


def downgrade() -> None:
    op.drop_index("ix_experiments_submitted_at", table_name="experiments")
    op.drop_table("experiments")
