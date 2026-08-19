"""persist validated experiment request parameters

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-19
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Nullable for experiments created before this migration. New API writes
    # always include the validated request parameters.
    op.add_column(
        "experiments",
        sa.Column("parameters", postgresql.JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("experiments", "parameters")
