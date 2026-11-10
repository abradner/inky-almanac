"""Bureau of Meteorology town forecast (précis) from BOM's public FTP products.

IDN11060 is the NSW town forecast XML: for each town, seven days of
max/min, a one-line précis ("Shower or two. Possible storm."), chance and
range of rain, and BOM's own icon code. These are BOM's official forecasts,
so they replace Open-Meteo's daily numbers; Open-Meteo still supplies the
hourly strip, UV and sun times, which this product doesn't carry.

(BOM's JSON API at api.weather.bom.gov.au is undocumented and every response
says "You must not use, copy or share it", so it isn't used.)
"""

import re
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime

PRECIS_NSW = "ftp://ftp.bom.gov.au/anon/gen/fwo/IDN11060.xml"

# BOM forecast_icon_code -> (icon kind used by icons.weather)
ICONS = {
    1: "clear", 2: "clear", 3: "partly", 4: "cloudy", 6: "fog", 8: "drizzle",
    9: "cloudy", 10: "fog", 11: "showers", 12: "rain", 13: "fog", 14: "clear",
    15: "snow", 16: "storm", 17: "showers", 18: "rain", 19: "storm",
}


@dataclass
class BomDay:
    t_min: float = None
    t_max: float = None
    precis: str = None
    rain_prob: int = None
    rain_range: str = None  # "0 to 2 mm"
    icon: str = None


def parse(xml_text, town):
    """{date: BomDay} for `town` (the area's description, e.g. "Sydney")."""
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
        b = BomDay()
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


def overlay(day_fc, b):
    """Put BOM's official daily numbers onto an Open-Meteo Day, field by field
    (BOM drops today's minimum once the morning has passed, for example)."""
    if day_fc is None or b is None:
        return day_fc
    if b.t_max is not None:
        day_fc.t_max = b.t_max
    if b.t_min is not None:
        day_fc.t_min = b.t_min
    if b.rain_prob is not None:
        day_fc.rain_prob = b.rain_prob
    if b.precis:
        day_fc.label_override = b.precis
    if b.icon:
        day_fc.icon_override = b.icon
    if b.rain_range:
        day_fc.rain_text = b.rain_range
    day_fc.source = "BOM"
    return day_fc
