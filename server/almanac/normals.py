"""What the weather usually does here, so a fact can say "above normal".

Anything that answers `normal_max(month, hour=None)` and
`normal_uv(month, hour=None)` will do (see `Normals`). The fact scorer
(`facts.py`) takes one as an argument, so swapping in a better table is a
one-line change where it is constructed (`DEFAULT` below).

TODO: replace PLACEHOLDER with real hourly-by-month normals for the
configured location. A separate effort is gathering them. Until then this
is a rough monthly table for central Sydney, and the `hour` argument is
accepted but ignored.
"""

from dataclasses import dataclass
from typing import Optional, Protocol


class Normals(Protocol):
    def normal_max(self, month: int, hour: Optional[int] = None) -> float:
        """The normal daily maximum temperature (°C) for this month. With an
        hour, the normal temperature at that hour (a real hourly table can
        answer that; the placeholder answers the daily maximum)."""

    def normal_uv(self, month: int, hour: Optional[int] = None) -> float:
        """The normal UV index for this month (at this hour, when given)."""


@dataclass(frozen=True)
class MonthlyNormals:
    """One value per month, January first. Ignores the hour."""

    max_temp: tuple
    uv: tuple

    def __post_init__(self):
        if len(self.max_temp) != 12 or len(self.uv) != 12:
            raise ValueError("monthly normals need 12 values each")

    def normal_max(self, month, hour=None):
        return self.max_temp[month - 1]

    def normal_uv(self, month, hour=None):
        return self.uv[month - 1]


# TODO: placeholder. Approximate long-term mean daily maxima for central
# Sydney (rounded), and approximate clear-sky peak UV by month. Good enough to
# tell a 36° day from a 26° one; not a substitute for the real normals.
PLACEHOLDER = MonthlyNormals(
    max_temp=(26.0, 26.0, 25.0, 22.5, 19.5, 17.0, 16.5, 18.0, 20.0, 22.0, 23.5, 25.0),
    uv=(13, 12, 10, 7, 4, 3, 3, 5, 7, 9, 11, 12),
)

DEFAULT = PLACEHOLDER
