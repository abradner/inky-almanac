"""Safety conditions worth a glance: fire danger, smoke and hazards near home.

Gathers what the configured providers say into one `Conditions` for the
poster's day:

- Fire danger rating and Total Fire Ban for the poster's day (a
  FireDangerProvider; the AFDRS colours happen to be inks this panel has).
- Air quality: an hourly US AQI forecast. Shown only when it's unhealthy
  (smoke or haze days), so on a normal day the poster stays uncluttered.
- Hazards near home from every HazardProvider, and the map of the most
  specific warning in force (a WarningMapProvider).

Providers cache on disk and fail quietly: a badge is better missing than
wrong, so data older than their staleness limits is ignored, not shown.
"""

import time
from dataclasses import dataclass, field

from . import hazards as hz
from . import providers as providers_mod
from .domain import MapImage
from .providers.base import HAZARD_STALE_AFTER, HAZARD_TTL  # noqa: F401 (policy constants, re-exported)
from .providers.cache import CacheOnly, offline  # noqa: F401 (the request path's transport)

AQI_SHOW_FROM = 101  # "unhealthy for sensitive groups" and worse


@dataclass
class Conditions:
    fire_rating: str = None  # MODERATE, HIGH, EXTREME, CATASTROPHIC, or None (no rating / unknown)
    fire_ban: bool = False
    aqi: int = None  # worst daytime US AQI, only if >= AQI_SHOW_FROM
    hazards: list = field(default_factory=list)  # hazards.Hazard, most urgent first
    unavailable: list = field(default_factory=list)  # hazard sources with no usable data
    alert: str = ""  # identity of the specific hazards in force (hazards.alert_id)
    warning: MapImage = None  # the map of a warning in force, when there is one


def worst_daytime_aqi(hours, day, first_hour=6, last_hour=22):
    """The worst AQI over [first_hour, last_hour] of `day`, from
    [domain.AirQualityHour]."""
    worst = None
    for h in hours:
        if h.us_aqi is None or h.at.date() != day or not first_hour <= h.at.hour <= last_hour:
            continue
        worst = h.us_aqi if worst is None else max(worst, h.us_aqi)
    return None if worst is None else int(round(worst))


def load(cfg, day, tomorrow, now=None, get_text=None, from_hour=None, providers=None):
    """Conditions for poster day `day`. `from_hour` (today's view) ignores AQI
    hours already past, so morning smoke doesn't linger on the badge all day.
    `providers` (a providers.Providers) defaults to the configured ones with
    `get_text` as their transport."""
    now = time.time() if now is None else now
    p = providers or providers_mod.build(cfg, get_text)
    c = Conditions()

    if p.fire_danger is not None:
        fd = p.fire_danger.fire_danger(day, now)
        c.fire_rating, c.fire_ban = fd.rating, fd.ban

    c.hazards, c.unavailable = load_hazards(cfg, now, get_text, providers=p)
    c.alert = hz.alert_id(c.hazards)
    c.warning = p.warning_maps.warning_map(c.hazards, now) if p.warning_maps is not None else None

    aq = p.air_quality.air_quality(now) if p.air_quality is not None else None
    if aq:
        first = cfg.first_hour if from_hour is None else max(cfg.first_hour, from_hour)
        worst = worst_daytime_aqi(aq, day, first, cfg.last_hour)
        if worst is not None and worst >= AQI_SHOW_FROM:
            c.aqi = worst
    return c


def load_hazards(cfg, now, get_text=None, providers=None):
    """(hazards near home, names of sources with no usable data).

    Each source fails on its own, in fetching and in parsing: one broken
    feed never hides the others. A source with no usable data is named, so
    the poster can say "no live hazard data" instead of looking like a
    quiet day."""
    p = providers or providers_mod.build(cfg, get_text)
    found, missing = [], []
    for source in p.hazards:
        got = source.hazards(now)
        if got is None:
            missing.append(source.name)
        else:
            found += got
    return hz.sort_and_dedupe(found), missing


def aqi_label(aqi):
    if aqi >= 301:
        return "hazardous"
    if aqi >= 201:
        return "very unhealthy"
    if aqi >= 151:
        return "unhealthy"
    return "poor"
