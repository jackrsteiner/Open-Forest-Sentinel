from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import earthaccess
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from forest_sentinel.aoi import load_aoi_config, persist_aoi
from forest_sentinel.hls import (
    HLS_SHORT_NAMES,
    HlsGranule,
    _aoi_bounding_box,
    discover_observations,
    search_hls_granules,
)
from forest_sentinel.models import Aoi, Observation

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def _granule_payload(
    granule_ur: str,
    beginning_datetime: str,
    cloud_cover_percent: float | None = None,
) -> dict[str, Any]:
    additional: list[dict[str, Any]] = []
    if cloud_cover_percent is not None:
        additional.append({"Name": "CLOUD_COVERAGE", "Values": [str(cloud_cover_percent)]})
    return {
        "umm": {
            "GranuleUR": granule_ur,
            "TemporalExtent": {"RangeDateTime": {"BeginningDateTime": beginning_datetime}},
            "AdditionalAttributes": additional,
        }
    }


def _stub_search_data(
    monkeypatch: pytest.MonkeyPatch,
    granules_by_short_name: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Replace ``earthaccess.search_data`` with a stub; record each call."""
    calls: list[dict[str, Any]] = []

    def fake(**kwargs: Any) -> list[dict[str, Any]]:
        calls.append(kwargs)
        return granules_by_short_name.get(kwargs["short_name"], [])

    monkeypatch.setattr(earthaccess, "search_data", fake)
    return calls


def _seed_aoi(session: Session) -> Aoi:
    config = load_aoi_config(EXAMPLES / "aoi-sample.geojson")
    aoi = persist_aoi(session, config)
    session.flush()
    return aoi


# ---------------------------------------------------------------------------
# Granule parsing


def test_search_parses_granule_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_search_data(
        monkeypatch,
        {
            "HLSL30": [
                _granule_payload(
                    "HLS.L30.T55LCC.2026100T140000.v2.0",
                    "2026-04-10T14:00:00.000Z",
                    cloud_cover_percent=12.5,
                )
            ],
            "HLSS30": [],
        },
    )

    granules = search_hls_granules(
        (0.0, 0.0, 1.0, 1.0), since=date(2026, 4, 1), until=date(2026, 4, 30)
    )

    assert granules == [
        HlsGranule(
            sensor="HLSL30",
            source_scene_id="HLS.L30.T55LCC.2026100T140000.v2.0",
            acquired_at=datetime(2026, 4, 10, 14, 0, tzinfo=UTC),
            cloud_cover_percent=12.5,
        )
    ]


def test_search_handles_missing_cloud_cover(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_search_data(
        monkeypatch,
        {
            "HLSL30": [
                _granule_payload(
                    "HLS.L30.T18LWQ.2026001T120000.v2.0",
                    "2026-01-01T12:00:00.000Z",
                )
            ],
            "HLSS30": [],
        },
    )

    [granule] = search_hls_granules(
        (0.0, 0.0, 1.0, 1.0), since=date(2026, 1, 1), until=date(2026, 1, 31)
    )
    assert granule.cloud_cover_percent is None


def test_search_concatenates_results_across_short_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _stub_search_data(
        monkeypatch,
        {
            "HLSL30": [
                _granule_payload("scene-l", "2026-01-05T12:00:00Z", 5.0),
            ],
            "HLSS30": [
                _granule_payload("scene-s1", "2026-01-06T00:00:00Z", 10.0),
                _granule_payload("scene-s2", "2026-01-07T00:00:00Z", 20.0),
            ],
        },
    )

    granules = search_hls_granules(
        (0.0, 0.0, 1.0, 1.0), since=date(2026, 1, 1), until=date(2026, 1, 31)
    )

    assert [g.source_scene_id for g in granules] == ["scene-l", "scene-s1", "scene-s2"]
    assert [g.sensor for g in granules] == ["HLSL30", "HLSS30", "HLSS30"]
    assert [c["short_name"] for c in calls] == list(HLS_SHORT_NAMES)


def test_search_returns_empty_list_when_no_results(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_search_data(monkeypatch, {"HLSL30": [], "HLSS30": []})
    granules = search_hls_granules(
        (0.0, 0.0, 1.0, 1.0), since=date(2026, 1, 1), until=date(2026, 1, 31)
    )
    assert granules == []


# ---------------------------------------------------------------------------
# AOI bounding box


def test_aoi_bounding_box_uses_geometry_bounds(db_session: Session) -> None:
    aoi = _seed_aoi(db_session)
    bbox = _aoi_bounding_box(aoi)
    assert all(isinstance(coord, float) for coord in bbox)
    assert bbox[0] < bbox[2]
    assert bbox[1] < bbox[3]


# ---------------------------------------------------------------------------
# discover_observations


def test_discover_records_new_observations(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    aoi = _seed_aoi(db_session)
    _stub_search_data(
        monkeypatch,
        {
            "HLSL30": [
                _granule_payload("scene-l-1", "2026-01-05T12:00:00Z", 5.0),
                _granule_payload("scene-l-2", "2026-01-12T12:00:00Z", 10.0),
            ],
            "HLSS30": [
                _granule_payload("scene-s-1", "2026-01-06T00:00:00Z", 1.0),
            ],
        },
    )

    result = discover_observations(db_session, aoi, since=date(2026, 1, 1), until=date(2026, 1, 31))
    db_session.commit()

    assert result.discovered == 3
    assert result.recorded == 3
    assert result.skipped == 0

    observations = db_session.scalars(select(Observation).order_by(Observation.acquired_at)).all()
    assert [o.source_scene_id for o in observations] == ["scene-l-1", "scene-s-1", "scene-l-2"]
    assert {o.sensor for o in observations} == {"HLSL30", "HLSS30"}


def test_discover_is_idempotent_per_aoi(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    aoi = _seed_aoi(db_session)
    _stub_search_data(
        monkeypatch,
        {
            "HLSL30": [_granule_payload("scene-l-1", "2026-01-05T12:00:00Z", 5.0)],
            "HLSS30": [],
        },
    )

    first = discover_observations(db_session, aoi, since=date(2026, 1, 1), until=date(2026, 1, 31))
    db_session.commit()
    second = discover_observations(db_session, aoi, since=date(2026, 1, 1), until=date(2026, 1, 31))
    db_session.commit()

    assert first.recorded == 1
    assert second.recorded == 0
    assert second.skipped == 1
    assert len(db_session.scalars(select(Observation)).all()) == 1


def test_discover_handles_empty_window(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    aoi = _seed_aoi(db_session)
    _stub_search_data(monkeypatch, {"HLSL30": [], "HLSS30": []})

    result = discover_observations(db_session, aoi, since=date(2026, 1, 1), until=date(2026, 1, 31))
    db_session.commit()

    assert result == type(result)(discovered=0, recorded=0, skipped=0)
    assert db_session.scalars(select(Observation)).all() == []
