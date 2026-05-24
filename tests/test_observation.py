from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from forest_sentinel.models import Aoi, Observation


def _make_aoi(session: Session, name: str = "Test AOI") -> Aoi:
    # A minimal WGS 84 multipolygon is enough for the FK target.
    aoi = Aoi(
        name=name,
        geometry="SRID=4326;MULTIPOLYGON(((0 0, 1 0, 1 1, 0 1, 0 0)))",
    )
    session.add(aoi)
    session.flush()
    return aoi


def test_observation_round_trips(db_session: Session) -> None:
    aoi = _make_aoi(db_session)
    acquired = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    db_session.add(
        Observation(
            aoi_id=aoi.id,
            sensor="HLSL30",
            acquired_at=acquired,
            source_scene_id="HLS.L30.T55LBC.2026002T000000.v2.0",
            cloud_cover_percent=12.5,
        )
    )
    db_session.commit()

    row = db_session.execute(select(Observation)).scalar_one()
    assert row.aoi_id == aoi.id
    assert row.sensor == "HLSL30"
    assert row.cloud_cover_percent == 12.5
    assert row.acquired_at == acquired


def test_observation_cloud_cover_is_optional(db_session: Session) -> None:
    aoi = _make_aoi(db_session)
    db_session.add(
        Observation(
            aoi_id=aoi.id,
            sensor="HLSS30",
            acquired_at=datetime(2026, 1, 2, tzinfo=UTC),
            source_scene_id="HLS.S30.T55LBC.2026002T000000.v2.0",
        )
    )
    db_session.commit()
    assert db_session.execute(select(Observation)).scalar_one().cloud_cover_percent is None


def test_observation_query_by_aoi_and_time_range(db_session: Session) -> None:
    aoi = _make_aoi(db_session)
    for day, scene in ((1, "a"), (5, "b"), (20, "c")):
        db_session.add(
            Observation(
                aoi_id=aoi.id,
                sensor="HLSL30",
                acquired_at=datetime(2026, 1, day, tzinfo=UTC),
                source_scene_id=scene,
            )
        )
    db_session.commit()

    in_window = (
        db_session.execute(
            select(Observation)
            .where(Observation.aoi_id == aoi.id)
            .where(Observation.acquired_at >= datetime(2026, 1, 3, tzinfo=UTC))
            .where(Observation.acquired_at < datetime(2026, 1, 10, tzinfo=UTC))
        )
        .scalars()
        .all()
    )
    assert [obs.source_scene_id for obs in in_window] == ["b"]


def test_duplicate_scene_for_same_aoi_is_rejected(db_session: Session) -> None:
    aoi = _make_aoi(db_session)
    common = {
        "aoi_id": aoi.id,
        "sensor": "HLSL30",
        "acquired_at": datetime(2026, 1, 1, tzinfo=UTC),
        "source_scene_id": "duplicate-scene",
    }
    db_session.add(Observation(**common))
    db_session.flush()
    db_session.add(Observation(**common))
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_same_scene_across_distinct_aois_is_allowed(db_session: Session) -> None:
    first = _make_aoi(db_session, name="AOI one")
    second = _make_aoi(db_session, name="AOI two")
    for aoi in (first, second):
        db_session.add(
            Observation(
                aoi_id=aoi.id,
                sensor="HLSL30",
                acquired_at=datetime(2026, 1, 1, tzinfo=UTC),
                source_scene_id="shared-scene",
            )
        )
    db_session.commit()
    assert len(db_session.execute(select(Observation)).scalars().all()) == 2
