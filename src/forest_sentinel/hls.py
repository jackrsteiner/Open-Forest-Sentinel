"""HLS scene discovery.

Given an AOI and a time window, ``discover_observations`` enumerates the
HLSL30 (Landsat 8/9) and HLSS30 (Sentinel-2) granules that intersect the
AOI's bounding box and records them as ``observation`` rows. This is the
metadata-only step of HLS ingestion; band pixel data is read later when
indices are computed.

The provider library is `earthaccess`_, NASA's official Earthdata client. It
talks to the CMR for discovery (no authentication required) and handles
Earthdata Login when (later) bands are read directly. Re-running discovery
is idempotent per AOI thanks to the ``observation`` unique constraint on
``(aoi_id, source_scene_id)``.

.. _earthaccess: https://github.com/nsidc/earthaccess
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

import earthaccess
from geoalchemy2.shape import to_shape
from sqlalchemy import select
from sqlalchemy.orm import Session

from forest_sentinel.models import Aoi, Observation

HLS_SHORT_NAMES: tuple[str, ...] = ("HLSL30", "HLSS30")


@dataclass(frozen=True)
class HlsGranule:
    """A parsed HLS granule: just the fields the ``observation`` table cares about."""

    sensor: str
    source_scene_id: str
    acquired_at: datetime
    cloud_cover_percent: float | None


@dataclass(frozen=True)
class DiscoveryResult:
    """Summary of one ``discover_observations`` call."""

    discovered: int
    recorded: int
    skipped: int


def discover_observations(
    session: Session,
    aoi: Aoi,
    *,
    since: date,
    until: date,
    short_names: Iterable[str] = HLS_SHORT_NAMES,
) -> DiscoveryResult:
    """Discover HLS granules for ``aoi`` between ``since`` and ``until`` (inclusive).

    Granules already recorded for this AOI are skipped; new ones are inserted
    as ``observation`` rows and flushed. Returns counts so the caller can log
    or report a summary.
    """
    bbox = _aoi_bounding_box(aoi)
    granules = search_hls_granules(bbox, since=since, until=until, short_names=short_names)

    existing = {
        scene_id
        for (scene_id,) in session.execute(
            select(Observation.source_scene_id).where(Observation.aoi_id == aoi.id)
        )
    }

    new_granules = [g for g in granules if g.source_scene_id not in existing]
    for granule in new_granules:
        session.add(
            Observation(
                aoi_id=aoi.id,
                sensor=granule.sensor,
                acquired_at=granule.acquired_at,
                source_scene_id=granule.source_scene_id,
                cloud_cover_percent=granule.cloud_cover_percent,
            )
        )
    session.flush()

    return DiscoveryResult(
        discovered=len(granules),
        recorded=len(new_granules),
        skipped=len(granules) - len(new_granules),
    )


def search_hls_granules(
    bbox: tuple[float, float, float, float],
    *,
    since: date,
    until: date,
    short_names: Iterable[str] = HLS_SHORT_NAMES,
) -> list[HlsGranule]:
    """Call ``earthaccess.search_data`` for each short name and parse the results.

    The returned list is in the order ``earthaccess`` returned it, concatenated
    across short names. Empty results — no granules in the window — return an
    empty list without raising.
    """
    granules: list[HlsGranule] = []
    for short_name in short_names:
        results = earthaccess.search_data(
            short_name=short_name,
            bounding_box=bbox,
            temporal=(since.isoformat(), until.isoformat()),
        )
        for result in results:
            granules.append(_parse_granule(result, short_name))
    return granules


def _aoi_bounding_box(aoi: Aoi) -> tuple[float, float, float, float]:
    """Return the AOI's ``(west, south, east, north)`` bbox in WGS 84."""
    geometry = to_shape(aoi.geometry)
    minx, miny, maxx, maxy = geometry.bounds
    return (float(minx), float(miny), float(maxx), float(maxy))


def _parse_granule(granule: Any, short_name: str) -> HlsGranule:
    umm = granule["umm"]
    source_scene_id = str(umm["GranuleUR"])
    begin = umm["TemporalExtent"]["RangeDateTime"]["BeginningDateTime"]
    acquired_at = _parse_iso_datetime(begin)
    cloud_cover_percent = _cloud_cover_from_additional_attributes(umm)
    return HlsGranule(
        sensor=short_name,
        source_scene_id=source_scene_id,
        acquired_at=acquired_at,
        cloud_cover_percent=cloud_cover_percent,
    )


def _parse_iso_datetime(value: str) -> datetime:
    """Parse an ISO 8601 timestamp; tolerate a trailing ``Z`` for UTC."""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _cloud_cover_from_additional_attributes(umm: dict[str, Any]) -> float | None:
    """HLS reports scene cloud cover via UMM ``AdditionalAttributes`` keyed ``CLOUD_COVERAGE``."""
    for attribute in umm.get("AdditionalAttributes", []) or []:
        if attribute.get("Name") != "CLOUD_COVERAGE":
            continue
        values = attribute.get("Values") or []
        if not values:
            return None
        try:
            return float(values[0])
        except (TypeError, ValueError):
            return None
    return None
