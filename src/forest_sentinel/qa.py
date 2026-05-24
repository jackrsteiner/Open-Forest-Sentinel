"""QA masking with the HLS ``Fmask`` band.

Both HLS collections ship an ``Fmask`` QA band, so masking is a cheap per-image
operation in Earth Engine (``docs/architecture.md`` §4a). Applying it inside Slice 1 —
before NBR/NDVI (#39) and before the trailing-median baseline (#40) — gives honest
quality metadata on the first detections and keeps cloud-driven false candidates out of
the candidate table.

The bit-decoding rule lives in :func:`fmask_clear` (pure, exhaustively tested); the
Earth Engine band-expression form lives in :func:`forest_sentinel.earthengine.apply_fmask_mask`
and is kept in lock-step with it.
"""

from typing import Any

from sqlalchemy.orm import Session

from forest_sentinel import earthengine
from forest_sentinel.models import QualityMask

# What "masked" means, recorded into the methodology parameters and the quality_mask row.
MASK_CATEGORIES = ("cloud", "cloud_shadow", "snow_ice", "high_aerosol")


def fmask_clear(value: int) -> bool:
    """True if an ``Fmask`` pixel value is clear (kept), False if it should be masked.

    Masks cloud, cloud shadow, snow/ice, and high-aerosol pixels.
    """
    if value & (1 << earthengine.FMASK_BIT_CLOUD):
        return False
    if value & (1 << earthengine.FMASK_BIT_CLOUD_SHADOW):
        return False
    if value & (1 << earthengine.FMASK_BIT_SNOW_ICE):
        return False
    aerosol = (value >> earthengine.FMASK_AEROSOL_SHIFT) & 0b11
    return aerosol != earthengine.FMASK_AEROSOL_HIGH


def mask_image(image: Any, *, ee_module: Any = earthengine) -> Any:
    """Apply the Fmask clear-pixel mask to an HLS image via the EE seam."""
    return ee_module.apply_fmask_mask(image)


def measure_valid_fraction(
    image: Any, band: str, region: Any, scale: int, *, ee_module: Any = earthengine
) -> float:
    """Fraction of valid (unmasked) pixels of ``band`` within ``region``."""
    return float(ee_module.valid_pixel_fraction(image, band, region, scale))


def record_quality_mask(
    session: Session,
    *,
    observation_id: int,
    valid_pixel_fraction: float,
    parameters: dict[str, Any] | None = None,
) -> QualityMask:
    """Persist (or update) the ``quality_mask`` coverage record for an observation."""
    mask = session.get(QualityMask, observation_id)
    payload = parameters if parameters is not None else {"masked": list(MASK_CATEGORIES)}
    if mask is None:
        mask = QualityMask(
            observation_id=observation_id,
            valid_pixel_fraction=valid_pixel_fraction,
            parameters=payload,
        )
        session.add(mask)
    else:
        mask.valid_pixel_fraction = valid_pixel_fraction
        mask.parameters = payload
    session.flush()
    return mask
