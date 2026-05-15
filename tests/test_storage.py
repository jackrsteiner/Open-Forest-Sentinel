from datetime import date
from pathlib import Path

import numpy as np
import pytest
import rasterio
from affine import Affine
from rasterio.crs import CRS
from rio_cogeo.cogeo import cog_validate

from forest_sentinel.storage import (
    DEFAULT_ROOT,
    ROOT_ENV_VAR,
    CogKey,
    LocalStorage,
    get_storage_root,
)


def _key(filename: str = "nbr.tif") -> CogKey:
    return CogKey(
        aoi="Example AOI",
        product="nbr",
        acquired_on=date(2026, 1, 15),
        filename=filename,
    )


def test_default_storage_root() -> None:
    assert get_storage_root() == DEFAULT_ROOT


def test_storage_root_env_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(ROOT_ENV_VAR, str(tmp_path / "elsewhere"))
    assert get_storage_root() == tmp_path / "elsewhere"


def test_path_layout(tmp_path: Path) -> None:
    storage = LocalStorage(root=tmp_path)
    path = storage.path_for(_key())
    assert path == tmp_path / "Example_AOI" / "nbr" / "2026-01-15" / "nbr.tif"


def test_path_layout_sanitizes_unsafe_components(tmp_path: Path) -> None:
    storage = LocalStorage(root=tmp_path)
    path = storage.path_for(
        CogKey(
            aoi="../escape",
            product="weird/name",
            acquired_on=date(2026, 1, 15),
            filename="x.tif",
        )
    )
    assert ".." not in path.parts
    assert all("/" not in part for part in path.parts[len(tmp_path.parts) :])


def test_empty_path_component_is_rejected(tmp_path: Path) -> None:
    storage = LocalStorage(root=tmp_path)
    with pytest.raises(ValueError, match="empty"):
        storage.path_for(
            CogKey(aoi="", product="nbr", acquired_on=date(2026, 1, 15), filename="x.tif")
        )


def test_write_cog_round_trip(tmp_path: Path) -> None:
    storage = LocalStorage(root=tmp_path)
    data = np.linspace(-1.0, 1.0, num=64 * 64, dtype="float32").reshape(64, 64)
    transform = Affine.translation(100_000.0, 200_000.0) * Affine.scale(30.0, -30.0)
    crs = CRS.from_epsg(32755)

    destination = storage.write_cog(_key(), data, transform=transform, crs=crs, nodata=-9999.0)

    assert destination.is_file()
    assert destination == tmp_path / "Example_AOI" / "nbr" / "2026-01-15" / "nbr.tif"

    is_valid, errors, _warnings = cog_validate(str(destination), quiet=True)
    assert is_valid, errors

    with rasterio.open(destination) as src:
        assert src.count == 1
        assert src.width == 64
        assert src.height == 64
        assert src.crs == crs
        assert src.nodata == pytest.approx(-9999.0)
        read = src.read(1)
        np.testing.assert_allclose(read, data, rtol=0, atol=1e-6)


def test_write_cog_accepts_3d_input(tmp_path: Path) -> None:
    storage = LocalStorage(root=tmp_path)
    data = np.zeros((2, 32, 32), dtype="float32")
    data[1, :, :] = 0.5
    transform = Affine.translation(0.0, 0.0) * Affine.scale(30.0, -30.0)
    crs = CRS.from_epsg(4326)

    destination = storage.write_cog(_key("two-band.tif"), data, transform=transform, crs=crs)

    with rasterio.open(destination) as src:
        assert src.count == 2
        np.testing.assert_allclose(src.read(2), data[1])


def test_write_cog_rejects_non_2d_or_3d_data(tmp_path: Path) -> None:
    storage = LocalStorage(root=tmp_path)
    transform = Affine.identity()
    crs = CRS.from_epsg(4326)
    with pytest.raises(ValueError, match="2-D or 3-D"):
        storage.write_cog(
            _key(),
            np.zeros((4, 4, 4, 4), dtype="float32"),
            transform=transform,
            crs=crs,
        )


def test_write_cog_creates_parent_directories(tmp_path: Path) -> None:
    storage = LocalStorage(root=tmp_path / "fresh" / "root")
    data = np.zeros((4, 4), dtype="float32")
    transform = Affine.translation(0.0, 1000.0) * Affine.scale(30.0, -30.0)
    crs = CRS.from_epsg(4326)

    destination = storage.write_cog(_key(), data, transform=transform, crs=crs)
    assert destination.is_file()
    assert (tmp_path / "fresh" / "root" / "Example_AOI" / "nbr" / "2026-01-15").is_dir()
