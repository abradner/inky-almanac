"""Plain domain data: what the scorer, the layout and the scheduler see.

Every external source is turned into these types by an adapter under
`providers/`; nothing outside `providers/` knows which source filled them in
or what its format looked like. See README "Adding or swapping a provider".

Conventions every adapter must follow:

- Temperatures in °C, wind in km/h, rain in mm, wind directions in degrees
  the wind blows *from* (meteorological convention), or None when unknown.
- Weather is described by WMO weather interpretation codes (WMO 4677, as
  used by most global models). An adapter whose source uses its own icon or
  condition codes maps them onto this vocabulary.
- Local times are timezone-aware datetimes or naive local wall-clock times,
  as each field says.
- `source` fields carry a short human credit ("Open-Meteo", "BOM"), only for
  printing on the poster; logic never branches on them.
"""

from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

# ------------------------------------------------------------------ forecast

# WMO weather codes -> (short label, icon kind)
_CODES = {
    0: ("Clear", "clear"),
    1: ("Mostly sunny", "clear"),
    2: ("Partly cloudy", "partly"),
    3: ("Cloudy", "cloudy"),
    45: ("Fog", "fog"),
    48: ("Fog", "fog"),
    51: ("Light drizzle", "drizzle"),
    53: ("Drizzle", "drizzle"),
    55: ("Heavy drizzle", "drizzle"),
    56: ("Freezing drizzle", "drizzle"),
    57: ("Freezing drizzle", "drizzle"),
    61: ("Light rain", "rain"),
    63: ("Rain", "rain"),
    65: ("Heavy rain", "rain"),
    66: ("Freezing rain", "rain"),
    67: ("Freezing rain", "rain"),
    71: ("Light snow", "snow"),
    73: ("Snow", "snow"),
    75: ("Heavy snow", "snow"),
    77: ("Snow grains", "snow"),
    80: ("Showers", "showers"),
    81: ("Showers", "showers"),
    82: ("Heavy showers", "showers"),
    85: ("Snow showers", "snow"),
    86: ("Snow showers", "snow"),
    95: ("Thunderstorms", "storm"),
    96: ("Storms, hail", "storm"),
    99: ("Storms, hail", "storm"),
}


def describe(code):
    return _CODES.get(int(code), ("Unknown", "cloudy"))


def uv_label(uv):
    if uv < 3:
        return "low"
    if uv < 6:
        return "moderate"
    if uv < 8:
        return "high"
    if uv < 11:
        return "very high"
    return "extreme"


@dataclass
class Hour:
    hour: int
    temp: float
    rain_prob: int
    code: int  # WMO weather code
    rain_mm: float = 0.0  # precipitation over the preceding hour
    wind: float = 0.0  # km/h
    gust: float = 0.0  # km/h
    uv: float = 0.0
    wind_dir: float = None  # degrees the wind blows from; None if unknown


@dataclass
class Day:
    day: date
    code: int
    t_max: float
    t_min: float
    rain_prob: int
    rain_mm: float
    uv: float
    sunrise: datetime  # naive local
    sunset: datetime
    hours: list
    # Set when an official daily outlook is laid over the model (apply_outlook).
    label_override: str = None
    icon_override: str = None
    rain_text: str = None
    rain_chance: int = None  # the outlook's chance of any rain, %; None without an outlook
    source: str = ""  # credit for the daily numbers, e.g. "BOM · Open-Meteo"

    @property
    def label(self):
        return self.label_override or describe(self.code)[0]

    @property
    def icon(self):
        return self.icon_override or describe(self.code)[1]


@dataclass
class Forecast:
    days: dict  # {date: Day}
    current_temp: float
    fetched_at: float  # unix seconds
    source: str = ""

    def day(self, d):
        return self.days.get(d)


@dataclass
class DayOutlook:
    """An official daily forecast for one day, laid over the model's Day
    field by field (any None field leaves the model's value alone)."""

    t_min: float = None
    t_max: float = None
    precis: str = None  # one-line summary, no trailing full stop
    rain_prob: int = None
    rain_range: str = None  # "0 to 2 mm"
    icon: str = None  # an icon kind from describe()'s vocabulary
    source: str = ""


def apply_outlook(day_fc, b):
    """Put an official outlook's daily numbers onto a model Day, field by
    field (an outlook may drop today's minimum once the morning has passed,
    for example)."""
    if day_fc is None or b is None:
        return day_fc
    if b.t_max is not None:
        day_fc.t_max = b.t_max
    if b.t_min is not None:
        day_fc.t_min = b.t_min
    if b.rain_prob is not None:
        day_fc.rain_prob = b.rain_prob
        day_fc.rain_chance = b.rain_prob
    if b.precis:
        day_fc.label_override = b.precis
    if b.icon:
        day_fc.icon_override = b.icon
    if b.rain_range:
        day_fc.rain_text = b.rain_range
    if b.source and not day_fc.source.startswith(b.source):
        day_fc.source = f"{b.source} · {day_fc.source}" if day_fc.source else b.source
    return day_fc


# ------------------------------------------------------------------ observations


@dataclass(frozen=True)
class Reading:
    """One station's observation at one moment. Any field may be None when
    the station does not measure it or the report left it out."""

    station: str  # the provider's station id
    name: str  # short place name, e.g. "Horsley Park"
    credit: str  # for the credit line, e.g. "Horsley Park AWS"
    at: datetime  # aware: when the observation was valid
    temp: Optional[float] = None
    apparent: Optional[float] = None
    dew_point: Optional[float] = None
    humidity: Optional[float] = None  # %
    wind_dir: Optional[float] = None  # degrees from
    wind: Optional[float] = None  # km/h
    gust: Optional[float] = None  # km/h
    max_gust: Optional[float] = None  # km/h, highest so far today
    # Rain accumulated since rain_start (the 9am reset, in Australia), and the
    # previous full accumulation period, which ends where this one starts.
    rain: Optional[float] = None
    rain_start: Optional[datetime] = None
    rain_prev_period: Optional[float] = None
    rain_prev_end: Optional[datetime] = None


@dataclass(frozen=True)
class StationLogs:
    """Successive readings per station, oldest first, as the provider has
    seen them (a few hours' worth). `home` is the station whose readings
    describe home; `crosscheck` stations stand in when it is silent;
    `leading` stations sit where weather arrives first (a southerly change
    reaches the coast before it reaches the west)."""

    logs: dict  # {station id: [Reading, ...]}
    home: str
    crosscheck: tuple = ()
    leading: tuple = ()
    source: str = ""

    def log(self, station):
        return self.logs.get(station, [])


# ------------------------------------------------------------------ hazards and maps


@dataclass(frozen=True)
class MapImage:
    """A warning's threat-area map, ready to draw in place of the photo."""

    png: bytes
    title: str
    area: str = ""
    issued: str = ""
    credit: str = ""


@dataclass(frozen=True)
class AirQualityHour:
    at: datetime  # naive local
    us_aqi: Optional[float]


@dataclass
class FireDanger:
    rating: Optional[str] = None  # MODERATE, HIGH, EXTREME, CATASTROPHIC or None
    ban: bool = False


@dataclass
class Photo:
    id: str
    title: str
    credit: str
    image: object  # PIL.Image.Image
