"""Raster storage abstraction.

Cloud Optimized GeoTIFFs (COGs) are the on-disk format for the index and
change rasters Slice 1 produces. This module hides the storage backend behind
one small interface: today, COGs land on a local filesystem root (the
prototype path on the GCE VM); tomorrow they may move to Google Cloud
Storage. Swapping backends should touch only this module.

The storage root is configurable via ``FOREST_SENTINEL_COG_ROOT`` and defaults
to ``data/cogs`` (relative to the working directory). Paths are laid out as::

    {root}/{aoi}/{product}/{YYYY-MM-DD}/{filename}

so artifacts for one AOI, one product, and one acquisition date group
together for easy inspection and bulk listing.
"""

import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Protocol

import numpy as np
from affine import Affine
from rasterio.crs import CRS
from rasterio.io import MemoryFile
from rio_cogeo.cogeo import cog_translate
from rio_cogeo.profiles import cog_profiles

DEFAULT_ROOT = Path("data/cogs")
ROOT_ENV_VAR = "FOREST_SENTINEL_COG_ROOT"


def get_storage_root() -> Path:
    """Return the configured raster storage root (``FOREST_SENTINEL_COG_ROOT`` or default)."""
    value = os.environ.get(ROOT_ENV_VAR)
    return Path(value) if value else DEFAULT_ROOT


@dataclass(frozen=True)
class CogKey:
    """Identifies one COG within the storage layout.

    ``aoi`` and ``product`` are sanitized into path components (alphanumerics,
    dashes, and underscores are kept; anything else becomes ``_``), so callers
    can pass human-readable names like ``"Example AOI"`` without surprises.
    """

    aoi: str
    product: str
    acquired_on: date
    filename: str


class Storage(Protocol):
    """Storage backend for raster artifacts. Swappable; one implementation today."""

    def path_for(self, key: CogKey) -> Path:
        """Return the path a COG with ``key`` would be (or has been) written to."""
        ...

    def write_cog(
        self,
        key: CogKey,
        data: np.ndarray,
        *,
        transform: Affine,
        crs: CRS,
        nodata: float | None = None,
    ) -> Path:
        """Write ``data`` as a valid COG and return its path."""
        ...


class LocalStorage:
    """A :class:`Storage` implementation that writes COGs under a local-filesystem root."""

    def __init__(self, root: Path | None = None) -> None:
        self._root = Path(root) if root is not None else get_storage_root()

    @property
    def root(self) -> Path:
        return self._root

    def path_for(self, key: CogKey) -> Path:
        return (
            self._root
            / _safe_component(key.aoi)
            / _safe_component(key.product)
            / key.acquired_on.isoformat()
            / key.filename
        )

    def write_cog(
        self,
        key: CogKey,
        data: np.ndarray,
        *,
        transform: Affine,
        crs: CRS,
        nodata: float | None = None,
    ) -> Path:
        if data.ndim == 2:
            data = data[np.newaxis, :, :]
        if data.ndim != 3:
            raise ValueError(f"data must be 2-D or 3-D, got {data.ndim}-D")

        destination = self.path_for(key)
        destination.parent.mkdir(parents=True, exist_ok=True)

        profile: dict[str, Any] = {
            "driver": "GTiff",
            "dtype": str(data.dtype),
            "count": int(data.shape[0]),
            "height": int(data.shape[1]),
            "width": int(data.shape[2]),
            "transform": transform,
            "crs": crs,
        }
        if nodata is not None:
            profile["nodata"] = nodata

        # Stage the array in an in-memory GeoTIFF, then let rio-cogeo translate
        # it to a fully-conformant COG on disk (tiled, with overviews, IFD
        # order, etc.).
        with MemoryFile() as memfile:
            with memfile.open(**profile) as src:
                src.write(data)
            with memfile.open() as src:
                cog_translate(
                    src,
                    str(destination),
                    cog_profiles.get("deflate"),  # type: ignore[no-untyped-call]
                    in_memory=True,
                    quiet=True,
                )
        return destination


def _safe_component(value: str) -> str:
    """Map a free-form name to a safe single path component."""
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in value)
    if not safe:
        raise ValueError(f"path component is empty after sanitization: {value!r}")
    return safe
