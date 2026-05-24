"""create observation table

Revision ID: 0002_observation
Revises: 0001_create_aoi_table
Create Date: 2026-05-24

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_observation"
down_revision: str | None = "0001_create_aoi_table"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "observation",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("aoi_id", sa.Integer(), nullable=False),
        sa.Column("sensor", sa.String(), nullable=False),
        sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_scene_id", sa.String(), nullable=False),
        sa.Column("cloud_cover_percent", sa.Float(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["aoi_id"], ["aoi.id"], name="fk_observation_aoi_id_aoi"),
        sa.PrimaryKeyConstraint("id", name="pk_observation"),
        sa.UniqueConstraint(
            "aoi_id", "source_scene_id", name="uq_observation_aoi_id_source_scene_id"
        ),
    )
    op.create_index("ix_observation_aoi_id_acquired_at", "observation", ["aoi_id", "acquired_at"])


def downgrade() -> None:
    op.drop_index("ix_observation_aoi_id_acquired_at", table_name="observation")
    op.drop_table("observation")
