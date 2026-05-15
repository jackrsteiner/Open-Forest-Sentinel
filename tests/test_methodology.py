import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from forest_sentinel.methodology import (
    MethodologyVersionMismatch,
    get_or_create_methodology_version,
)
from forest_sentinel.models import MethodologyVersion


def test_creates_a_row_when_none_exists(db_session: Session) -> None:
    row = get_or_create_methodology_version(
        db_session,
        name="optical-change",
        version="0.1",
        parameters={"delta_nbr_threshold": -0.25, "min_area_ha": 0.5},
    )
    db_session.commit()

    assert row.id is not None
    assert row.name == "optical-change"
    assert row.version == "0.1"
    assert row.parameters == {"delta_nbr_threshold": -0.25, "min_area_ha": 0.5}
    assert row.created_at is not None


def test_returns_existing_row_for_identical_inputs(db_session: Session) -> None:
    params = {"delta_nbr_threshold": -0.25, "min_area_ha": 0.5}
    first = get_or_create_methodology_version(
        db_session, name="optical-change", version="0.1", parameters=params
    )
    db_session.commit()

    second = get_or_create_methodology_version(
        db_session, name="optical-change", version="0.1", parameters=params
    )
    assert second.id == first.id


def test_empty_parameters_are_supported(db_session: Session) -> None:
    row = get_or_create_methodology_version(
        db_session, name="optical-change", version="0.1", parameters={}
    )
    db_session.commit()
    assert row.parameters == {}


def test_different_identity_creates_a_new_row(db_session: Session) -> None:
    first = get_or_create_methodology_version(
        db_session, name="optical-change", version="0.1", parameters={"a": 1}
    )
    second = get_or_create_methodology_version(
        db_session, name="optical-change", version="0.2", parameters={"a": 1}
    )
    third = get_or_create_methodology_version(
        db_session, name="radar-change", version="0.1", parameters={"a": 1}
    )
    db_session.commit()

    assert len({first.id, second.id, third.id}) == 3


def test_mismatched_parameters_raise(db_session: Session) -> None:
    get_or_create_methodology_version(
        db_session,
        name="optical-change",
        version="0.1",
        parameters={"delta_nbr_threshold": -0.25},
    )
    db_session.commit()

    with pytest.raises(MethodologyVersionMismatch, match="optical-change"):
        get_or_create_methodology_version(
            db_session,
            name="optical-change",
            version="0.1",
            parameters={"delta_nbr_threshold": -0.30},
        )


def test_unique_constraint_enforced_at_db_level(db_session: Session) -> None:
    db_session.add(MethodologyVersion(name="optical-change", version="0.1", parameters={"a": 1}))
    db_session.flush()
    db_session.add(MethodologyVersion(name="optical-change", version="0.1", parameters={"a": 2}))
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_parameter_key_ordering_does_not_matter(db_session: Session) -> None:
    first = get_or_create_methodology_version(
        db_session,
        name="optical-change",
        version="0.1",
        parameters={"a": 1, "b": 2},
    )
    db_session.commit()

    second = get_or_create_methodology_version(
        db_session,
        name="optical-change",
        version="0.1",
        parameters={"b": 2, "a": 1},
    )
    assert second.id == first.id

    rows = db_session.scalars(select(MethodologyVersion)).all()
    assert len(rows) == 1
