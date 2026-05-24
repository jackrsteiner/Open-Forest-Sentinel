"""Methodology-version provenance.

Every derived Slice 1 artifact references the ``methodology_version`` that produced
it. Because Slice 1 compute runs server-side in Earth Engine, the stored
``parameters`` must also pin the EE script version and input collection/asset IDs so
a run is reproducible. ``get_or_create_methodology_version`` is the single entry point
the pipeline uses to obtain that reference.
"""

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from forest_sentinel.models import MethodologyVersion


class MethodologyVersionMismatch(ValueError):
    """Raised when a ``(name, version)`` exists with different ``parameters``.

    Methodology versions are stable provenance records; the same identity must not
    silently map to divergent parameters. Bump the ``version`` instead.
    """


def get_or_create_methodology_version(
    session: Session,
    *,
    name: str,
    version: str,
    parameters: dict[str, Any],
) -> MethodologyVersion:
    """Return the row for ``(name, version)``, creating it if absent.

    Raises ``MethodologyVersionMismatch`` if a row with the same ``(name, version)``
    already stores different ``parameters``. Dict comparison is order-insensitive, so
    re-running with the same parameters in a different key order is treated as
    identical.
    """
    existing = session.execute(
        select(MethodologyVersion)
        .where(MethodologyVersion.name == name)
        .where(MethodologyVersion.version == version)
    ).scalar_one_or_none()

    if existing is not None:
        if existing.parameters != parameters:
            raise MethodologyVersionMismatch(
                f"methodology {name!r} version {version!r} already exists with different "
                "parameters; bump the version instead of mutating it"
            )
        return existing

    created = MethodologyVersion(name=name, version=version, parameters=parameters)
    session.add(created)
    session.flush()
    return created
