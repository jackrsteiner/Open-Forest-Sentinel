"""Methodology version provenance.

Every derived artifact (``index_raster``, ``change_raster``,
``disturbance_candidate``) references the methodology version that produced
it. This module provides a get-or-create helper that returns the canonical
row for a given ``(name, version, parameters)`` tuple, creating it on first
use.

Methodology versions are stable provenance records: a ``(name, version)``
identity is bound to its parameters. Asking for the same identity with
different parameters raises ``MethodologyVersionMismatch`` rather than
silently creating a new row.
"""

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from forest_sentinel.models import MethodologyVersion


class MethodologyVersionMismatch(ValueError):
    """Raised when a ``(name, version)`` exists with different parameters."""


def get_or_create_methodology_version(
    session: Session,
    *,
    name: str,
    version: str,
    parameters: dict[str, Any],
) -> MethodologyVersion:
    """Return the canonical ``MethodologyVersion`` row for the given identity.

    Looks up by ``(name, version)``. If a row exists, its ``parameters`` must
    match the supplied ``parameters`` exactly; otherwise
    ``MethodologyVersionMismatch`` is raised. If no row exists, one is
    inserted and returned (after a flush).
    """
    existing = session.scalars(
        select(MethodologyVersion).where(
            MethodologyVersion.name == name,
            MethodologyVersion.version == version,
        )
    ).one_or_none()
    if existing is not None:
        if existing.parameters != parameters:
            raise MethodologyVersionMismatch(
                f"methodology version {name!r} v{version!r} already exists "
                f"with different parameters"
            )
        return existing

    row = MethodologyVersion(name=name, version=version, parameters=parameters)
    session.add(row)
    session.flush()
    return row
