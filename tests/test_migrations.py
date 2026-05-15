from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, inspect, text


def test_migrations_create_aoi_table(alembic_config: Config, clean_database: Engine) -> None:
    command.upgrade(alembic_config, "head")

    inspector = inspect(clean_database)
    assert "aoi" in inspector.get_table_names()

    columns = {column["name"] for column in inspector.get_columns("aoi")}
    assert {"id", "name", "geometry", "created_at"} <= columns

    # The geometry column is registered with PostGIS as a MULTIPOLYGON in EPSG:4326.
    with clean_database.connect() as connection:
        row = connection.execute(
            text(
                "SELECT type, srid FROM geometry_columns "
                "WHERE f_table_name = 'aoi' AND f_geometry_column = 'geometry'"
            )
        ).one()
    assert row[0] == "MULTIPOLYGON"
    assert row[1] == 4326


def test_downgrade_removes_aoi_table(alembic_config: Config, clean_database: Engine) -> None:
    command.upgrade(alembic_config, "head")
    command.downgrade(alembic_config, "base")

    assert "aoi" not in inspect(clean_database).get_table_names()


def test_migrations_create_observation_table(
    alembic_config: Config, clean_database: Engine
) -> None:
    command.upgrade(alembic_config, "head")

    inspector = inspect(clean_database)
    assert "observation" in inspector.get_table_names()

    columns = {column["name"] for column in inspector.get_columns("observation")}
    assert {
        "id",
        "aoi_id",
        "sensor",
        "acquired_at",
        "source_scene_id",
        "cloud_cover_percent",
        "created_at",
    } <= columns

    foreign_keys = inspector.get_foreign_keys("observation")
    assert any(
        fk["referred_table"] == "aoi" and fk["constrained_columns"] == ["aoi_id"]
        for fk in foreign_keys
    )

    unique_constraints = inspector.get_unique_constraints("observation")
    assert any(
        sorted(uc["column_names"]) == ["aoi_id", "source_scene_id"] for uc in unique_constraints
    )


def test_downgrade_removes_observation_table(
    alembic_config: Config, clean_database: Engine
) -> None:
    command.upgrade(alembic_config, "head")
    command.downgrade(alembic_config, "base")

    assert "observation" not in inspect(clean_database).get_table_names()
