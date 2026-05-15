"""Per-pixel ΔNBR / ΔNDVI against a trailing-median baseline.

For a given ``observation`` with index rasters from bead #39, this module:

1. Reads the current observation's index COG (NBR or NDVI).
2. Reads the trailing window of valid prior observations' index COGs for the
   same AOI under the same methodology.
3. Computes the per-pixel ``np.nanmedian`` baseline across the window.
4. Computes ``delta = current - baseline``.
5. Writes the delta as a COG through :mod:`forest_sentinel.storage`.
6. Persists a ``change_raster`` row with provenance to the source observation,
   to every contributing ``index_raster`` (current + baseline window), and to
   the methodology version.

The trailing window size is read from the methodology's parameters
(``baseline_window``, default :data:`DEFAULT_BASELINE_WINDOW`). If there are
no prior valid observations for an index type, that index type is **skipped
silently** for this run — a fresh AOI just doesn't have change products yet.
The next run with one more observation produces deltas.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import numpy as np
import rasterio
from affine import Affine
from rasterio.crs import CRS
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from forest_sentinel.models import (
    ChangeRaster,
    ChangeRasterSource,
    IndexRaster,
    MethodologyVersion,
    Observation,
)
from forest_sentinel.storage import CogKey, Storage

DEFAULT_BASELINE_WINDOW = 5

# Maps an index_type to the corresponding change_type (kept distinct so the
# domain reads naturally: "nbr" indices produce "delta_nbr" change rasters).
CHANGE_TYPE_BY_INDEX: dict[str, str] = {
    "nbr": "delta_nbr",
    "ndvi": "delta_ndvi",
}


@dataclass(frozen=True)
class _RasterRead:
    data: np.ndarray
    transform: Affine
    crs: CRS


def compute_change_products_for_observation(
    session: Session,
    observation: Observation,
    *,
    methodology: MethodologyVersion,
    storage: Storage,
    aoi_name: str,
    baseline_window: int | None = None,
    index_types: Iterable[str] = ("nbr", "ndvi"),
) -> list[ChangeRaster]:
    """Compute trailing-median change products for ``observation``.

    Returns the ``change_raster`` rows that were produced. Index types with no
    prior baseline are skipped silently. Re-runs upsert: an existing
    ``change_raster`` for ``(observation, change_type, methodology)`` has its
    COG and source-provenance rows replaced rather than duplicated.
    """
    window_size = _resolve_window_size(methodology, baseline_window)
    produced: list[ChangeRaster] = []

    for index_type in index_types:
        if index_type not in CHANGE_TYPE_BY_INDEX:
            raise ValueError(f"Unsupported index type: {index_type!r}")

        current = _current_index_raster(session, observation, methodology, index_type)
        if current is None:
            continue  # this observation has no index of this type to compare against

        baseline = _baseline_index_rasters(
            session, observation, methodology, index_type, window_size
        )
        if not baseline:
            continue  # no prior valid observations yet; skip silently

        current_read = _read_cog(current.cog_path)
        baseline_reads = [_read_cog(ir.cog_path) for ir in baseline]
        _require_aligned(current_read, baseline_reads)

        baseline_array = np.nanmedian(np.stack([r.data for r in baseline_reads], axis=0), axis=0)
        delta = current_read.data - baseline_array

        change_type = CHANGE_TYPE_BY_INDEX[index_type]
        cog_path = storage.write_cog(
            CogKey(
                aoi=aoi_name,
                product=change_type,
                acquired_on=observation.acquired_at.date(),
                filename=f"{change_type}.tif",
            ),
            delta.astype("float32"),
            transform=current_read.transform,
            crs=current_read.crs,
            nodata=float("nan"),
        )

        row = _upsert_change_raster(
            session,
            observation_id=observation.id,
            methodology_id=methodology.id,
            change_type=change_type,
            cog_path=str(cog_path),
        )
        session.flush()
        _replace_sources(session, row, [current, *baseline])
        produced.append(row)

    session.flush()
    return produced


def _resolve_window_size(methodology: MethodologyVersion, override: int | None) -> int:
    if override is not None:
        return int(override)
    parameters: dict[str, Any] = methodology.parameters or {}
    return int(parameters.get("baseline_window", DEFAULT_BASELINE_WINDOW))


def _current_index_raster(
    session: Session,
    observation: Observation,
    methodology: MethodologyVersion,
    index_type: str,
) -> IndexRaster | None:
    return session.scalars(
        select(IndexRaster).where(
            IndexRaster.observation_id == observation.id,
            IndexRaster.methodology_version_id == methodology.id,
            IndexRaster.index_type == index_type,
        )
    ).one_or_none()


def _baseline_index_rasters(
    session: Session,
    observation: Observation,
    methodology: MethodologyVersion,
    index_type: str,
    window_size: int,
) -> list[IndexRaster]:
    return list(
        session.scalars(
            select(IndexRaster)
            .join(Observation, Observation.id == IndexRaster.observation_id)
            .where(
                Observation.aoi_id == observation.aoi_id,
                Observation.acquired_at < observation.acquired_at,
                IndexRaster.methodology_version_id == methodology.id,
                IndexRaster.index_type == index_type,
            )
            .order_by(Observation.acquired_at.desc())
            .limit(window_size)
        )
    )


def _read_cog(path: str) -> _RasterRead:
    with rasterio.open(path) as src:
        return _RasterRead(
            data=src.read(1).astype("float32"),
            transform=src.transform,
            crs=src.crs,
        )


def _require_aligned(current: _RasterRead, baseline: list[_RasterRead]) -> None:
    for other in baseline:
        if other.data.shape != current.data.shape:
            raise ValueError(
                "Change baseline rasters must share the current raster's shape; "
                f"got {other.data.shape} vs {current.data.shape}"
            )
        if other.transform != current.transform:
            raise ValueError("Change baseline rasters must share the current raster's transform")
        if other.crs != current.crs:
            raise ValueError("Change baseline rasters must share the current raster's CRS")


def _upsert_change_raster(
    session: Session,
    *,
    observation_id: int,
    methodology_id: int,
    change_type: str,
    cog_path: str,
) -> ChangeRaster:
    existing = session.scalars(
        select(ChangeRaster).where(
            ChangeRaster.observation_id == observation_id,
            ChangeRaster.methodology_version_id == methodology_id,
            ChangeRaster.change_type == change_type,
        )
    ).one_or_none()
    if existing is not None:
        existing.cog_path = cog_path
        return existing

    row = ChangeRaster(
        observation_id=observation_id,
        methodology_version_id=methodology_id,
        change_type=change_type,
        cog_path=cog_path,
    )
    session.add(row)
    return row


def _replace_sources(
    session: Session,
    change_raster: ChangeRaster,
    contributing: list[IndexRaster],
) -> None:
    session.execute(
        delete(ChangeRasterSource).where(ChangeRasterSource.change_raster_id == change_raster.id)
    )
    for index_raster in contributing:
        session.add(
            ChangeRasterSource(
                change_raster_id=change_raster.id,
                index_raster_id=index_raster.id,
            )
        )
