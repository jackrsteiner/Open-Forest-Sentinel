"""The transparent confidence rule (E15, #106): explained, append-on-change."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from forest_sentinel.confidence import (
    HIGH_CUTOFF,
    MEDIUM_CUTOFF,
    RULE_VERSION,
    assess_events_for_aoi,
    compute_assessment,
    normalize_currency,
    normalize_magnitude,
    normalize_persistence,
    score_to_level,
)
from forest_sentinel.events import track_events_for_aoi
from forest_sentinel.models import ConfidenceAssessment
from tests.fakes import make_aoi, make_candidate, make_methodology

_PATCH = [(0.1, 0.1), (0.2, 0.1), (0.2, 0.2), (0.1, 0.2), (0.1, 0.1)]
_PATCH_GROWN = [(0.15, 0.1), (0.3, 0.1), (0.3, 0.2), (0.15, 0.2), (0.15, 0.1)]


@pytest.mark.parametrize(
    ("delta_min", "expected"),
    [(None, None), (-0.05, 0.0), (-0.1, 0.0), (-0.3, 0.5), (-0.5, 1.0), (-0.9, 1.0)],
)
def test_magnitude_normalization(delta_min: float | None, expected: float | None) -> None:
    result = normalize_magnitude(delta_min)
    assert result == (pytest.approx(expected) if expected is not None else None)


@pytest.mark.parametrize(("count", "expected"), [(1, 0.0), (3, 0.5), (5, 1.0), (9, 1.0)])
def test_persistence_normalization(count: int, expected: float) -> None:
    assert normalize_persistence(count) == pytest.approx(expected)


@pytest.mark.parametrize(("days", "expected"), [(0, 1.0), (90, 0.5), (180, 0.0), (400, 0.0)])
def test_currency_normalization(days: float, expected: float) -> None:
    assert normalize_currency(days) == pytest.approx(expected)


def test_level_cutoffs_are_pinned() -> None:
    assert score_to_level(HIGH_CUTOFF) == "high"
    assert score_to_level(HIGH_CUTOFF - 0.01) == "medium"
    assert score_to_level(MEDIUM_CUTOFF) == "medium"
    assert score_to_level(MEDIUM_CUTOFF - 0.01) == "low"


def test_compute_assessment_records_every_input() -> None:
    assessment = compute_assessment(
        delta_min=-0.5,
        delta_mean=-0.3,
        mean_valid_fraction=0.8,
        observation_count=5,
        days_since_last=0,
    )
    # All factors maxed except coverage (0.8): 0.35 + 0.30 + 0.2*0.8 + 0.15 = 0.96.
    assert assessment.score == pytest.approx(0.96)
    assert assessment.level == "high"
    inputs = assessment.inputs
    assert inputs["rule_version"] == RULE_VERSION
    assert inputs["factors"]["magnitude"]["delta_min"] == -0.5
    assert inputs["factors"]["persistence"]["observation_count"] == 5
    assert inputs["subscores"]["coverage"] == pytest.approx(0.8)
    assert inputs["missing"] == []


def test_missing_statistics_degrade_with_renormalized_weights() -> None:
    # Pre-#95 candidates: no magnitude, no coverage. Weights renormalize over
    # persistence (0.30) + currency (0.15).
    assessment = compute_assessment(
        delta_min=None,
        delta_mean=None,
        mean_valid_fraction=None,
        observation_count=5,
        days_since_last=0,
    )
    assert assessment.score == pytest.approx(1.0)
    assert sorted(assessment.inputs["missing"]) == ["coverage", "magnitude"]
    assert assessment.inputs["subscores"]["magnitude"] is None


def test_assess_events_appends_explained_rows(db_session: Session) -> None:
    aoi = make_aoi(db_session)
    methodology = make_methodology(db_session)
    make_candidate(
        db_session,
        aoi,
        methodology,
        day=1,
        ring=_PATCH,
        area_m2=10_000.0,
        delta_min=-0.5,
        delta_mean=-0.3,
        valid_pixel_fraction=0.9,
    )
    make_candidate(
        db_session,
        aoi,
        methodology,
        day=8,
        ring=_PATCH_GROWN,
        area_m2=15_000.0,
        delta_min=-0.4,
        delta_mean=-0.25,
        valid_pixel_fraction=0.7,
    )
    track_events_for_aoi(db_session, aoi=aoi)

    appended = assess_events_for_aoi(db_session, aoi=aoi, now=datetime(2026, 1, 10, tzinfo=UTC))

    assert appended == 1
    row = db_session.execute(select(ConfidenceAssessment)).scalar_one()
    assert row.rule_version == RULE_VERSION
    # Deepest drop across the event's candidates, averaged coverage.
    assert row.inputs["factors"]["magnitude"]["delta_min"] == -0.5
    assert row.inputs["factors"]["coverage"]["mean_valid_fraction"] == pytest.approx(0.8)
    assert row.inputs["factors"]["persistence"]["observation_count"] == 2
    assert row.level in ("low", "medium", "high")
    assert 0.0 <= row.score <= 1.0


def test_unchanged_conclusions_are_not_reappended(db_session: Session) -> None:
    aoi = make_aoi(db_session)
    methodology = make_methodology(db_session)
    make_candidate(
        db_session, aoi, methodology, day=1, ring=_PATCH, area_m2=10_000.0, delta_min=-0.5
    )
    track_events_for_aoi(db_session, aoi=aoi)
    now = datetime(2026, 1, 10, tzinfo=UTC)

    assert assess_events_for_aoi(db_session, aoi=aoi, now=now) == 1
    # Same moment, same evidence: the conclusion did not move — no new row.
    assert assess_events_for_aoi(db_session, aoi=aoi, now=now) == 0

    # Months later the currency factor has decayed: the score moved, history grows.
    assert assess_events_for_aoi(db_session, aoi=aoi, now=datetime(2026, 6, 1, tzinfo=UTC)) == 1
    rows = db_session.execute(select(ConfidenceAssessment)).scalars().all()
    assert len(rows) == 2
