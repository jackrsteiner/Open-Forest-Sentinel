from pathlib import Path
from typing import Any

import earthaccess
import numpy as np
import pytest
import rasterio
from affine import Affine
from geoalchemy2.shape import to_shape
from rasterio.crs import CRS
from shapely.geometry import mapping
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from forest_sentinel.cli import main
from forest_sentinel.indices import HLS_FILL_VALUE
from forest_sentinel.models import (
    Aoi,
    ChangeRaster,
    DisturbanceCandidate,
    IndexRaster,
    Observation,
)

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
SAMPLE_AOI = EXAMPLES / "aoi-sample.geojson"

# The sample AOI sits near 10°E, 50°N. Stage the synthetic HLS bands in
# UTM zone 32N (which covers 6°E–12°E) so the rasters actually intersect
# the AOI's WGS 84 bounding box and area calculations come out in square
# metres.
_TRANSFORM = Affine.translation(572_000.0, 5_555_000.0) * Affine.scale(30.0, -30.0)
_CRS = CRS.from_epsg(32632)
_HEIGHT = 12
_WIDTH = 12


def _stub_earthaccess(
    monkeypatch: pytest.MonkeyPatch,
    by_short_name: dict[str, list[dict[str, Any]]],
) -> None:
    def fake(**kwargs: Any) -> list[dict[str, Any]]:
        return by_short_name.get(kwargs["short_name"], [])

    monkeypatch.setattr(earthaccess, "search_data", fake)


def _granule_payload(
    granule_ur: str, beginning_datetime: str, cloud_cover_percent: float
) -> dict[str, Any]:
    return {
        "umm": {
            "GranuleUR": granule_ur,
            "TemporalExtent": {"RangeDateTime": {"BeginningDateTime": beginning_datetime}},
            "AdditionalAttributes": [
                {"Name": "CLOUD_COVERAGE", "Values": [str(cloud_cover_percent)]}
            ],
        }
    }


def _stage_band(path: Path, value_reflectance: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = int(round(value_reflectance / 0.0001))
    pixels = np.full((_HEIGHT, _WIDTH), raw, dtype="int16")
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        dtype="int16",
        count=1,
        height=_HEIGHT,
        width=_WIDTH,
        transform=_TRANSFORM,
        crs=_CRS,
        nodata=HLS_FILL_VALUE,
    ) as dst:
        dst.write(pixels, 1)


def _stage_scene(band_root: Path, scene_id: str, *, red: float, nir: float, swir2: float) -> None:
    _stage_band(band_root / scene_id / "B04.tif", red)
    _stage_band(band_root / scene_id / "B05.tif", nir)
    _stage_band(band_root / scene_id / "B07.tif", swir2)


# ---------------------------------------------------------------------------
# AOI / argument-parsing / DB-connection paths (Slice 0 surface, evolved)


