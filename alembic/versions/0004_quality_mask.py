"""create quality_mask table

Revision ID: 0004_quality_mask
Revises: 0003_methodology_version
Create Date: 2026-05-24

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004_quality_mask"
down_revision: str | None = "0003_methodology_version"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "quality_mask",
        sa.Column("observation_id", sa.Integer(), nullable=False),
        sa.Column("valid_pixel_fraction", sa.Float(), nullable=False),
        sa.Column("parameters", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["observation_id"],
            ["observation.id"],
            name="fk_quality_mask_observation_id_observation",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("observation_id", name="pk_quality_mask"),
    )


def downgrade() -> None:
    op.drop_table("quality_mask")
