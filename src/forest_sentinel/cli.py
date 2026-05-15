"""Command-line entrypoint for Open Forest Sentinel.

``forest-sentinel run`` executes the Slice 1 pipeline over a configured AOI
and a time window:

1. Load and persist the AOI (idempotent — re-runs reuse the existing row).
2. Resolve the methodology version (provenance for every derived artifact).
3. Discover HLS observations via ``earthaccess`` and record new ones.
4. If band rasters are available locally (``--band-root``), compute index
   rasters (NBR / NDVI), change rasters (ΔNBR / ΔNDVI vs. trailing-median
   baseline), and disturbance-candidate polygons for every observation.
5. Print a per-stage summary.

Without ``--band-root`` the run stops after discovery — useful when bands
have not been staged yet but observations should still be inventoried.
"""

import argparse
import sys
from datetime import date
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from forest_sentinel.aoi import AoiConfig, AoiConfigError, load_aoi_config, persist_aoi
from forest_sentinel.candidates import extract_candidates_for_change_raster
from forest_sentinel.change import compute_change_products_for_observation
from forest_sentinel.db import get_engine
from forest_sentinel.hls import _aoi_bounding_box, discover_observations
from forest_sentinel.indices import LocalBandResolver, compute_indices_for_observation
from forest_sentinel.methodology import get_or_create_methodology_version
from forest_sentinel.models import Aoi, MethodologyVersion, Observation
from forest_sentinel.storage import LocalStorage

DEFAULT_METHODOLOGY_NAME = "optical-change"
DEFAULT_METHODOLOGY_VERSION = "0.1"


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and dispatch. Returns a process exit code."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    return _run(args)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="forest-sentinel",
        description="Forest disturbance monitoring for a configurable Area of Interest.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    run_parser = subparsers.add_parser("run", help="Run the Slice 1 pipeline.")
    run_parser.add_argument(
        "--aoi",
        required=True,
        type=Path,
        metavar="PATH",
        help="Path to the AOI GeoJSON configuration file.",
    )
    run_parser.add_argument(
        "--since",
        required=True,
        type=date.fromisoformat,
        metavar="YYYY-MM-DD",
        help="Inclusive start of the HLS time window.",
    )
    run_parser.add_argument(
        "--until",
        required=True,
        type=date.fromisoformat,
        metavar="YYYY-MM-DD",
        help="Inclusive end of the HLS time window.",
    )
    run_parser.add_argument(
        "--band-root",
        type=Path,
        default=None,
        metavar="PATH",
        help=(
            "Root directory containing staged HLS bands "
            "({band_root}/{source_scene_id}/{ASSET}.tif). "
            "If omitted, the run stops after discovery."
        ),
    )
    run_parser.add_argument(
        "--methodology-name",
        default=DEFAULT_METHODOLOGY_NAME,
        help="Methodology name to record on derived artifacts.",
    )
    run_parser.add_argument(
        "--methodology-version",
        default=DEFAULT_METHODOLOGY_VERSION,
        help="Methodology version to record on derived artifacts.",
    )
    return parser


def _run(args: argparse.Namespace) -> int:
    try:
        config = load_aoi_config(args.aoi)
    except AoiConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    engine = get_engine()
    try:
        with Session(engine) as session:
            aoi = _get_or_persist_aoi(session, config)
            methodology = get_or_create_methodology_version(
                session,
                name=args.methodology_name,
                version=args.methodology_version,
                parameters={},
            )
            session.flush()

            discovery = discover_observations(session, aoi, since=args.since, until=args.until)

            index_count = 0
            change_count = 0
            candidate_count = 0
            if args.band_root is not None:
                index_count, change_count, candidate_count = _run_raster_stages(
                    session,
                    aoi=aoi,
                    methodology=methodology,
                    band_root=args.band_root,
                )

            aoi_id = aoi.id
            total_aois = session.execute(select(func.count()).select_from(Aoi)).scalar_one()
            session.commit()
    except OperationalError as exc:
        print(f"error: could not connect to the database ({exc})", file=sys.stderr)
        return 1
    finally:
        engine.dispose()

    minx, miny, maxx, maxy = config.geometry.bounds
    print(f"Loaded AOI {config.name!r} from {args.aoi}")
    print(f"Persisted as aoi id={aoi_id}")
    print(f"Bounding box (minx, miny, maxx, maxy): ({minx}, {miny}, {maxx}, {maxy})")
    print(f"Total AOIs in database: {total_aois}")
    print(f"Time window: {args.since.isoformat()} → {args.until.isoformat()}")
    print(f"  observations discovered: {discovery.discovered}")
    print(f"  observations recorded:   {discovery.recorded}")
    print(f"  observations skipped:    {discovery.skipped}")
    if args.band_root is None:
        print("  (band-root not provided; index/change/candidate stages skipped)")
    else:
        print(f"  index rasters:           {index_count}")
        print(f"  change rasters:          {change_count}")
        print(f"  disturbance candidates:  {candidate_count}")
    return 0


def _get_or_persist_aoi(session: Session, config: AoiConfig) -> Aoi:
    """Return the AOI row for ``config.name``, persisting it on first use."""
    existing = session.scalars(select(Aoi).where(Aoi.name == config.name)).one_or_none()
    if existing is not None:
        return existing
    return persist_aoi(session, config)


def _run_raster_stages(
    session: Session,
    *,
    aoi: Aoi,
    methodology: MethodologyVersion,
    band_root: Path,
) -> tuple[int, int, int]:
    """Run indices → change → candidates for every observation of ``aoi``.

    Returns ``(index_raster_count, change_raster_count, candidate_count)`` —
    the rows produced by *this* run across all observations of the AOI under
    the supplied methodology.
    """
    resolver = LocalBandResolver(root=str(band_root))
    storage = LocalStorage()
    aoi_bbox = _aoi_bounding_box(aoi)

    observations = list(
        session.scalars(
            select(Observation)
            .where(Observation.aoi_id == aoi.id)
            .order_by(Observation.acquired_at)
        )
    )

    index_count = 0
    change_count = 0
    candidate_count = 0
    for observation in observations:
        index_rasters = compute_indices_for_observation(
            session,
            observation,
            methodology=methodology,
            storage=storage,
            resolver=resolver,
            aoi_bbox_wgs84=aoi_bbox,
            aoi_name=aoi.name,
        )
        index_count += len(index_rasters)

        change_rasters = compute_change_products_for_observation(
            session,
            observation,
            methodology=methodology,
            storage=storage,
            aoi_name=aoi.name,
        )
        change_count += len(change_rasters)

        for change_raster in change_rasters:
            # Candidate extraction is NBR-driven (bead #41); ΔNDVI is kept as
            # supporting evidence but does not directly emit candidates.
            if change_raster.change_type != "delta_nbr":
                continue
            candidates = extract_candidates_for_change_raster(
                session,
                change_raster,
                methodology=methodology,
            )
            candidate_count += len(candidates)

    return index_count, change_count, candidate_count
