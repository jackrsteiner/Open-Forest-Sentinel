from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest
import rasterio
from affine import Affine
from rasterio.crs import CRS
from sqlalchemy import select
from sqlalchemy.orm import Session

from forest_sentinel.aoi import load_aoi_config, persist_aoi
from forest_sentinel.change import (
    CHANGE_TYPE_BY_INDEX,
    DEFAULT_BASELINE_WINDOW,
    compute_change_products_for_observation,
)
from forest_sentinel.methodology import get_or_create_methodology_version
from forest_sentinel.models import (
    Aoi,
    ChangeRaster,
    ChangeRasterSource,
    IndexRaster,
    MethodologyVersion,
    Observation,
)
from forest_sentinel.storage import LocalStorage

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"

_TRANSFORM = Affine.translation(500_000.0, 9_300_000.0) * Affine.scale(30.0, -30.0)
_CRS = CRS.from_epsg(32755)
_SHAPE = (4, 4)


def _write_index_cog(path: Path, values: np.ndarray) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    profile = {
        "driver": "GTiff",
        "dtype": "float32",
        "count": 1,
        "height": int(values.shape[0]),
        "width": int(values.shape[1]),
        "transform": _TRANSFORM,
        "crs": _CRS,
        "nodata": float("nan"),
    }
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(values.astype("float32"), 1)
    return path


def _make_aoi(session: Session) -> Aoi:
    aoi = persist_aoi(session, load_aoi_config(EXAMPLES / "aoi-sample.geojson"))
    session.flush()
    return aoi


def _make_observation(session: Session, aoi: Aoi, *, scene: str, days: int) -> Observation:
    observation = Observation(
        aoi_id=aoi.id,
        sensor="HLSL30",
        acquired_at=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=days),
        source_scene_id=scene,
        cloud_cover_percent=5.0,
    )
    session.add(observation)
    session.flush()
    return observation


def _add_index_raster(
    session: Session,
    *,
    observation: Observation,
    methodology: MethodologyVersion,
    index_type: str,
    cog: Path,
) -> IndexRaster:
    row = IndexRaster(
        observation_id=observation.id,
        methodology_version_id=methodology.id,
        index_type=index_type,
        cog_path=str(cog),
    )
    session.add(row)
    session.flush()
    return row


def _seed_observations_with_nbr_values(
    session: Session,
    tmp_path: Path,
    nbr_per_day: dict[int, float],
    methodology: MethodologyVersion,
) -> tuple[Aoi, dict[int, Observation]]:
    aoi = _make_aoi(session)
    observations: dict[int, Observation] = {}
    for day, value in nbr_per_day.items():
        observation = _make_observation(session, aoi, scene=f"scene-{day:02d}", days=day)
        cog = _write_index_cog(
            tmp_path / "indices" / f"day-{day:02d}-nbr.tif",
            np.full(_SHAPE, value, dtype="float32"),
        )
        _add_index_raster(
            session,
            observation=observation,
            methodology=methodology,
            index_type="nbr",
            cog=cog,
        )
        observations[day] = observation
    return aoi, observations


# ---------------------------------------------------------------------------
# Constants


def test_change_type_mapping_is_complete() -> None:
    assert CHANGE_TYPE_BY_INDEX == {"nbr": "delta_nbr", "ndvi": "delta_ndvi"}


# ---------------------------------------------------------------------------
# End-to-end deltas


def test_delta_against_trailing_median_baseline(db_session: Session, tmp_path: Path) -> None:
    methodology = get_or_create_methodology_version(
        db_session,
        name="optical-change",
        version="0.1",
        parameters={"baseline_window": 3},
    )
    _, observations = _seed_observations_with_nbr_values(
        db_session,
        tmp_path,
        nbr_per_day={1: 0.8, 5: 0.7, 9: 0.9, 13: 0.4},  # baseline median for day 13 = 0.8
        methodology=methodology,
    )
    storage = LocalStorage(root=tmp_path / "cogs")

    produced = compute_change_products_for_observation(
        db_session,
        observations[13],
        methodology=methodology,
        storage=storage,
        aoi_name="Example AOI",
        index_types=["nbr"],
    )
    db_session.commit()

    assert len(produced) == 1
    [row] = produced
    assert row.change_type == "delta_nbr"
    assert row.observation_id == observations[13].id

    with rasterio.open(row.cog_path) as src:
        delta = src.read(1)
    np.testing.assert_allclose(delta, np.full(_SHAPE, 0.4 - 0.8), atol=1e-5)


def test_baseline_window_limits_contributing_rasters(db_session: Session, tmp_path: Path) -> None:
    methodology = get_or_create_methodology_version(
        db_session,
        name="optical-change",
        version="0.1",
        parameters={"baseline_window": 2},
    )
    _, observations = _seed_observations_with_nbr_values(
        db_session,
        tmp_path,
        nbr_per_day={1: 0.1, 5: 0.5, 9: 0.9, 13: 0.2},  # window=2 → baseline {0.9, 0.5} → 0.7
        methodology=methodology,
    )
    storage = LocalStorage(root=tmp_path / "cogs")

    produced = compute_change_products_for_observation(
        db_session,
        observations[13],
        methodology=methodology,
        storage=storage,
        aoi_name="Example AOI",
        index_types=["nbr"],
    )
    db_session.commit()
    [row] = produced

    with rasterio.open(row.cog_path) as src:
        delta = src.read(1)
    np.testing.assert_allclose(delta, np.full(_SHAPE, 0.2 - 0.7), atol=1e-5)

    sources = db_session.scalars(
        select(ChangeRasterSource).where(ChangeRasterSource.change_raster_id == row.id)
    ).all()
    # current + 2 baseline observations
    assert len(sources) == 3


