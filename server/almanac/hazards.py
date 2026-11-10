"""Hazards near home, in the spirit of the Hazards Near Me NSW app.

Three sources, all public:
- NSW RFS current incidents (official GeoJSON feed): fires with an
  Australian Warning System level (Advice / Watch and Act / Emergency
  Warning). Incidents marked "Not Applicable" or "Planned Burn" are skipped.
- NSW SES warnings (floods, storms, tsunami) from HazardWatch, the platform
  behind Hazards Near Me. SES offers no open API, so this reads the alerts
  HazardWatch embeds in its own page (CAP-AU alerts plus their polygons). It
  is the fragile one: if the page changes shape it yields nothing, quietly.
- Bureau of Meteorology warnings for NSW (public RSS), filtered to the areas
  that cover home (Sydney Metropolitan, Hawkesbury-Nepean, ...).

RFS and SES hazards count when home is inside the warning area or within
radius_km of it. Each becomes a short badge; nothing here tries to republish
a warning, and the badge points you at Hazards Near Me / BOM for detail.
"""

import json
import math
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime

RFS_INCIDENTS = "https://www.rfs.nsw.gov.au/feeds/majorIncidents.json"
HAZARDWATCH = "https://www.hazardwatch.gov.au/"
BOM_WARNINGS_NSW = "https://reg.bom.gov.au/fwo/IDZ00054.warnings_nsw.xml"

# Australian Warning System levels, most urgent first.
LEVELS = ("emergency", "watch", "advice")
_AWS = {
    "emergency warning": "emergency",
    "watch and act": "watch",
    "advice": "advice",
}


@dataclass(frozen=True)
class Hazard:
    kind: str  # "fire", "flood", "storm", "weather", ...
    level: str  # one of LEVELS ("watch" for BOM warnings)
    where: str  # short place text
    km: float = None  # distance from home; 0 inside the area; None if unknown
    source: str = ""

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


# ------------------------------------------------------------------ RFS


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
            level = _AWS.get((p.get("category") or "").strip().lower())
            if not level:
                continue  # Not Applicable, Planned Burn, ...
            km = distance_km(f.get("geometry"), lat, lon)
            if km is None or km > radius_km:
                continue
            out.append(Hazard("fire", level, _suburb(p.get("title")), round(km), "RFS"))
        except Exception as e:
            print(f"hazards: skipped an RFS feature ({e!r})")
    return out


# ------------------------------------------------------------------ SES (HazardWatch)


def hazardwatch_data(html):
    m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html, re.S)
    if not m:
        raise ValueError("HazardWatch page has no __NEXT_DATA__")
    data = json.loads(m.group(1))["props"]["pageProps"]["alertData"]
    if not isinstance(data.get("alerts"), list):
        raise ValueError("HazardWatch alertData has no alerts list")
    return data


_KIND = (("flood", "flood"), ("storm", "storm"), ("tsunami", "tsunami"), ("wind", "storm"), ("fire", "fire"))


def _kind(event):
    e = (event or "").lower()
    for word, kind in _KIND:
        if word in e:
            return kind
    return "hazard"


def _expired(info, now_utc):
    exp = info.get("expires")
    if not exp or now_utc is None:
        return False
    try:
        return datetime.fromisoformat(exp.replace("Z", "+00:00")) <= now_utc
    except ValueError:
        return False


def ses_hazards(alert_data, lat, lon, radius_km, skip_agencies=("NSWRFS",), now_utc=None):
    """SES (and other non-RFS) alerts near home that haven't expired. RFS
    alerts are skipped here because the RFS feed already covers them."""
    geo = {}
    for f in (alert_data.get("geojson") or {}).get("features") or []:
        ident = ((f or {}).get("properties") or {}).get("alertIdentifier")
        if ident:
            geo[ident] = f.get("geometry")
    out = []
    for a in alert_data.get("alerts") or []:
        try:  # one malformed alert must not hide the rest
            if a.get("agencyCode") in skip_agencies or a.get("status") != "Actual":
                continue
            info = a.get("info") or {}
            if _expired(info, now_utc):
                continue
            param = info.get("parameter") or {}
            level = _AWS.get((param.get("AustralianWarningSystem:WarningLevel") or "").strip().lower())
            if not level:
                continue
            km = distance_km(geo.get(a.get("identifier")), lat, lon)
            if km is None or km > radius_km:
                continue
            where = param.get("AffectedLocation") or " ".join((info.get("headline") or "").split()) or "Unnamed"
            out.append(Hazard(_kind(info.get("event")), level, where.strip(), round(km), a.get("agencyCode") or "SES"))
        except Exception as e:
            print(f"hazards: skipped an SES alert ({e!r})")
    return out


# ------------------------------------------------------------------ BOM


def bom_hazards(rss_xml, areas, ignore):
    """BOM NSW warnings whose title names one of `areas` and none of `ignore`."""
    out = []
    root = ET.fromstring(rss_xml)
    for item in root.iter("item"):
        title = " ".join((item.findtext("title") or "").split())
        # "10/04:40 EDT Severe Thunderstorm Warning for ..." -> drop the stamp
        title = re.sub(r"^\d{1,2}/\d{2}:\d{2}\s+\w+\s+", "", title)
        low = title.lower()
        if not any(a.lower() in low for a in areas) or any(i.lower() in low for i in ignore):
            continue
        if low.startswith(("final", "cancellation")):
            continue
        name = re.split(r"\s+for\s+", title, maxsplit=1)[0]  # "Severe Thunderstorm Warning"
        kind = "flood" if "flood" in low else "storm" if "thunderstorm" in low else "weather"
        level = "advice" if "watch" in name.lower() or "minor" in name.lower() else "watch"
        out.append(Hazard(kind, level, name, None, "BOM"))
    return out


def check_rss(xml_text):
    if ET.fromstring(xml_text).find("channel") is None:
        raise ValueError("not an RSS document")


def check_geojson(data):
    if not isinstance(data.get("features"), list):
        raise ValueError("no features")


def sort_and_dedupe(hazards):
    seen, out = set(), []
    for h in sorted(hazards, key=lambda h: h.rank):
        key = (h.kind, h.level, h.where)
        if key not in seen:
            seen.add(key)
            out.append(h)
    return out
