"""Facts about the coming hour, ranked by how far out of the ordinary they are.

The side panel shows the top fact large beside the coming hour and up to four
more in a table. Each fact gets a score; unremarkable ones score 0 and are
left out. Hazards (hail, storms, fog) have fixed scores and every other fact
is capped below them, so hail and storms always lead.

`score(inputs)` is pure: plain values in, facts out. `inputs_for(forecast,
...)` pulls those values out of a weather.Forecast, and `coming_hour(...)`
does both. The top fact's score also sets how often the panel re-checks
the coming hour between hourly refreshes (`refresh_minutes`), and `digest`
fingerprints what the panel would show, so only a real change redraws (see
app.Almanac.alert).
"""

from dataclasses import dataclass
from datetime import timedelta
from typing import Optional

from . import normals as normals_mod
from . import weather
from .palette import BLACK, BLUE, ORANGE, RED

# Re-check tiers: (lowest top-fact score, minutes between re-checks).
# A redraw takes the panel ~40 s, so nothing re-checks more often than the
# frame's own 10-minute cap on alert redraws.
REFRESH_TIERS = ((90, 10), (75, 15), (50, 30))
# At a re-check, a displayed fact's score must have moved by more than this
# since it was drawn to be worth a redraw: worse news sooner than better.
WORSE_BY = 5
BETTER_BY = 10
NON_HAZARD_CAP = 89  # hail (100) and storms (90) always outrank everything else

HAIL, STORMS = (96, 99), (95,)
FOG = (45, 48)
SNOW = (71, 73, 75, 77, 85, 86)

DIRS = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")


@dataclass(frozen=True)
class Fact:
    key: str  # stable kind ("hail", "rain", ...): part of the fast-refresh alert id
    score: int
    label: str
    value: str
    ink: int
    sub: str = ""
    hazard: bool = False


@dataclass(frozen=True)
class Inputs:
    """Everything the scorer looks at. Wind directions are degrees the wind
    blows *from* (Open-Meteo's convention), or None when unknown."""

    hour: int  # the clock hour now; the coming hour starts at hour + 1
    month: int  # this month (for the UV normal)
    code: int  # WMO weather code of the coming hour
    rain_prob: int = 0
    rain_mm: float = 0.0
    wind: float = 0.0  # the coming hour's mean wind, km/h
    gust: float = 0.0
    dir_now: Optional[float] = None
    dir_next: Optional[float] = None
    uv: float = 0.0
    since9_mm: float = 0.0
    last_hr_mm: float = 0.0
    storms_later: bool = False
    high: Optional[float] = None  # the forecast day's maximum
    high_month: Optional[int] = None  # the forecast day's month
    evening: bool = False  # the forecast describes tomorrow


def half_up(x):
    """Round half away from zero, as the mockup's Math.round does for these
    (positive) values; Python's round() is banker's rounding."""
    return int(x + 0.5) if x >= 0 else -int(-x + 0.5)


def fmt_mm(mm):
    return f"{half_up(mm)}" if mm >= 1 else f"{mm:.1f}"


def compass(deg):
    return DIRS[int((deg % 360) / 45 + 0.5) % 8]


def turn(a, b):
    """The smaller angle between two directions, in degrees."""
    d = abs(a - b) % 360
    return min(d, 360 - d)


# -- one function per fact; each returns a Fact or None ----------------------


def hazard_facts(i):
    out = []
    if i.code in HAIL:
        out.append(Fact("hail", 100, "next hour", "Hail", RED, hazard=True))
    elif i.code in STORMS:
        out.append(Fact("storms", 90, "next hour", "Storms", RED, hazard=True))
    elif i.storms_later:
        out.append(Fact("storms-later", 60, "today", "Storms later", RED, hazard=True))
    if i.code in FOG:
        out.append(Fact("fog", 50, "low visibility", "Fog", BLACK, hazard=True))
    return out


def rain_fact(i):
    if i.rain_mm >= 0.2:
        s = 40 + min(40, i.rain_mm * 8)
    elif i.rain_prob >= 25:
        s = i.rain_prob * 0.6
    else:
        return None
    wet = "Snow" if i.code in SNOW else "Rain"
    return _capped("rain", s, f"{wet} next hr", f"{i.rain_prob}% · {fmt_mm(i.rain_mm)} mm", BLUE)


def since9_fact(i):
    # Both totals are Open-Meteo's modelled precipitation, not gauge readings.
    if i.since9_mm < 1:
        return None
    return _capped("since9", 20 + min(30, i.since9_mm * 2), "Since 9am", f"{fmt_mm(i.since9_mm)} mm", BLUE,
                   sub=f"{fmt_mm(i.last_hr_mm)} mm last hr")


def wind_fact(i):
    wind, gust = half_up(i.wind), half_up(i.gust)
    changing = (
        i.dir_now is not None and i.dir_next is not None
        and turn(i.dir_now, i.dir_next) >= 90 and max(i.wind, i.gust) >= 20
    )
    if changing:
        to = compass(i.dir_next)
        s = 55 + (10 if to == "S" else 0) + max(0, i.gust - 40) * 0.5
        label = "Southerly change" if to == "S" else "Wind change"
        return _capped("wind-change", s, label, f"{compass(i.dir_now)}→{to} gust {gust} km/h", ORANGE)
    if i.gust >= 40 or i.wind >= 30:
        s = 30 + max(0, i.gust - 40) * 0.8
    elif i.gust >= 25:
        s = 10
    else:
        return None
    label = "Wind" if i.dir_next is None else f"Wind {compass(i.dir_next)}"
    return _capped("wind", s, label, f"{wind} gust {gust} km/h", ORANGE)


