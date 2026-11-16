"""Hazards near home, in the spirit of the Hazards Near Me NSW app: the
domain type, its ranking and identity, and the geometry adapters use to say
how far a warning area is from home.

The sources (RFS incidents, SES warnings via HazardWatch, BOM warnings) are
adapters under providers/; each turns its feed into `Hazard`s. A hazard
counts when home is inside its area or within the configured radius. Each
becomes a short badge; nothing here tries to republish a warning, and the
badge points you at Hazards Near Me / BOM for detail.
"""

import math
from dataclasses import dataclass

# Australian Warning System levels, most urgent first.
LEVELS = ("emergency", "watch", "advice")
# AWS level names as agencies write them -> LEVELS (for adapters).
AWS_LEVELS = {
    "emergency warning": "emergency",
    "watch and act": "watch",
    "advice": "advice",
}


@dataclass(frozen=True)
class Hazard:
    kind: str  # "fire", "flood", "storm", "weather", ...
    level: str  # one of LEVELS (a named warning without an AWS level: "watch" or "advice")
    where: str  # short place text; for a titled warning, its name
    km: float = None  # distance from home; 0 inside the area; None if unknown
    source: str = ""  # credit only ("RFS", "BOM"): logic never branches on it
    # Specific hazards pinpoint a threat near home that asks for action (a
    # storm cell, a Watch and Act fire, a flooding river): they trigger an
    # immediate refresh and may bring a map. Broad ones are notices (flood
    # watch, fire weather, and RFS/SES "Advice"): badges at the hourly cadence.
    specific: bool = True
    ident: str = ""  # stable identity of this issue of the warning (alert id)
    url: str = None  # the warning's page, for its map
    detail: str = ""  # titled warnings: the area it covers ("Blue Mountains/Hawkesbury and Sydney")
    # A titled warning is a named product ("Severe Thunderstorm Warning")
    # rather than an incident at a place: `where` is its name, it has no
    # distance, and the Australian Warning System's colours don't apply.
    titled: bool = False

    @property
    def rank(self):
        return (LEVELS.index(self.level), self.km if self.km is not None else 999)


# ------------------------------------------------------------------ geometry


def haversine_km(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(a))


def _inside(ring, lon, lat):
    """Ray casting; ring is [[lon, lat], ...]."""
    inside = False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if (yi > lat) != (yj > lat) and lon < (xj - xi) * (lat - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def _geometries(g):
    if not g:
        return
    if g.get("type") == "GeometryCollection":
        for sub in g.get("geometries", []):
            yield from _geometries(sub)
    else:
        yield g


def _seg_km(lat, lon, a, b):
    """Distance from home to segment a-b ([lon, lat] each), in a flat
    projection centred on home: plenty accurate at tens of km."""
    kx = 111.32 * math.cos(math.radians(lat))
    ky = 110.57
    ax, ay = (a[0] - lon) * kx, (a[1] - lat) * ky
    bx, by = (b[0] - lon) * kx, (b[1] - lat) * ky
    dx, dy = bx - ax, by - ay
    L = dx * dx + dy * dy
    t = 0.0 if L == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / L))
    px, py = ax + t * dx, ay + t * dy
    return math.hypot(px, py)


def _rings(g):
    t, coords = g.get("type"), g.get("coordinates") or []
    if t == "Polygon":
        return [coords]
    if t == "MultiPolygon":
        return coords
    return []


def distance_km(geometry, lat, lon):
    """0 if (lat, lon) is inside any polygon (outer ring), else the distance to
    the nearest point or polygon EDGE (not just the nearest vertex: a long
    straight edge can pass close by between far-apart corners). None if the
    geometry has no usable coordinates."""
    best = None
    for g in _geometries(geometry):
        if g.get("type") == "Point":
            c = g.get("coordinates")
            if c and len(c) >= 2:
                d = haversine_km(lat, lon, c[1], c[0])
                best = d if best is None else min(best, d)
            continue
        for poly in _rings(g):
            if not poly or not poly[0]:
                continue
            if _inside(poly[0], lon, lat):
                return 0.0
            for ring in poly:
                for a, b in zip(ring, ring[1:]):
                    d = _seg_km(lat, lon, a, b)
                    best = d if best is None else min(best, d)
    return best


def alert_id(hazards):
    """Identity of the specific hazards in force ("" if none). It changes when
    one appears, is reissued, escalates or is cancelled."""
    import hashlib

    idents = sorted(h.ident or repr(h) for h in hazards if h.specific)
    return hashlib.sha256("\n".join(idents).encode()).hexdigest()[:12] if idents else ""


def sort_and_dedupe(hazards):
    seen, out = set(), []
    for h in sorted(hazards, key=lambda h: h.rank):
        # Titled warnings share names ("Flood Warning" for two rivers): keep each.
        key = h.ident if h.titled and h.ident else (h.kind, h.level, h.where)
        if key not in seen:
            seen.add(key)
            out.append(h)
    return out
