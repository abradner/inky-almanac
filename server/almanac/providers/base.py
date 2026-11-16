"""The provider interfaces: one Protocol per kind of external data.

An adapter is a class constructed as `Adapter(cfg, get_text=None)` that
satisfies one of these. `get_text(url) -> str` is its transport: None means
"fetch for real", and `cache.offline` means "read your disk cache only",
which is what the request path passes. Adapters return domain types
(`almanac.domain`, `almanac.hazards.Hazard`) and never raise for an
unreachable source: they fall back to their cache, then to None or empty.

Register a new adapter in `providers/__init__.py`'s REGISTRY and select it
with the matching ALMANAC_* setting (see config.py and the README).
"""

from datetime import date
from typing import Optional, Protocol

from ..domain import DayOutlook, FireDanger, Forecast, MapImage, Photo, RadarImage, StationLogs
from . import cache

HAZARD_TTL = 90  # refreshed every 2 min in the background; requests read the cache
HAZARD_STALE_AFTER = 90 * 60  # an old "Advice" may have become an Emergency, or been cleared


class ForecastProvider(Protocol):
    """Hourly and daily model forecast for the configured location."""

    def forecast(self, now: Optional[float] = None, ttl: Optional[float] = None) -> Optional[Forecast]: ...


class OutlookProvider(Protocol):
    """Official daily forecasts, laid over the model's (domain.apply_outlook)."""

    def outlook(self, now: Optional[float] = None) -> "dict[date, DayOutlook]": ...


class ObservationsProvider(Protocol):
    """Station observations. `refresh` is the only call that touches the
    network: the background refresher makes it, and it must rate-limit
    itself to the source's cadence. `logs` reads the provider's own store."""

    def refresh(self, now: Optional[float] = None) -> None: ...

    def logs(self, now: Optional[float] = None) -> Optional[StationLogs]: ...


class HazardProvider(Protocol):
    """One source of hazards near home. `name` is printed when the source has
    no usable data ("no live data: SES"); `hazards` returns None then."""

    name: str

    def hazards(self, now: float) -> Optional[list]: ...


class WarningMapProvider(Protocol):
    """The threat-area map for the most specific hazard in force, if any."""

    def warning_map(self, hazards: list, now: float) -> Optional[MapImage]: ...


class FireDangerProvider(Protocol):
    def fire_danger(self, day: date, now: float) -> FireDanger: ...


class AirQualityProvider(Protocol):
    """Hourly US AQI forecast: [domain.AirQualityHour], or None."""

    def air_quality(self, now: float) -> Optional[list]: ...


class PhotoProvider(Protocol):
    def photo_for(self, day: date, shuffle: int = 0) -> Optional[Photo]: ...

    def refresh_pool(self) -> None: ...


class RadarProvider(Protocol):
    """Rain radar frames, fetched just in time: the background refresher
    calls `refresh` only while the poster is in radar mode, and it must not
    fetch faster than the source issues frames. `latest` reads the
    provider's own store (never the network)."""

    def refresh(self, now: Optional[float] = None) -> None: ...

    def latest(self, now: Optional[float] = None) -> Optional[RadarImage]: ...


# Normals: see normals.Normals (normal_max / normal_uv), which predates this
# module and which facts.py is written against.


class CachedHazardFeed:
    """Shared shape of a hazard feed: fetch text, check it, cache it, parse
    it. Each source fails on its own, in fetching and in parsing; data older
    than HAZARD_STALE_AFTER counts as unavailable, so the poster can say "no
    live hazard data" instead of looking like a quiet day."""

    name = ""
    cache_name = ""

    def __init__(self, cfg, get_text=None):
        self.cfg = cfg
        self.get_text = get_text or (lambda url: cache.get_text(url, cfg.user_agent))

    def fetch(self):
        raise NotImplementedError

    def check(self, data):
        pass

    def parse(self, data, now):
        raise NotImplementedError

    def hazards(self, now):
        data, _ = cache.cached(self.cfg, self.cache_name, self.fetch, now, ttl=HAZARD_TTL, check=self.check,
                               stale_after=HAZARD_STALE_AFTER)
        if data is None:
            return None
        try:
            return self.parse(data, now)
        except Exception as e:
            print(f"conditions: {self.name} hazards unusable ({e!r})")
            return None