def test_run_persists_aoi_and_reports(
    migrated_database: Engine,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_earthaccess(monkeypatch, {})

    exit_code = main(
        [
            "run",
            "--aoi",
            str(SAMPLE_AOI),
            "--since",
            "2026-01-01",
            "--until",
            "2026-01-31",
        ]
    )
    assert exit_code == 0

    output = capsys.readouterr().out
    assert "Example AOI" in output
    assert "id=" in output
    assert "Total AOIs in database: 1" in output
    assert "observations discovered: 0" in output
    assert "band-root not provided" in output

    with Session(migrated_database) as session:
        assert [row.name for row in session.scalars(select(Aoi))] == ["Example AOI"]


def test_run_with_bad_config_exits_nonzero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = main(
        [
            "run",
            "--aoi",
            str(tmp_path / "missing.geojson"),
            "--since",
            "2026-01-01",
            "--until",
            "2026-01-31",
        ]
    )
    assert exit_code == 1
    assert "error:" in capsys.readouterr().err


def test_run_is_idempotent_for_same_aoi(
    migrated_database: Engine,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two runs with the same AOI reuse the row instead of erroring."""
    _stub_earthaccess(monkeypatch, {})

    args = [
        "run",
        "--aoi",
        str(SAMPLE_AOI),
        "--since",
        "2026-01-01",
        "--until",
        "2026-01-31",
    ]
    assert main(args) == 0
    capsys.readouterr()
    assert main(args) == 0
    output = capsys.readouterr().out
    assert "Total AOIs in database: 1" in output


def test_run_reports_database_connection_error(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(
        "FOREST_SENTINEL_DATABASE_URL",
        "postgresql+psycopg://nobody:nobody@localhost:1/nowhere",
    )
    exit_code = main(
        [
            "run",
            "--aoi",
            str(SAMPLE_AOI),
            "--since",
            "2026-01-01",
            "--until",
            "2026-01-31",
        ]
    )
    assert exit_code == 1
    assert "could not connect to the database" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Slice 1 hallway test: end-to-end pipeline produces candidate polygons


def test_run_full_pipeline_produces_candidates(
    migrated_database: Engine,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    band_root = tmp_path / "bands"
    cog_root = tmp_path / "cogs"
    monkeypatch.setenv("FOREST_SENTINEL_COG_ROOT", str(cog_root))

    # Five baseline scenes with healthy NBR, then a sixth scene where NBR
    # drops sharply (post-disturbance). The pipeline should produce a
    # disturbance candidate on the sixth observation.
    baselines = [
        ("scene-01", "2026-01-05T12:00:00Z", 0.10, 0.60, 0.10),
        ("scene-02", "2026-01-10T12:00:00Z", 0.10, 0.60, 0.10),
        ("scene-03", "2026-01-15T12:00:00Z", 0.10, 0.60, 0.10),
        ("scene-04", "2026-01-20T12:00:00Z", 0.10, 0.60, 0.10),
        ("scene-05", "2026-01-25T12:00:00Z", 0.10, 0.60, 0.10),
    ]
    disturbed = ("scene-06", "2026-01-30T12:00:00Z", 0.30, 0.30, 0.40)

    for scene_id, _, red, nir, swir2 in [*baselines, disturbed]:
        _stage_scene(band_root, scene_id, red=red, nir=nir, swir2=swir2)

    _stub_earthaccess(
        monkeypatch,
        {
            "HLSL30": [
                _granule_payload(scene_id, when, 5.0)
                for scene_id, when, *_ in [*baselines, disturbed]
            ],
            "HLSS30": [],
        },
    )

    exit_code = main(
        [
            "run",
            "--aoi",
            str(SAMPLE_AOI),
            "--since",
            "2026-01-01",
            "--until",
            "2026-01-31",
            "--band-root",
            str(band_root),
        ]
    )
    assert exit_code == 0

    output = capsys.readouterr().out
    assert "observations discovered: 6" in output
    assert "observations recorded:   6" in output
    assert "index rasters:           12" in output  # 2 indices × 6 obs
    # Only observations with a prior baseline get change products: obs 2..6 = 5 obs × 2 indices.
    assert "change rasters:          10" in output
    assert "disturbance candidates:  1" in output

    with Session(migrated_database) as session:
        observations = session.scalars(select(Observation)).all()
        index_rasters = session.scalars(select(IndexRaster)).all()
        change_rasters = session.scalars(select(ChangeRaster)).all()
        candidates = session.scalars(select(DisturbanceCandidate)).all()

        assert len(observations) == 6
        assert len(index_rasters) == 12
        assert len(change_rasters) == 10
        assert len(candidates) == 1
        [candidate] = candidates
        polygon = to_shape(candidate.geometry)
        # The candidate is in WGS 84 and can be dumped to GeoJSON for review.
        geojson = mapping(polygon)
        assert geojson["type"] == "Polygon"
        assert candidate.area_m2 > 0
        assert candidate.detected_at.isoformat() == "2026-01-30"