def uv_fact(i, normals):
    if i.hour >= 18:  # the sun is going down: UV is not news
        return None
    uv = half_up(i.uv)
    if uv >= normals.normal_uv(i.month, i.hour + 1) + 2:
        s, label = 40, "UV, above normal"
    elif uv >= 8:
        s, label = 12, "UV"
    else:
        return None
    return _capped("uv", s, label, f"{uv} {weather.uv_label(uv)}", ORANGE)


def high_fact(i, normals):
    if i.high is None or i.high_month is None:
        return None
    hi = half_up(i.high)
    dev = hi - half_up(normals.normal_max(i.high_month))
    if hi >= 38:
        s = 75
    elif hi >= 35:
        s = 55
    elif abs(dev) >= 6:
        s = 30
    else:
        return None
    label = ("Tomorrow" if i.evening else "Today") + " vs normal"
    return _capped("high", s, label, f"{'+' if dev > 0 else ''}{dev}° ({hi}°)", RED if dev > 0 else BLUE)


def _capped(key, s, label, value, ink, sub=""):
    return Fact(key, half_up(min(NON_HAZARD_CAP, s)), label, value, ink, sub=sub)


def score(i, normals=None):
    """The coming hour's facts, highest score first (ties keep this order:
    hazards, rain, since 9am, wind, UV, the high)."""
    normals = normals or normals_mod.DEFAULT
    found = hazard_facts(i) + [rain_fact(i), since9_fact(i), wind_fact(i), uv_fact(i, normals), high_fact(i, normals)]
    return sorted((f for f in found if f is not None), key=lambda f: -f.score)


# -- from a forecast ---------------------------------------------------------


@dataclass(frozen=True)
class Coming:
    label_hour: int  # the clock hour now; the panel says "3–4PM" for 15
    hour: object  # weather.Hour for the coming hour
    facts: list


def _hour(forecast, when):
    day = forecast.day(when.date())
    return next((h for h in (day.hours if day else []) if h.hour == when.hour), None)


def inputs_for(forecast, now, poster_day, is_tomorrow):
    """Inputs for the hour starting now, or None if the forecast lacks it."""
    if forecast is None or now is None:
        return None
    nxt = now + timedelta(hours=1)
    hour = _hour(forecast, nxt)
    if hour is None:
        return None
    current = _hour(forecast, now)
    # "Since 9am" and "last hr" sum Open-Meteo's *modelled* hourly
    # precipitation (each value covers the hour before its timestamp), not
    # rain-gauge readings. Before 10am there is no completed hour since 9.
    today = forecast.day(now.date())
    since9 = sum(h.rain_mm for h in (today.hours if today else []) if 9 < h.hour <= now.hour)
    later = forecast.day(nxt.date())
    storms_later = any(h.code in STORMS + HAIL for h in later.hours if h.hour > nxt.hour) if later else False
    day_fc = forecast.day(poster_day)
    return Inputs(
        hour=now.hour,
        month=now.month,
        code=hour.code,
        rain_prob=hour.rain_prob,
        rain_mm=hour.rain_mm,
        wind=hour.wind,
        gust=hour.gust,
        dir_now=None if current is None else current.wind_dir,
        dir_next=hour.wind_dir,
        uv=hour.uv,
        since9_mm=since9,
        last_hr_mm=0.0 if current is None else current.rain_mm,
        storms_later=storms_later,
        high=None if day_fc is None else day_fc.t_max,
        high_month=poster_day.month,
        evening=is_tomorrow,
    )


def coming_hour(forecast, now, poster_day, is_tomorrow, normals=None):
    """The coming hour and its ranked facts, or None without data."""
    i = inputs_for(forecast, now, poster_day, is_tomorrow)
    if i is None:
        return None
    return Coming(now.hour, _hour(forecast, now + timedelta(hours=1)), score(i, normals))


def refresh_minutes(coming):
    """How often to re-check the coming hour: 10, 15 or 30 minutes by the top
    fact's score, or None for the ordinary hourly refresh."""
    top = coming.facts[0].score if coming and coming.facts else 0
    return next((minutes for floor, minutes in REFRESH_TIERS if top >= floor), None)


SHOWN_FACTS = 5  # the hero fact and up to four table rows


def shown_state(coming):
    """[(kind, score)] of the facts the panel shows, hero first."""
    return [(f.key, f.score) for f in (coming.facts if coming else [])[:SHOWN_FACTS]]


def worth_redrawing(drawn, now):
    """Whether the facts shown have changed enough since `drawn` (both
    shown_state lists) to redraw before the hour: a fact appeared or went,
    the hero is a different kind, or a score rose by more than WORSE_BY or
    fell by more than BETTER_BY. Anything less (a few mm, a few km/h) waits
    for the hourly refresh."""
    if {k for k, _ in drawn} != {k for k, _ in now}:
        return True
    if drawn and now and drawn[0][0] != now[0][0]:
        return True
    before = dict(drawn)
    return any(s - before[k] > WORSE_BY or before[k] - s > BETTER_BY for k, s in now)


def digest(coming):
    """A short fingerprint of the facts the panel shows (the hero fact and the
    table rows: kind, score, label, value, sub-text)."""
    import hashlib

    shown = [(f.key, f.score, f.label, f.value, f.sub) for f in (coming.facts if coming else [])[:SHOWN_FACTS]]
    return hashlib.sha256(repr(shown).encode()).hexdigest()[:8]