def test_no_baseline_skips_silently(db_session: Session, tmp_path: Path) -> None:
    methodology = get_or_create_methodology_version(
        db_session, name="optical-change", version="0.1", parameters={}
    )
    _, observations = _seed_observations_with_nbr_values(
        db_session, tmp_path, nbr_per_day={1: 0.5}, methodology=methodology
    )
    storage = LocalStorage(root=tmp_path / "cogs")

    produced = compute_change_products_for_observation(
        db_session,
        observations[1],
        methodology=methodology,
        storage=storage,
        aoi_name="Example AOI",
        index_types=["nbr"],
    )
    db_session.commit()

    assert produced == []
    assert db_session.scalars(select(ChangeRaster)).all() == []


def test_missing_current_index_skips_silently(db_session: Session, tmp_path: Path) -> None:
    methodology = get_or_create_methodology_version(
        db_session, name="optical-change", version="0.1", parameters={}
    )
    aoi = _make_aoi(db_session)
    observation = _make_observation(db_session, aoi, scene="lonely", days=1)
    storage = LocalStorage(root=tmp_path / "cogs")

    produced = compute_change_products_for_observation(
        db_session,
        observation,
        methodology=methodology,
        storage=storage,
        aoi_name="Example AOI",
        index_types=["nbr"],
    )
    db_session.commit()

    assert produced == []


def test_idempotent_rerun(db_session: Session, tmp_path: Path) -> None:
    methodology = get_or_create_methodology_version(
        db_session,
        name="optical-change",
        version="0.1",
        parameters={"baseline_window": 2},
    )
    _, observations = _seed_observations_with_nbr_values(
        db_session,
        tmp_path,
        nbr_per_day={1: 0.5, 5: 0.6, 9: 0.4},
        methodology=methodology,
    )
    storage = LocalStorage(root=tmp_path / "cogs")

    first = compute_change_products_for_observation(
        db_session,
        observations[9],
        methodology=methodology,
        storage=storage,
        aoi_name="Example AOI",
        index_types=["nbr"],
    )
    db_session.commit()
    second = compute_change_products_for_observation(
        db_session,
        observations[9],
        methodology=methodology,
        storage=storage,
        aoi_name="Example AOI",
        index_types=["nbr"],
    )
    db_session.commit()

    assert {r.id for r in first} == {r.id for r in second}
    assert len(db_session.scalars(select(ChangeRaster)).all()) == 1


def test_default_window_size_used_when_not_in_parameters(
    db_session: Session, tmp_path: Path
) -> None:
    """A methodology without a `baseline_window` parameter falls back to the default."""
    methodology = get_or_create_methodology_version(
        db_session, name="optical-change", version="0.1", parameters={}
    )
    assert DEFAULT_BASELINE_WINDOW >= 1
    # one baseline + one current is enough; the default is generous
    _, observations = _seed_observations_with_nbr_values(
        db_session, tmp_path, nbr_per_day={1: 0.5, 5: 0.4}, methodology=methodology
    )
    storage = LocalStorage(root=tmp_path / "cogs")

    produced = compute_change_products_for_observation(
        db_session,
        observations[5],
        methodology=methodology,
        storage=storage,
        aoi_name="Example AOI",
        index_types=["nbr"],
    )
    db_session.commit()

    assert len(produced) == 1


def test_shape_mismatch_raises(db_session: Session, tmp_path: Path) -> None:
    methodology = get_or_create_methodology_version(
        db_session,
        name="optical-change",
        version="0.1",
        parameters={"baseline_window": 1},
    )
    aoi = _make_aoi(db_session)

    baseline_obs = _make_observation(db_session, aoi, scene="base", days=1)
    baseline_cog = _write_index_cog(
        tmp_path / "indices" / "base.tif", np.full(_SHAPE, 0.5, dtype="float32")
    )
    _add_index_raster(
        db_session,
        observation=baseline_obs,
        methodology=methodology,
        index_type="nbr",
        cog=baseline_cog,
    )

    current_obs = _make_observation(db_session, aoi, scene="curr", days=5)
    bigger_cog = _write_index_cog(
        tmp_path / "indices" / "curr.tif", np.full((8, 8), 0.3, dtype="float32")
    )
    _add_index_raster(
        db_session,
        observation=current_obs,
        methodology=methodology,
        index_type="nbr",
        cog=bigger_cog,
    )

    storage = LocalStorage(root=tmp_path / "cogs")

    with pytest.raises(ValueError, match="shape"):
        compute_change_products_for_observation(
            db_session,
            current_obs,
            methodology=methodology,
            storage=storage,
            aoi_name="Example AOI",
            index_types=["nbr"],
        )


def test_unsupported_index_type_raises(db_session: Session, tmp_path: Path) -> None:
    methodology = get_or_create_methodology_version(
        db_session, name="optical-change", version="0.1", parameters={}
    )
    aoi = _make_aoi(db_session)
    observation = _make_observation(db_session, aoi, scene="x", days=1)
    storage = LocalStorage(root=tmp_path / "cogs")

    with pytest.raises(ValueError, match="Unsupported index type"):
        compute_change_products_for_observation(
            db_session,
            observation,
            methodology=methodology,
            storage=storage,
            aoi_name="Example AOI",
            index_types=["mystery"],
        )
