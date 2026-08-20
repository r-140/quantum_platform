"""Persist the latest verification observation per backend.

Revision ID: 0004
Revises: 0003
"""

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE backend_calibration_state (
            backend_name TEXT PRIMARY KEY,
            observed_at TIMESTAMPTZ NOT NULL,
            probe_type TEXT NOT NULL,
            shots INTEGER NOT NULL CHECK (shots > 0),
            error_rate DOUBLE PRECISION NOT NULL
                CHECK (error_rate >= 0 AND error_rate <= 1),
            counts JSONB NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )


def downgrade() -> None:
    op.drop_table("backend_calibration_state")
