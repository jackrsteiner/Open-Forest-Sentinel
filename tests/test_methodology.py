import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from forest_sentinel.methodology import (
    MethodologyVersionMismatch,
    get_or_create_methodology_version,
)
from forest_sentinel.models import MethodologyVersion

_PARAMS = {
    "ee_script_version": "slice1-v1",
    "collections": ["NASA/HLS/HLSL30/v002", "NASA/HLS/HLSS30/v002"],
    "delta_nbr_threshold": -0.25,
}


def test_creates_when_absent(db_session: Session) -> None:
    created = get_or_create_methodology_version(
        db_session, name="optical-change", version="1.0.0", parameters=_PARAMS
    )
    db_session.commit()
    assert created.id is not None
    assert db_session.execute(select(MethodologyVersion)).scalars().all() == [created]


def test_returns_existing_for_identical_inputs(db_session: Session) -> None:
    first = get_or_create_methodology_version(
        db_session, name="optical-change", version="1.0.0", parameters=_PARAMS
    )
    second = get_or_create_methodology_version(
        db_session, name="optical-change", version="1.0.0", parameters=_PARAMS
    )
    assert first.id == second.id
    assert len(db_session.execute(select(MethodologyVersion)).scalars().all()) == 1


def test_identical_inputs_are_key_order_insensitive(db_session: Session) -> None:
    first = get_or_create_methodology_version(
        db_session, name="m", version="1", parameters={"a": 1, "b": 2}
    )
    second = get_or_create_methodology_version(
        db_session, name="m", version="1", parameters={"b": 2, "a": 1}
    )
    assert first.id == second.id


def test_distinct_versions_create_distinct_rows(db_session: Session) -> None:
    a = get_or_create_methodology_version(db_session, name="m", version="1", parameters={})
    b = get_or_create_methodology_version(db_session, name="m", version="2", parameters={})
    assert a.id != b.id


def test_parameter_mismatch_raises(db_session: Session) -> None:
    get_or_create_methodology_version(
        db_session, name="m", version="1", parameters={"threshold": -0.25}
    )
    with pytest.raises(MethodologyVersionMismatch, match="different"):
        get_or_create_methodology_version(
            db_session, name="m", version="1", parameters={"threshold": -0.30}
        )


def test_parameters_carry_ee_provenance(db_session: Session) -> None:
    row = get_or_create_methodology_version(
        db_session, name="optical-change", version="1.0.0", parameters=_PARAMS
    )
    db_session.commit()
    assert row.parameters["ee_script_version"] == "slice1-v1"
    assert "NASA/HLS/HLSL30/v002" in row.parameters["collections"]


def test_db_level_unique_constraint(db_session: Session) -> None:
    db_session.add(MethodologyVersion(name="m", version="1", parameters={}))
    db_session.flush()
    db_session.add(MethodologyVersion(name="m", version="1", parameters={}))
    with pytest.raises(IntegrityError):
        db_session.flush()
