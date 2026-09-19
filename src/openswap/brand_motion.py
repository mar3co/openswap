"""OpenSoft brand-mark motion shared by native surfaces.

The source treatment lives in ``opensoft-site``: a closed ring opens into the
two offset halves of the OpenSoft symbol using the brand spring curve.  Keeping
the easing math here makes the native AppKit rendering deterministic and
testable without importing AppKit.
"""

from __future__ import annotations


BRAND_MOTION_DURATION = 2.5
BRAND_MARK_OFFSET = 3.0
_SPRING = (0.34, 1.45, 0.64, 1.0)


def _bezier(t: float, first: float, second: float) -> float:
    inverse = 1.0 - t
    return 3.0 * inverse * inverse * t * first + 3.0 * inverse * t * t * second + t**3


def brand_motion_progress(fraction: float) -> float:
    """Return the CSS ``cubic-bezier(.34,1.45,.64,1)`` value at ``fraction``."""
    x = max(0.0, min(float(fraction), 1.0))
    if x in (0.0, 1.0):
        return x

    x1, y1, x2, y2 = _SPRING
    low, high = 0.0, 1.0
    for _ in range(18):
        parameter = (low + high) / 2.0
        if _bezier(parameter, x1, x2) < x:
            low = parameter
        else:
            high = parameter
    return _bezier((low + high) / 2.0, y1, y2)


def brand_mark_centers(progress: float) -> tuple[float, float]:
    """Y centers for the left and right half-rings in the 32-unit view box."""
    offset = BRAND_MARK_OFFSET * float(progress)
    return 16.0 - offset, 16.0 + offset
