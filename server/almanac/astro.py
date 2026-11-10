"""Moon phase, computed locally (no API needed)."""

import math
from datetime import datetime, timezone

SYNODIC_DAYS = 29.530588853
# A known new moon: 2000-01-06 18:14 UTC.
_NEW_MOON = datetime(2000, 1, 6, 18, 14, tzinfo=timezone.utc)

_NAMES = [
    (0.0339, "New moon"),
    (0.2161, "Waxing crescent"),
    (0.2839, "First quarter"),
    (0.4661, "Waxing gibbous"),
    (0.5339, "Full moon"),
    (0.7161, "Waning gibbous"),
    (0.7839, "Last quarter"),
    (0.9661, "Waning crescent"),
    (1.0001, "New moon"),
]


def moon_phase(when):
    """Fraction of the synodic month elapsed: 0 new, 0.25 first quarter, 0.5 full."""
    days = (when.astimezone(timezone.utc) - _NEW_MOON).total_seconds() / 86400
    return (days / SYNODIC_DAYS) % 1.0


def illumination(phase):
    return (1 - math.cos(2 * math.pi * phase)) / 2


def phase_name(phase):
    for limit, name in _NAMES:
        if phase < limit:
            return name
    return "New moon"


def lit_span(phase, half_width, southern):
    """Lit x-range [lo, hi] of one row of the moon's disc, centred on 0.

    half_width is the disc's half-width at this row. In the northern hemisphere a
    waxing moon is lit on the right; south of the equator it is the mirror image.
    Returns None when nothing on the row is lit.
    """
    c = math.cos(2 * math.pi * phase) * half_width
    if phase < 0.5:
        lo, hi = c, half_width
    else:
        lo, hi = -half_width, -c
    if southern:
        lo, hi = -hi, -lo
    if hi - lo <= 0:
        return None
    return lo, hi
