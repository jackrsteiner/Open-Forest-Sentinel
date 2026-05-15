from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest
import rasterio
from affine import Affine
from geoalchemy2.shape import to_shape
from rasterio.crs import CRS
from sqlalchemy import select
from sqlalchemy.orm import Session

from forest_sentinel.aoi import load_aoi_config, persist_aoi
from forest_sentinel.candidates import (
    DEFAULT_DELTA_NBR_THRESHOLD,
    DEFAULT_MIN_AREA_M2,
    extract_candidates_for_change_raster,
)
from forest_sentinel.methodology import get_or_create_methodology_version
from forest_sentinel.models import (
    Aoi,
    ChangeRaster,
    DisturbanceCandidate,
    MethodologyVersion,
    Observation,
)

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"

# UTM zone 55S; 30 m pixels — matches the indices/change synthetic fixtures.
_TRANSFORM = Affine.translation(500_000.0, 9_300_000.0) * Affine.scale(30.0, -30.0)
_CRS = CRS.from_epsg(32755)
_SHAPE = (10, 10)
_PIXEL_AREA_M2 = 30.0 * 30.0  # 900


def _write_change_cog(path: Path, data: np.ndarray) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    profile = {
        "driver": "GTiff",
        "dtype": "float32",
        "count": 1,
        "height": int(data.shape[0]),
        "width": int(data.shape[1]),
        "transform": _TRANSFORM,
        "crs": _CRS,
        "nodata": float("nan"),
    }
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(data.astype("float32"), 1)
    return path


def _setup_world(
    db_session: Session, tmp_path: Path, *, change_data: np.ndarray
) -> tuple[Aoi, Observation, ChangeRaster, MethodologyVersion]:
    aoi = persist_aoi(db_session, load_aoi_config(EXAMPLES / "aoi-sample.geojson"))
    db_session.flush()
    observation = Observation(
        aoi_id=aoi.id,
        sensor="HLSL30",
        acquired_at=datetime(2026, 3, 14, 12, 0, tzinfo=UTC),
        source_scene_id="scene-1",
        cloud_cover_percent=5.0,
    )
    db_session.add(observation)
    db_session.flush()
    methodology = get_or_create_methodology_version(
        db_session,
        name="optical-change",
        version="0.1",
        parameters={"delta_nbr_threshold": -0.25, "min_area_m2": 900},
    )
    cog = _write_change_cog(tmp_path / "change" / "delta_nbr.tif", change_data)
    change_raster = ChangeRaster(
        observation_id=observation.id,
        methodology_version_id=methodology.id,
        change_type="delta_nbr",
        cog_path=str(cog),
    )
    db_session.add(change_raster)
    db_session.flush()
    return aoi, observation, change_raster, methodology


def test_defaults_are_sensible() -> None:
    assert DEFAULT_DELTA_NBR_THRESHOLD < 0
    assert DEFAULT_MIN_AREA_M2 > 0


def test_no_disturbance_yields_no_candidates(db_session: Session, tmp_path: Path) -> None:
    _, _, change_raster, methodology = _setup_world(
        db_session, tmp_path, change_data=np.zeros(_SHAPE, dtype="float32")
    )

    rows = extract_candidates_for_change_raster(db_session, change_raster, methodology=methodology)
    db_session.commit()

    assert rows == []
    assert db_session.scalars(select(DisturbanceCandidate)).all() == []


def test_single_patch_produces_one_candidate(db_session: Session, tmp_path: Path) -> None:
    data = np.zeros(_SHAPE, dtype="float32")
    data[2:5, 3:7] = -0.5  # 3 × 4 = 12 pixels = 10_800 m²
    _, observation, change_raster, methodology = _setup_world(
        db_session, tmp_path, change_data=data
    )

    rows = extract_candidates_for_change_raster(db_session, change_raster, methodology=methodology)
    db_session.commit()

    assert len(rows) == 1
    [candidate] = rows
    assert candidate.change_raster_id == change_raster.id
    assert candidate.methodology_version_id == methodology.id
    assert candidate.detected_at == observation.acquired_at.date()
    assert candidate.area_m2 == pytest.approx(12 * _PIXEL_AREA_M2)

    # Geometry round-trips out of PostGIS as a WGS 84 polygon.
    polygon = to_shape(candidate.geometry)
    assert polygon.is_valid
    # Plausibility: the polygon's centroid is in the patch's bounds
    # (somewhere near 500_000 + ~135m east, 9_300_000 - ~105m south),
    # which lies in UTM zone 55S — verify it's at least in the Pacific
    # hemisphere after projection back to WGS 84.
    assert 130.0 < polygon.centroid.x < 180.0


