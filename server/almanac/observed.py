"""What the stations have actually seen: rain since 9am and in the last hour,
and whether a southerly change has arrived, from domain.StationLogs.

Pure: logs and a moment in, an `Observed` out. Nothing here knows which
provider filled the logs in.

Staleness: a reading older than STALE_AFTER counts as missing, so a dead
feed falls back to the forecast's modelled figures instead of freezing.

"Here" is the home station, or the first cross-check station with a fresh
reading when home is silent.

Rain in the last hour differences successive cumulative "since 9am"
readings an hour apart (within HOUR_TOLERANCE). Across the 9am reset the
part before the reset comes from the previous period's total, which the
newest reading carries; without it, or with a gap in the log, the figure is
unknown (None) rather than guessed.

A southerly change, at one station, is all of:

1. the newest reading is fresh, its wind blows from the southerly sector
   (SOUTHERLY_FROM..SOUTHERLY_TO degrees: SE through S to SW) at
   MIN_WIND_KMH or more;
2. the wind swung into that sector within SOUTHERLY_WINDOW: the reading
   just before the swing (the baseline, at most BASELINE_GAP older) blew
   from outside it, MIN_TURN_DEG or more away; and
3. the air behind it is different: the temperature fell by TEMP_DROP_C or
   more from the baseline, or the gust rose by GUST_RISE_KMH or more.

A swing of wind alone, with neither a temperature drop nor a gust rise, is
not a southerly change (sea breezes and eddies do that all summer). A
change seen at home is `southerly_here`; one seen at a leading (coastal)
station but not yet at home is `southerly_coast`: it is on its way.
"""

from dataclasses import dataclass
from datetime import timedelta
from typing import Optional

STALE_AFTER = timedelta(minutes=30)
CLOCK_SKEW = timedelta(minutes=2)  # a reading this far "in the future" still counts
HOUR_TOLERANCE = timedelta(minutes=10)

SOUTHERLY_FROM, SOUTHERLY_TO = 135.0, 225.0  # SE .. SW, degrees the wind blows from
MIN_WIND_KMH = 10.0
SOUTHERLY_WINDOW = timedelta(minutes=60)
BASELINE_GAP = timedelta(minutes=30)
MIN_TURN_DEG = 45.0
TEMP_DROP_C = 2.0
GUST_RISE_KMH = 15.0


@dataclass(frozen=True)
class Southerly:
    station: str  # short name, e.g. "Kurnell"
    at: object  # aware datetime of the first reading after the swing
    from_dir: float
    to_dir: float
    temp_drop: Optional[float]
    gust: Optional[float]


@dataclass(frozen=True)
class Observed:
    station: str  # the "here" station's short name
    credit: str  # the station, for the credit line, e.g. "Horsley Park AWS"
    at: object  # aware datetime of its newest reading
    since9_mm: Optional[float] = None
    last_hr_mm: Optional[float] = None
    southerly_here: Optional[Southerly] = None
    southerly_coast: Optional[Southerly] = None
    source: str = ""  # e.g. "BOM"
    temp: Optional[float] = None  # air temperature at `at`, °C


def _usable(log, now):
    return [r for r in log if r.at <= now + CLOCK_SKEW]


def fresh(reading, now):
    return reading is not None and -CLOCK_SKEW <= now - reading.at <= STALE_AFTER


def latest(log, now):
    """The newest reading at or before now, if it is fresh."""
    log = _usable(log, now)
    return log[-1] if log and fresh(log[-1], now) else None


def rain_last_hour(log, now):
    """mm in the hour to the newest fresh reading, or None if unknown."""
    log = _usable(log, now)
    last = latest(log, now)
    if last is None or last.rain is None:
        return None
    target = last.at - timedelta(hours=1)
    near = [r for r in log if abs(r.at - target) <= HOUR_TOLERANCE]
    if not near:
        return None  # a gap in the log: no reading an hour back
    before = min(near, key=lambda r: abs(r.at - target))
    if before.rain is None or before.rain_start is None or last.rain_start is None:
        return None
    if before.rain_start == last.rain_start:
        d = last.rain - before.rain
    elif (
        before.rain_start < last.rain_start
        and last.rain_prev_end == last.rain_start
        and last.rain_prev_period is not None
    ):
        # The 9am reset fell inside the hour: what fell after `before` and
        # up to the reset, then what has fallen since.
        d = (last.rain_prev_period - before.rain) + last.rain
        if last.rain_prev_period < before.rain:
            return None
    else:
        return None
    return round(d, 1) if d >= 0 else None


def southerly(log, now):
    """A Southerly for this station's log, or None (see the module rules)."""
    log = _usable(log, now)
    last = latest(log, now)
    if last is None or not _southerly_wind(last):
        return None
    i = len(log) - 1
    while i > 0 and _southerly_wind(log[i - 1]):
        i -= 1
    if i == 0:
        return None  # southerly for as long as the log goes back: no swing seen
    first, base = log[i], log[i - 1]
    if now - first.at > SOUTHERLY_WINDOW or first.at - base.at > BASELINE_GAP or base.wind_dir is None:
        return None
    if _turn(base.wind_dir, first.wind_dir) < MIN_TURN_DEG:
        return None
    after = log[i:]
    temps = [r.temp for r in after if r.temp is not None]
    gusts = [r.gust for r in after if r.gust is not None]
    drop = base.temp - min(temps) if base.temp is not None and temps else None
    rise = max(gusts) - base.gust if base.gust is not None and gusts else None
    if not ((drop is not None and drop >= TEMP_DROP_C) or (rise is not None and rise >= GUST_RISE_KMH)):
        return None
    return Southerly(last.name, first.at, base.wind_dir, first.wind_dir, drop, max(gusts) if gusts else None)


def summarise(logs, now):
    """Observed for `now` from domain.StationLogs, or None when no "here"
    station has a fresh reading."""
    if logs is None:
        return None
    here = None
    for station in (logs.home, *logs.crosscheck):
        if latest(logs.log(station), now) is not None:
            here = station
            break
    if here is None:
        return None
    log = logs.log(here)
    last = latest(log, now)
    s_here = southerly(log, now)
    s_coast = None
    if s_here is None:
        seen = [s for s in (southerly(logs.log(st), now) for st in logs.leading if st != here) if s]
        s_coast = max(seen, key=lambda s: s.at) if seen else None
    return Observed(
        station=last.name,
        credit=last.credit,
        source=logs.source,
        at=last.at,
        # Before 9am a "since 9am" total still runs from yesterday's 9am.
        since9_mm=last.rain if last.rain_start is not None and last.rain_start.date() == last.at.date() else None,
        last_hr_mm=rain_last_hour(log, now),
        southerly_here=s_here,
        southerly_coast=s_coast,
        temp=last.temp,
    )


def _southerly_wind(r):
    return (
        r.wind_dir is not None
        and SOUTHERLY_FROM <= r.wind_dir % 360 <= SOUTHERLY_TO
        and r.wind is not None
        and r.wind >= MIN_WIND_KMH
    )


def _turn(a, b):
    d = abs(a - b) % 360
    return min(d, 360 - d)
