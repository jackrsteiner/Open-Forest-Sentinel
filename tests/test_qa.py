from datetime import UTC, datetime
from typing import Any

import pytest
from geoalchemy2.shape import from_shape
from shapely.geometry import MultiPolygon, Polygon
from sqlalchemy import select
from sqlalchemy.orm import Session

from forest_sentinel import qa
from forest_sentinel.models import Aoi, Observation, QualityMask

# Fmask bit values (HLS v2.0): cloud=bit1, shadow=bit3, snow/ice=bit4, aerosol=bits6-7.
_CLOUD = 1 << 1
_SHADOW = 1 << 3
_SNOW = 1 << 4
_AEROSOL_LOW = 0b01 << 6
_AEROSOL_MODERATE = 0b10 << 6
_AEROSOL_HIGH = 0b11 << 6
_WATER = 1 << 5


@pytest.mark.parametrize(
    ("value", "clear"),
    [
        (0, True),
        (_WATER, True),  # water is not masked
        (_AEROSOL_LOW, True),
        (_AEROSOL_MODERATE, True),
        (_CLOUD, False),
        (_SHADOW, False),
        (_SNOW, False),
        (_AEROSOL_HIGH, False),
        (_CLOUD | _WATER, False),  # any masked flag wins
    ],
)
def test_fmask_clear(value: int, clear: bool) -> None:
    assert qa.fmask_clear(value) is clear


class FakeEarthEngine:
    def __init__(self) -> None:
        self.masked: list[Any] = []
        self.fraction_calls: list[tuple[Any, str, Any, int]] = []

    def apply_fmask_mask(self, image: Any) -> str:
        self.masked.append(image)
        return f"masked({image})"

    def valid_pixel_fraction(self, image: Any, band: str, region: Any, scale: int) -> float:
        self.fraction_calls.append((image, band, region, scale))
        return 0.8


def test_mask_image_delegates_to_seam() -> None:
    fake = FakeEarthEngine()
    assert qa.mask_image("img", ee_module=fake) == "masked(img)"
    assert fake.masked == ["img"]


def test_measure_valid_fraction_delegates() -> None:
    fake = FakeEarthEngine()
    fraction = qa.measure_valid_fraction("img", "NBR", {"type": "Polygon"}, 30, ee_module=fake)
    assert fraction == 0.8
    assert fake.fraction_calls == [("img", "NBR", {"type": "Polygon"}, 30)]


def _make_observation(session: Session) -> Observation:
    square = Polygon([(0, 0), (1, 0), (1, 1), (0, 1), (0, 0)])
    aoi = Aoi(name="Test AOI", geometry=from_shape(MultiPolygon([square]), srid=4326))
    session.add(aoi)
    session.flush()
    obs = Observation(
        aoi_id=aoi.id,
        sensor="HLSL30",
        acquired_at=datetime(2026, 1, 2, tzinfo=UTC),
        source_scene_id="scene-1",
    )
    session.add(obs)
    session.flush()
    return obs


def test_record_quality_mask_persists_default_categories(db_session: Session) -> None:
    obs = _make_observation(db_session)
    mask = qa.record_quality_mask(db_session, observation_id=obs.id, valid_pixel_fraction=0.7)
    db_session.commit()

    stored = db_session.execute(select(QualityMask)).scalar_one()
    assert stored.observation_id == obs.id
    assert stored.valid_pixel_fraction == 0.7
    assert stored.parameters["masked"] == list(qa.MASK_CATEGORIES)
    assert mask.observation_id == obs.id


def test_record_quality_mask_updates_existing(db_session: Session) -> None:
    obs = _make_observation(db_session)
    qa.record_quality_mask(db_session, observation_id=obs.id, valid_pixel_fraction=0.5)
    qa.record_quality_mask(
        db_session,
        observation_id=obs.id,
        valid_pixel_fraction=0.9,
        parameters={"masked": ["cloud"]},
    )
    db_session.commit()

    rows = db_session.execute(select(QualityMask)).scalars().all()
    assert len(rows) == 1
    assert rows[0].valid_pixel_fraction == 0.9
    assert rows[0].parameters == {"masked": ["cloud"]}