def test_disconnected_patches_produce_separate_candidates(
    db_session: Session, tmp_path: Path
) -> None:
    data = np.zeros(_SHAPE, dtype="float32")
    data[1:3, 1:3] = -0.5
    data[6:9, 5:8] = -0.5
    _, _, change_raster, methodology = _setup_world(db_session, tmp_path, change_data=data)

    rows = extract_candidates_for_change_raster(db_session, change_raster, methodology=methodology)
    db_session.commit()

    assert len(rows) == 2


def test_minimum_area_filters_small_patches(db_session: Session, tmp_path: Path) -> None:
    data = np.zeros(_SHAPE, dtype="float32")
    data[0, 0] = -0.5  # 1 pixel = 900 m² — below 5_000 m² threshold
    data[5:9, 5:9] = -0.5  # 16 pixels = 14_400 m² — passes
    _, _, change_raster, methodology = _setup_world(db_session, tmp_path, change_data=data)

    rows = extract_candidates_for_change_raster(
        db_session,
        change_raster,
        methodology=methodology,
        min_area_m2=5_000,
    )
    db_session.commit()

    assert len(rows) == 1
    assert rows[0].area_m2 == pytest.approx(16 * _PIXEL_AREA_M2)


def test_threshold_respects_sign(db_session: Session, tmp_path: Path) -> None:
    """Positive ΔNBR (NBR rose; regrowth) must not produce candidates."""
    data = np.full(_SHAPE, 0.5, dtype="float32")  # all pixels above threshold
    _, _, change_raster, methodology = _setup_world(db_session, tmp_path, change_data=data)

    rows = extract_candidates_for_change_raster(db_session, change_raster, methodology=methodology)
    db_session.commit()

    assert rows == []


def test_nan_pixels_are_ignored(db_session: Session, tmp_path: Path) -> None:
    data = np.full(_SHAPE, np.nan, dtype="float32")
    _, _, change_raster, methodology = _setup_world(db_session, tmp_path, change_data=data)

    rows = extract_candidates_for_change_raster(db_session, change_raster, methodology=methodology)
    db_session.commit()

    assert rows == []


def test_methodology_parameters_drive_defaults(db_session: Session, tmp_path: Path) -> None:
    data = np.zeros(_SHAPE, dtype="float32")
    data[2:5, 3:7] = -0.5  # ΔNBR = -0.5

    # Methodology has a high threshold; nothing should pass.
    aoi = persist_aoi(db_session, load_aoi_config(EXAMPLES / "aoi-sample.geojson"))
    db_session.flush()
    observation = Observation(
        aoi_id=aoi.id,
        sensor="HLSL30",
        acquired_at=datetime(2026, 3, 14, 12, 0, tzinfo=UTC),
        source_scene_id="scene-x",
    )
    db_session.add(observation)
    db_session.flush()
    strict_methodology = get_or_create_methodology_version(
        db_session,
        name="optical-change",
        version="0.2",
        parameters={"delta_nbr_threshold": -0.9, "min_area_m2": 900},
    )
    cog = _write_change_cog(tmp_path / "change" / "delta_nbr.tif", data)
    change_raster = ChangeRaster(
        observation_id=observation.id,
        methodology_version_id=strict_methodology.id,
        change_type="delta_nbr",
        cog_path=str(cog),
    )
    db_session.add(change_raster)
    db_session.flush()

    rows = extract_candidates_for_change_raster(
        db_session, change_raster, methodology=strict_methodology
    )
    db_session.commit()
    assert rows == []


def test_rerun_replaces_existing_candidates(db_session: Session, tmp_path: Path) -> None:
    data = np.zeros(_SHAPE, dtype="float32")
    data[2:5, 3:7] = -0.5
    _, _, change_raster, methodology = _setup_world(db_session, tmp_path, change_data=data)

    first = extract_candidates_for_change_raster(db_session, change_raster, methodology=methodology)
    db_session.commit()
    first_ids = {row.id for row in first}

    second = extract_candidates_for_change_raster(
        db_session, change_raster, methodology=methodology
    )
    db_session.commit()
    second_ids = {row.id for row in second}

    # Rows are replaced, not appended.
    assert len(db_session.scalars(select(DisturbanceCandidate)).all()) == len(second)
    assert first_ids.isdisjoint(second_ids)
