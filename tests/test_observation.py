from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from forest_sentinel.aoi import load_aoi_config, persist_aoi
from forest_sentinel.models import Observation

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def _seed_aoi(session: Session, name: str = "Example AOI") -> int:
    config = load_aoi_config(EXAMPLES / "aoi-sample.geojson")
    if name != config.name:
        config = type(config)(name=name, geometry=config.geometry)
    aoi = persist_aoi(session, config)
    session.flush()
    return aoi.id


def test_persist_observation_round_trip(db_session: Session) -> None:
    aoi_id = _seed_aoi(db_session)
    acquired = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

    observation = Observation(
        aoi_id=aoi_id,
        sensor="HLSL30",
        acquired_at=acquired,
        source_scene_id="HLS.L30.T18LWQ.2026001T120000.v2.0",
        cloud_cover_percent=12.3,
    )
    db_session.add(observation)
    db_session.commit()

    fetched = db_session.scalars(select(Observation).where(Observation.id == observation.id)).one()
    assert fetched.aoi_id == aoi_id
    assert fetched.sensor == "HLSL30"
    assert fetched.acquired_at == acquired
    assert fetched.source_scene_id == "HLS.L30.T18LWQ.2026001T120000.v2.0"
    assert fetched.cloud_cover_percent == pytest.approx(12.3)
    assert fetched.created_at is not None


def test_cloud_cover_percent_is_optional(db_session: Session) -> None:
    aoi_id = _seed_aoi(db_session)
    db_session.add(
        Observation(
            aoi_id=aoi_id,
            sensor="HLSS30",
            acquired_at=datetime(2026, 2, 1, tzinfo=UTC),
            source_scene_id="HLS.S30.T55MBN.2026032T000000.v2.0",
        )
    )
    db_session.commit()


def test_query_by_aoi_and_time_range(db_session: Session) -> None:
    aoi_id = _seed_aoi(db_session)
    for day in (1, 5, 10, 20):
        db_session.add(
            Observation(
                aoi_id=aoi_id,
                sensor="HLSL30",
                acquired_at=datetime(2026, 1, day, tzinfo=UTC),
                source_scene_id=f"scene-{day:02d}",
            )
        )
    db_session.commit()

    in_range = db_session.scalars(
        select(Observation)
        .where(Observation.aoi_id == aoi_id)
        .where(Observation.acquired_at >= datetime(2026, 1, 5, tzinfo=UTC))
        .where(Observation.acquired_at <= datetime(2026, 1, 15, tzinfo=UTC))
    ).all()
    assert {obs.source_scene_id for obs in in_range} == {"scene-05", "scene-10"}


def test_duplicate_source_scene_for_same_aoi_is_rejected(db_session: Session) -> None:
    aoi_id = _seed_aoi(db_session)
    db_session.add(
        Observation(
            aoi_id=aoi_id,
            sensor="HLSL30",
            acquired_at=datetime(2026, 1, 1, tzinfo=UTC),
            source_scene_id="duplicate-scene",
        )
    )
    db_session.commit()

    db_session.add(
        Observation(
            aoi_id=aoi_id,
            sensor="HLSL30",
            acquired_at=datetime(2026, 1, 2, tzinfo=UTC),
            source_scene_id="duplicate-scene",
        )
    )
    with pytest.raises(IntegrityError):
        db_session.flush()
