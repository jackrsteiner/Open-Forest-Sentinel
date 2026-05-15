from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest
import rasterio
from affine import Affine
from rasterio.crs import CRS
from sqlalchemy import select
from sqlalchemy.orm import Session

from forest_sentinel.aoi import load_aoi_config, persist_aoi
from forest_sentinel.indices import (
    HLS_FILL_VALUE,
    HLS_SCALE,
    BandResolver,
    HlsBand,
    LocalBandResolver,
    asset_for,
    compute_indices_for_observation,
    compute_nbr,
    compute_ndvi,
    read_band_window,
)
from forest_sentinel.methodology import get_or_create_methodology_version
from forest_sentinel.models import IndexRaster, Observation
from forest_sentinel.storage import LocalStorage

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"

# Synthetic-band geospatial frame: small AOI in UTM zone 55S.
_TRANSFORM = Affine.translation(500_000.0, 9_300_000.0) * Affine.scale(30.0, -30.0)
_CRS = CRS.from_epsg(32755)
_HEIGHT = 8
_WIDTH = 8

# Same bbox as the synthetic raster, expressed in WGS 84 so it matches the
# real `compute_indices_for_observation` contract (AOI bbox is WGS 84).
from rasterio.warp import transform_bounds  # noqa: E402

_AOI_BBOX = transform_bounds(
    _CRS,
    "EPSG:4326",
    500_000.0,
    9_300_000.0 - _HEIGHT * 30.0,
    500_000.0 + _WIDTH * 30.0,
    9_300_000.0,
)


# ---------------------------------------------------------------------------
# Per-sensor band assets


def test_asset_for_hlsl30() -> None:
    assert asset_for("HLSL30", HlsBand.RED) == "B04"
    assert asset_for("HLSL30", HlsBand.NIR) == "B05"
    assert asset_for("HLSL30", HlsBand.SWIR2) == "B07"


def test_asset_for_hlss30_uses_narrow_nir() -> None:
    assert asset_for("HLSS30", HlsBand.RED) == "B04"
    assert asset_for("HLSS30", HlsBand.NIR) == "B8A"
    assert asset_for("HLSS30", HlsBand.SWIR2) == "B12"


def test_asset_for_unsupported_sensor_raises() -> None:
    with pytest.raises(ValueError, match="Unsupported"):
        asset_for("UNKNOWN", HlsBand.RED)


# ---------------------------------------------------------------------------
# Pure index math


def test_compute_nbr_matches_formula() -> None:
    nir = np.array([[0.5, 0.3]], dtype="float32")
    swir2 = np.array([[0.1, 0.3]], dtype="float32")
    expected = np.array([[(0.5 - 0.1) / (0.5 + 0.1), 0.0]], dtype="float32")
    np.testing.assert_allclose(compute_nbr(nir, swir2), expected, rtol=1e-5)


def test_compute_ndvi_matches_formula() -> None:
    nir = np.array([[0.6]], dtype="float32")
    red = np.array([[0.2]], dtype="float32")
    np.testing.assert_allclose(compute_ndvi(nir, red), [[0.5]], rtol=1e-5)


def test_indices_propagate_nan() -> None:
    nan = np.float32("nan")
    nir = np.array([[nan, 0.5]], dtype="float32")
    swir2 = np.array([[0.1, nan]], dtype="float32")
    assert np.isnan(compute_nbr(nir, swir2)).all()


def test_indices_handle_zero_denominator() -> None:
    nir = np.array([[0.0]], dtype="float32")
    swir2 = np.array([[0.0]], dtype="float32")
    assert np.isnan(compute_nbr(nir, swir2)[0, 0])


# ---------------------------------------------------------------------------
# read_band_window


