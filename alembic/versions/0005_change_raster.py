"""create change_raster and change_raster_source tables

Revision ID: 0005_change_raster
Revises: 0004_index_raster
Create Date: 2026-05-15

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_change_raster"
down_revision: str | None = "0004_index_raster"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "change_raster",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("observation_id", sa.Integer(), nullable=False),
        sa.Column("methodology_version_id", sa.Integer(), nullable=False),
        sa.Column("change_type", sa.String(), nullable=False),
        sa.Column("cog_path", sa.String(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_change_raster"),
        sa.ForeignKeyConstraint(
            ["observation_id"],
            ["observation.id"],
            name="fk_change_raster_observation_id_observation",
        ),
        sa.ForeignKeyConstraint(
            ["methodology_version_id"],
            ["methodology_version.id"],
            name="fk_change_raster_methodology_version_id",
        ),
        sa.UniqueConstraint(
            "observation_id",
            "change_type",
            "methodology_version_id",
            name="uq_change_raster_observation_change_methodology",
        ),
    )
    op.create_table(
        "change_raster_source",
        sa.Column("change_raster_id", sa.Integer(), nullable=False),
        sa.Column("index_raster_id", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint(
            "change_raster_id",
            "index_raster_id",
            name="pk_change_raster_source",
        ),
        sa.ForeignKeyConstraint(
            ["change_raster_id"],
            ["change_raster.id"],
            name="fk_change_raster_source_cr_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["index_raster_id"],
            ["index_raster.id"],
            name="fk_change_raster_source_ir_id",
        ),
    )


def downgrade() -> None:
    op.drop_table("change_raster_source")
    op.drop_table("change_raster")
