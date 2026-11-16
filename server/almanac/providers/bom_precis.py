"""Daily outlook adapter: the Bureau of Meteorology town forecast (précis)
from BOM's public FTP products, as domain.DayOutlook.

IDN11060 is the NSW town forecast XML: for each town, seven days of
max/min, a one-line précis ("Shower or two. Possible storm."), chance and
range of rain, and BOM's own icon code. These are BOM's official forecasts,
so they are laid over the forecast provider's daily numbers
(domain.apply_outlook); the forecast provider still supplies the hourly
strip, UV and sun times, which this product doesn't carry.

(BOM's JSON API at api.weather.bom.gov.au is undocumented and every response
says "You must not use, copy or share it", so it isn't used.)
"""

import json
import re
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime

from ..domain import DayOutlook
from . import cache

PRECIS_NSW = "ftp://ftp.bom.gov.au/anon/gen/fwo/IDN11060.xml"
SOURCE = "BOM"

# BOM forecast_icon_code -> (icon kind used by icons.weather)
ICONS = {
    1: "clear", 2: "clear", 3: "partly", 4: "cloudy", 6: "fog", 8: "drizzle",
    9: "cloudy", 10: "fog", 11: "showers", 12: "rain", 13: "fog", 14: "clear",
    15: "snow", 16: "storm", 17: "showers", 18: "rain", 19: "storm",
}


def parse(xml_text, town):
    """{date: DayOutlook} for `town` (the area's description, e.g. "Sydney")."""
    root = ET.fromstring(xml_text)
    area = None
    for a in root.iter("area"):
        if a.get("type") == "location" and (a.get("description") or "").lower() == town.lower():
            area = a
            break
    if area is None:
        raise ValueError(f"BOM précis has no town {town!r}")
    out = {}
    for p in area.findall("forecast-period"):
        day = datetime.fromisoformat(p.get("start-time-local")).date()
        b = DayOutlook(source=SOURCE)
        for e in p.findall("element"):
            t, v = e.get("type"), (e.text or "").strip()
            if t == "air_temperature_minimum":
                b.t_min = float(v)
            elif t == "air_temperature_maximum":
                b.t_max = float(v)
            elif t == "precipitation_range":
                b.rain_range = v
            elif t == "forecast_icon_code" and v.isdigit():
                b.icon = ICONS.get(int(v))
        for t in p.findall("text"):
            kind, v = t.get("type"), (t.text or "").strip()
            if kind == "precis":
                b.precis = v.rstrip(".")
            elif kind == "probability_of_precipitation":
                m = re.match(r"(\d+)", v)
                b.rain_prob = int(m.group(1)) if m else None
        out[day] = b
    return out


def fetch(ua):
    req = urllib.request.Request(PRECIS_NSW, headers={"User-Agent": ua})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8")


def load_bom_days(cfg, now=None, get_text=None):
    """{date: DayOutlook} for cfg.bom_town, or {} if BOM can't be reached."""
    now = time.time() if now is None else now
    get_text = get_text or (lambda url: fetch(cfg.user_agent))
    xml_text, _ = cache.cached(cfg, "bom-precis.json", lambda: get_text(PRECIS_NSW), now, ttl=30 * 60,
                               check=lambda x: parse(x, cfg.bom_town))
    if not xml_text:
        return {}
    try:
        days = parse(xml_text, cfg.bom_town)
    except (ValueError, ET.ParseError) as e:
        print(f"conditions: BOM précis unusable ({e!r})")
        return {}
    return _keep_first_seen(cfg, days)


def _keep_first_seen(cfg, days):
    """BOM's afternoon issue drops today's maximum and swaps the day précis
    for the evening one. Keep the first-seen max, précis and icon per day, so
    the poster doesn't quietly change (and refresh) mid-afternoon."""
    path = cfg.cache_dir / "bom-first-seen.json"
    try:
        seen = json.loads(path.read_text())
    except (OSError, ValueError):
        seen = {}
    for day, b in days.items():
        key = day.isoformat()
        first = seen.get(key)
        if first is None:
            seen[key] = {"t_max": b.t_max, "precis": b.precis, "icon": b.icon}
            continue
        if first.get("t_max") is None and b.t_max is not None:
            first["t_max"] = b.t_max
        b.t_max = first.get("t_max") if first.get("t_max") is not None else b.t_max
        b.precis = first.get("precis") or b.precis
        b.icon = first.get("icon") or b.icon
    keep = sorted(seen)[-10:]  # a week and a bit
    try:
        path.write_text(json.dumps({k: seen[k] for k in keep}))
    except OSError:
        pass
    return days


class BomOutlook:
    """The OutlookProvider for BOM's NSW town forecasts (IDN11060)."""

    name = "bom"

    def __init__(self, cfg, get_text=None):
        self.cfg = cfg
        self._get_text = get_text

    def outlook(self, now=None):
        return load_bom_days(self.cfg, now=now, get_text=self._get_text)
