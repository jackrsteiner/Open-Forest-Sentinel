"""Compute per-observation NBR and NDVI from HLS bands.

For each ``observation`` the pipeline reads the relevant HLS bands (RED, NIR,
SWIR2) AOI-windowed, computes NBR and NDVI as float reflectance ratios, writes
each as a Cloud Optimized GeoTIFF through :mod:`forest_sentinel.storage`, and
records one ``index_raster`` row per index (with provenance back to the source
observation and a methodology version).

Band reading is decoupled from the rest of the pipeline through a
:class:`BandResolver` protocol: production code uses an HLS-aware resolver
that returns authenticated S3/HTTPS URLs; tests use a local-path resolver
that points at fixture rasters. This bead ships :class:`LocalBandResolver`
for fixture and developer use; bead #42 wires up the production resolver.

HLS surface reflectance is stored as scaled ``int16`` with fill value
``-9999`` and scale factor ``0.0001`` per the HLS v2.0 user guide. This
module converts to float, masks fill values to ``NaN``, and propagates
``NaN`` through index math so downstream change detection can ignore
missing pixels honestly.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

import numpy as np
import rasterio
from affine import Affine
from rasterio.crs import CRS
from rasterio.windows import from_bounds
from sqlalchemy import select
from sqlalchemy.orm import Session

from forest_sentinel.models import IndexRaster, MethodologyVersion, Observation
from forest_sentinel.storage import CogKey, Storage

# HLS v2.0 surface-reflectance scaling.
HLS_SCALE = 0.0001
HLS_FILL_VALUE = -9999

# Indices this module produces. Order is significant: tests assert on it.
INDEX_TYPES: tuple[str, ...] = ("nbr", "ndvi")


class HlsBand(StrEnum):
    """HLS spectral bands referenced by NBR/NDVI computation."""

    RED = "red"
    NIR = "nir"
    SWIR2 = "swir2"


# (sensor, band) → HLS asset short name (HLS v2.0 file naming).
# HLSS30 uses the *narrow* NIR (B8A) so HLSL30 and HLSS30 indices are
# directly comparable in change detection.
_BAND_ASSETS: dict[tuple[str, HlsBand], str] = {
    ("HLSL30", HlsBand.RED): "B04",
    ("HLSL30", HlsBand.NIR): "B05",
    ("HLSL30", HlsBand.SWIR2): "B07",
    ("HLSS30", HlsBand.RED): "B04",
    ("HLSS30", HlsBand.NIR): "B8A",
    ("HLSS30", HlsBand.SWIR2): "B12",
}


def asset_for(sensor: str, band: HlsBand) -> str:
    """Return the HLS asset short name for ``(sensor, band)``."""
    try:
        return _BAND_ASSETS[(sensor, band)]
    except KeyError as exc:
        raise ValueError(f"Unsupported (sensor, band): ({sensor!r}, {band})") from exc


class BandResolver(Protocol):
    """Resolves an observation + band to a readable raster path or URL."""

    def resolve(self, observation: Observation, band: HlsBand) -> str:
        """Return a path or URL ``rasterio.open`` can read for this band."""
        ...


@dataclass(frozen=True)
class LocalBandResolver:
    """A :class:`BandResolver` that maps observations to local files.

    Files are expected at ``{root}/{source_scene_id}/{asset}.tif`` where
    ``asset`` is the HLS short name returned by :func:`asset_for`.
    """

    root: str

    def resolve(self, observation: Observation, band: HlsBand) -> str:
        return (
            f"{self.root}/{observation.source_scene_id}/{asset_for(observation.sensor, band)}.tif"
        )


@dataclass(frozen=True)
class BandRead:
    """A windowed band read in HLS reflectance units (``NaN`` where masked)."""

    data: np.ndarray
    transform: Affine
    crs: CRS


def read_band_window(path: str, bbox: tuple[float, float, float, float]) -> BandRead:
    """Read ``path`` AOI-windowed; mask the HLS fill value; scale to reflectance."""
    with rasterio.open(path) as src:
        window = from_bounds(*bbox, transform=src.transform)
        raw = src.read(1, window=window, boundless=False)
        transform = src.window_transform(window)
        crs = src.crs

    data = raw.astype("float32")
    data[raw == HLS_FILL_VALUE] = np.nan
    data *= HLS_SCALE
    return BandRead(data=data, transform=transform, crs=crs)


def compute_nbr(nir: np.ndarray, swir2: np.ndarray) -> np.ndarray:
    """NBR = (NIR - SWIR2) / (NIR + SWIR2). Zero denominators → NaN."""
    return _ratio(nir - swir2, nir + swir2)


def compute_ndvi(nir: np.ndarray, red: np.ndarray) -> np.ndarray:
    """NDVI = (NIR - RED) / (NIR + RED). Zero denominators → NaN."""
    return _ratio(nir - red, nir + red)


def _ratio(numerator: np.ndarray, denominator: np.ndarray) -> np.ndarray:
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(denominator != 0, numerator / denominator, np.float32(np.nan))


def compute_indices_for_observation(
    session: Session,
    observation: Observation,
    *,
    methodology: MethodologyVersion,
    storage: Storage,
    resolver: BandResolver,
    aoi_bbox: tuple[float, float, float, float],
    aoi_name: str,
    index_types: Iterable[str] = INDEX_TYPES,
) -> list[IndexRaster]:
    """Compute the configured indices for ``observation`` and persist their rasters.

    Re-running with the same ``(observation, index_type, methodology)``
    overwrites the COG on disk and updates the existing ``index_raster``
    row's ``cog_path`` rather than creating a duplicate.
    """
    red = read_band_window(resolver.resolve(observation, HlsBand.RED), aoi_bbox)
    nir = read_band_window(resolver.resolve(observation, HlsBand.NIR), aoi_bbox)
    swir2 = read_band_window(resolver.resolve(observation, HlsBand.SWIR2), aoi_bbox)

    arrays = {
        "nbr": compute_nbr(nir.data, swir2.data),
        "ndvi": compute_ndvi(nir.data, red.data),
    }

    rows: list[IndexRaster] = []
    acquired_on = observation.acquired_at.date()
    for index_type in index_types:
        if index_type not in arrays:
            raise ValueError(f"Unsupported index type: {index_type!r}")
        cog_path = storage.write_cog(
            CogKey(
                aoi=aoi_name,
                product=index_type,
                acquired_on=acquired_on,
                filename=f"{index_type}.tif",
            ),
            arrays[index_type],
            transform=nir.transform,
            crs=nir.crs,
            nodata=float("nan"),
        )
        rows.append(
            _upsert_index_raster(
                session,
                observation_id=observation.id,
                methodology_id=methodology.id,
                index_type=index_type,
                cog_path=str(cog_path),
            )
        )
    session.flush()
    return rows


def _upsert_index_raster(
    session: Session,
    *,
    observation_id: int,
    methodology_id: int,
    index_type: str,
    cog_path: str,
) -> IndexRaster:
    existing = session.scalars(
        select(IndexRaster).where(
            IndexRaster.observation_id == observation_id,
            IndexRaster.methodology_version_id == methodology_id,
            IndexRaster.index_type == index_type,
        )
    ).one_or_none()
    if existing is not None:
        existing.cog_path = cog_path
        return existing

    row = IndexRaster(
        observation_id=observation_id,
        methodology_version_id=methodology_id,
        index_type=index_type,
        cog_path=cog_path,
    )
    session.add(row)
    return row