def _write_band(
    path: Path,
    pixels: np.ndarray,
    transform: Affine = _TRANSFORM,
    crs: CRS = _CRS,
) -> Path:
    profile = {
        "driver": "GTiff",
        "dtype": "int16",
        "count": 1,
        "height": int(pixels.shape[0]),
        "width": int(pixels.shape[1]),
        "transform": transform,
        "crs": crs,
        "nodata": HLS_FILL_VALUE,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(pixels.astype("int16"), 1)
    return path


def test_read_band_window_masks_fill_and_scales(tmp_path: Path) -> None:
    pixels = np.full((_HEIGHT, _WIDTH), 1000, dtype="int16")
    pixels[0, 0] = HLS_FILL_VALUE
    path = _write_band(tmp_path / "band.tif", pixels)

    read = read_band_window(str(path), _AOI_BBOX)

    assert read.data.shape == (_HEIGHT, _WIDTH)
    assert np.isnan(read.data[0, 0])
    expected = 1000 * HLS_SCALE
    assert read.data[1, 1] == pytest.approx(expected, rel=1e-5)


# ---------------------------------------------------------------------------
# End-to-end compute_indices_for_observation


def _make_uniform_band(value_reflectance: float) -> np.ndarray:
    """An ``(H, W)`` int16 raster that scales to ``value_reflectance``."""
    raw = int(round(value_reflectance / HLS_SCALE))
    return np.full((_HEIGHT, _WIDTH), raw, dtype="int16")


def _seed_observation(db_session: Session, scene_id: str = "scene-1") -> Observation:
    aoi = persist_aoi(db_session, load_aoi_config(EXAMPLES / "aoi-sample.geojson"))
    db_session.flush()
    observation = Observation(
        aoi_id=aoi.id,
        sensor="HLSL30",
        acquired_at=datetime(2026, 1, 15, 12, 0, tzinfo=UTC),
        source_scene_id=scene_id,
        cloud_cover_percent=5.0,
    )
    db_session.add(observation)
    db_session.flush()
    return observation


def _stage_bands(
    root: Path,
    observation: Observation,
    *,
    red: float,
    nir: float,
    swir2: float,
) -> LocalBandResolver:
    scene_dir = root / observation.source_scene_id
    _write_band(scene_dir / "B04.tif", _make_uniform_band(red))
    _write_band(scene_dir / "B05.tif", _make_uniform_band(nir))
    _write_band(scene_dir / "B07.tif", _make_uniform_band(swir2))
    return LocalBandResolver(root=str(root))


def test_local_band_resolver_paths(tmp_path: Path) -> None:
    observation = Observation(
        aoi_id=1,
        sensor="HLSL30",
        acquired_at=datetime(2026, 1, 1, tzinfo=UTC),
        source_scene_id="scene-X",
    )
    resolver = LocalBandResolver(root=str(tmp_path))
    assert resolver.resolve(observation, HlsBand.NIR) == f"{tmp_path}/scene-X/B05.tif"


def test_compute_indices_writes_cogs_and_rows(db_session: Session, tmp_path: Path) -> None:
    observation = _seed_observation(db_session)
    resolver = _stage_bands(tmp_path / "bands", observation, red=0.10, nir=0.50, swir2=0.20)
    storage = LocalStorage(root=tmp_path / "cogs")
    methodology = get_or_create_methodology_version(
        db_session, name="optical-change", version="0.1", parameters={"baseline": "median"}
    )

    rows = compute_indices_for_observation(
        db_session,
        observation,
        methodology=methodology,
        storage=storage,
        resolver=resolver,
        aoi_bbox_wgs84=_AOI_BBOX,
        aoi_name="Example AOI",
    )
    db_session.commit()

    by_index = {row.index_type: row for row in rows}
    assert set(by_index) == {"nbr", "ndvi"}

    for row in rows:
        path = Path(row.cog_path)
        assert path.is_file()
        with rasterio.open(path) as src:
            data = src.read(1)
        expected = {
            "nbr": (0.50 - 0.20) / (0.50 + 0.20),
            "ndvi": (0.50 - 0.10) / (0.50 + 0.10),
        }[row.index_type]
        np.testing.assert_allclose(data, expected, rtol=1e-4)

    persisted = db_session.scalars(select(IndexRaster)).all()
    assert {r.index_type for r in persisted} == {"nbr", "ndvi"}
    assert all(r.observation_id == observation.id for r in persisted)
    assert all(r.methodology_version_id == methodology.id for r in persisted)


def test_compute_indices_is_idempotent(db_session: Session, tmp_path: Path) -> None:
    observation = _seed_observation(db_session)
    resolver = _stage_bands(tmp_path / "bands", observation, red=0.10, nir=0.50, swir2=0.20)
    storage = LocalStorage(root=tmp_path / "cogs")
    methodology = get_or_create_methodology_version(
        db_session, name="optical-change", version="0.1", parameters={}
    )

    first = compute_indices_for_observation(
        db_session,
        observation,
        methodology=methodology,
        storage=storage,
        resolver=resolver,
        aoi_bbox_wgs84=_AOI_BBOX,
        aoi_name="Example AOI",
    )
    db_session.commit()
    second = compute_indices_for_observation(
        db_session,
        observation,
        methodology=methodology,
        storage=storage,
        resolver=resolver,
        aoi_bbox_wgs84=_AOI_BBOX,
        aoi_name="Example AOI",
    )
    db_session.commit()

    assert {r.id for r in first} == {r.id for r in second}
    assert len(db_session.scalars(select(IndexRaster)).all()) == 2


def test_compute_indices_rejects_unknown_index_type(db_session: Session, tmp_path: Path) -> None:
    observation = _seed_observation(db_session)
    resolver = _stage_bands(tmp_path / "bands", observation, red=0.10, nir=0.50, swir2=0.20)
    storage = LocalStorage(root=tmp_path / "cogs")
    methodology = get_or_create_methodology_version(
        db_session, name="optical-change", version="0.1", parameters={}
    )

    with pytest.raises(ValueError, match="Unsupported index type"):
        compute_indices_for_observation(
            db_session,
            observation,
            methodology=methodology,
            storage=storage,
            resolver=resolver,
            aoi_bbox_wgs84=_AOI_BBOX,
            aoi_name="Example AOI",
            index_types=["mystery"],
        )


def test_band_resolver_is_a_structural_protocol() -> None:
    """Any object with a matching ``resolve`` satisfies :class:`BandResolver`."""

    class _Fake:
        def resolve(self, observation: Observation, band: HlsBand) -> str:
            return "x"

    resolver: BandResolver = _Fake()
    assert (
        resolver.resolve(
            Observation(
                aoi_id=1,
                sensor="HLSL30",
                acquired_at=datetime(2026, 1, 1, tzinfo=UTC),
                source_scene_id="x",
            ),
            HlsBand.RED,
        )
        == "x"
    )
