"""Hazard adapter: NSW RFS current incidents (official GeoJSON feed).

Fires with an Australian Warning System level (Advice / Watch and Act /
Emergency Warning) within the radius of home. Incidents marked "Not
Applicable" or "Planned Burn" are skipped.
"""

import json

from ..hazards import AWS_LEVELS, Hazard, distance_km
from .base import CachedHazardFeed

RFS_INCIDENTS = "https://www.rfs.nsw.gov.au/feeds/majorIncidents.json"


def _suburb(title):
    """'FIFIELD RD, FIFIELD' -> 'Fifield'; 'Goldrock Gully, Legume' -> 'Legume'."""
    title = title or ""
    part = title.split(",")[-1].strip() or title.strip()
    return part.title() or "Unnamed"


def rfs_hazards(geojson, lat, lon, radius_km):
    out = []
    for f in geojson.get("features") or []:
        try:  # one malformed feature must not hide the rest
            p = (f or {}).get("properties") or {}
            level = AWS_LEVELS.get((p.get("category") or "").strip().lower())
            if not level:
                continue  # Not Applicable, Planned Burn, ...
            km = distance_km(f.get("geometry"), lat, lon)
            if km is None or km > radius_km:
                continue
            out.append(Hazard("fire", level, _suburb(p.get("title")), round(km), "RFS", specific=level != "advice",
                          ident="RFS:%s:%s" % (p.get("guid") or p.get("title"), p.get("category"))))
        except Exception as e:
            print(f"hazards: skipped an RFS feature ({e!r})")
    return out


def check_geojson(data):
    if not isinstance(data.get("features"), list):
        raise ValueError("no features")


class RfsIncidents(CachedHazardFeed):
    name = "RFS"
    cache_name = "rfs-incidents.json"

    def fetch(self):
        return json.loads(self.get_text(RFS_INCIDENTS))

    def check(self, data):
        check_geojson(data)

    def parse(self, data, now):
        c = self.cfg
        return rfs_hazards(data, c.latitude, c.longitude, c.hazard_radius_km)
