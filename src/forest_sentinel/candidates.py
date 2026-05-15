"""Extract disturbance-candidate polygons from a ΔNBR change raster.

Algorithm: read the change raster, build a binary mask of pixels where
``delta <= threshold`` (an NBR drop of at least ``-threshold``), polygonize
the mask with :func:`rasterio.features.shapes`, drop polygons smaller than
``min_area_m2`` (computed in the raster's native projected CRS, so area is
in square metres), reproject each surviving polygon to WGS 84, and persist
it as a ``disturbance_candidate`` row.

Detection thresholds are policy and configurable, with documented defaults
(:data:`DEFAULT_DELTA_NBR_THRESHOLD`, :data:`DEFAULT_MIN_AREA_M2`). Either
or both can be set on the methodology's ``parameters`` so the run that
produces a candidate carries the exact rule used in its provenance.

NaN pixels in the change raster never satisfy ``<= threshold`` and so are
naturally treated as "not disturbed" without special handling.
"""

from datetime import date

import numpy as np
import rasterio
from affine import Affine
from geoalchemy2.shape import from_shape
from pyproj import Transformer
from rasterio.features import shapes
from shapely.geometry import Polygon
from shapely.geometry import shape as shapely_shape
from shapely.ops import transform as shapely_transform
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from forest_sentinel.models import (
    AOI_SRID,
    ChangeRaster,
    DisturbanceCandidate,
    MethodologyVersion,
    Observation,
)

DEFAULT_DELTA_NBR_THRESHOLD = -0.25
DEFAULT_MIN_AREA_M2 = 4500.0  # ≈ 0.45 ha — about a 50 m × 90 m patch


def extract_candidates_for_change_raster(
    session: Session,
    change_raster: ChangeRaster,
    *,
    methodology: MethodologyVersion,
    delta_threshold: float | None = None,
    min_area_m2: float | None = None,
) -> list[DisturbanceCandidate]:
    """Threshold, polygonize, and persist candidate disturbance polygons.

    Re-runs replace any existing candidates for the same
    ``(change_raster, methodology)`` rather than duplicating, so the row set
    always reflects the latest run's parameters.
    """
    parameters = methodology.parameters or {}
    threshold = (
        delta_threshold
        if delta_threshold is not None
        else float(parameters.get("delta_nbr_threshold", DEFAULT_DELTA_NBR_THRESHOLD))
    )
    minimum_area_m2 = (
        min_area_m2
        if min_area_m2 is not None
        else float(parameters.get("min_area_m2", DEFAULT_MIN_AREA_M2))
    )

    with rasterio.open(change_raster.cog_path) as src:
        data = src.read(1)
        raster_transform = src.transform
        raster_crs = src.crs

    # NaN comparisons are False, so NaN pixels are excluded automatically.
    mask = (data <= threshold).astype("uint8")
    polygons_native = _polygonize_mask(mask, raster_transform, minimum_area_m2)

    detected_at = _observation_acquired_on(session, change_raster.observation_id)
    to_wgs84 = Transformer.from_crs(raster_crs, f"EPSG:{AOI_SRID}", always_xy=True).transform

    session.execute(
        delete(DisturbanceCandidate).where(
            DisturbanceCandidate.change_raster_id == change_raster.id,
            DisturbanceCandidate.methodology_version_id == methodology.id,
        )
    )

    rows: list[DisturbanceCandidate] = []
    for polygon in polygons_native:
        wgs84_polygon = shapely_transform(to_wgs84, polygon)
        row = DisturbanceCandidate(
            change_raster_id=change_raster.id,
            methodology_version_id=methodology.id,
            geometry=from_shape(wgs84_polygon, srid=AOI_SRID),
            detected_at=detected_at,
            area_m2=float(polygon.area),
        )
        session.add(row)
        rows.append(row)
    session.flush()
    return rows


def _polygonize_mask(
    mask: np.ndarray,
    transform: Affine,
    minimum_area_m2: float,
) -> list[Polygon]:
    polygons: list[Polygon] = []
    for geom, value in shapes(mask, transform=transform):
        if value != 1:
            continue
        polygon = shapely_shape(geom)
        if not isinstance(polygon, Polygon):
            continue
        if polygon.area >= minimum_area_m2:
            polygons.append(polygon)
    return polygons


def _observation_acquired_on(session: Session, observation_id: int) -> date:
    observation = session.scalars(select(Observation).where(Observation.id == observation_id)).one()
    return observation.acquired_at.date()
