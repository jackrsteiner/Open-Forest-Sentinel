"""create index_raster table

Revision ID: 0004_index_raster
Revises: 0003_methodology_version
Create Date: 2026-05-15

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_index_raster"
down_revision: str | None = "0003_methodology_version"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "index_raster",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("observation_id", sa.Integer(), nullable=False),
        sa.Column("methodology_version_id", sa.Integer(), nullable=False),
        sa.Column("index_type", sa.String(), nullable=False),
        sa.Column("cog_path", sa.String(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_index_raster"),
        sa.ForeignKeyConstraint(
            ["observation_id"],
            ["observation.id"],
            name="fk_index_raster_observation_id_observation",
        ),
        sa.ForeignKeyConstraint(
            ["methodology_version_id"],
            ["methodology_version.id"],
            name="fk_index_raster_methodology_version_id",
        ),
        sa.UniqueConstraint(
            "observation_id",
            "index_type",
            "methodology_version_id",
            name="uq_index_raster_observation_index_methodology",
        ),
    )


def downgrade() -> None:
    op.drop_table("index_raster")
