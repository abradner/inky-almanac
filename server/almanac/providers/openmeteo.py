"""Forecast adapter: Open-Meteo (free, no key), cached on disk.

Turns Open-Meteo's forecast JSON into domain.Forecast. If Open-Meteo is
unreachable the last good forecast is reused and the poster says how old it
is, so a dead network never blanks the frame.
"""

import json
import time
import urllib.parse
import urllib.request
from datetime import date, datetime

from ..domain import Day, Forecast, Hour, describe, uv_label  # noqa: F401 (re-exported for tests)

API = "https://api.open-meteo.com/v1/forecast"
TTL_SECONDS = 20 * 60
SOURCE = "Open-Meteo"


def _num(v, default=0.0):
    return default if v is None else v


def _at(block, key, i):
    v = block.get(key)
    return v[i] if v and i < len(v) else None


def parse(payload, fetched_at):
    daily = payload["daily"]
    hourly = payload["hourly"]
    by_day = {}
    for i, t in enumerate(hourly["time"]):
        ts = datetime.fromisoformat(t)
        by_day.setdefault(ts.date(), []).append(
            Hour(
                hour=ts.hour,
                temp=_num(hourly["temperature_2m"][i]),
                rain_prob=int(_num(hourly["precipitation_probability"][i], 0)),
                code=int(_num(hourly["weather_code"][i], 3)),
                rain_mm=_num(_at(hourly, "precipitation", i)),
                wind=_num(_at(hourly, "wind_speed_10m", i)),
                gust=_num(_at(hourly, "wind_gusts_10m", i)),
                uv=_num(_at(hourly, "uv_index", i)),
                wind_dir=_at(hourly, "wind_direction_10m", i),
            )
        )
    days = {}
    for i, d in enumerate(daily["time"]):
        dd = date.fromisoformat(d)
        days[dd] = Day(
            day=dd,
            code=int(_num(daily["weather_code"][i], 3)),
            t_max=_num(daily["temperature_2m_max"][i]),
            t_min=_num(daily["temperature_2m_min"][i]),
            rain_prob=int(_num(daily["precipitation_probability_max"][i], 0)),
            rain_mm=_num(daily["precipitation_sum"][i]),
            uv=_num(daily["uv_index_max"][i]),
            sunrise=datetime.fromisoformat(daily["sunrise"][i]),
            sunset=datetime.fromisoformat(daily["sunset"][i]),
            hours=by_day.get(dd, []),
            source=SOURCE,
        )
    current = payload.get("current", {}).get("temperature_2m")
    return Forecast(days=days, current_temp=current, fetched_at=fetched_at, source=SOURCE)


def _url(cfg):
    q = {
        "latitude": cfg.latitude,
        "longitude": cfg.longitude,
        "timezone": cfg.timezone,
        "forecast_days": 4,
        # The hourly series starts at 00:00 today, so the hours already past
        # are there too (modelled, not observed): facts.py sums them for
        # "since 9am" when no station observations are available.
        "current": "temperature_2m,weather_code",
        "hourly": "temperature_2m,precipitation_probability,weather_code,precipitation,wind_speed_10m,wind_gusts_10m,wind_direction_10m,uv_index",
        "daily": ",".join(
            [
                "weather_code",
                "temperature_2m_max",
                "temperature_2m_min",
                "sunrise",
                "sunset",
                "uv_index_max",
                "precipitation_probability_max",
                "precipitation_sum",
            ]
        ),
    }
    return API + "?" + urllib.parse.urlencode(q)


def load(cfg, now=None, fetch=None, ttl=TTL_SECONDS):
    """Return a Forecast, from cache when fresh, from the network otherwise.

    Falls back to a stale cache when the network fails; returns None only if
    there has never been a successful fetch. `ttl` is how old the cache may
    be before a fetch (shorter while the panel re-checks an urgent hour).
    """
    now = time.time() if now is None else now
    path = cfg.cache_dir / "weather.json"
    cached = None
    if path.exists():
        try:
            cached = json.loads(path.read_text())
        except ValueError:
            cached = None
    if cached and now - cached["fetched_at"] < ttl:
        return parse(cached["payload"], cached["fetched_at"])
    try:
        if fetch is None:
            req = urllib.request.Request(_url(cfg), headers={"User-Agent": cfg.user_agent})
            with urllib.request.urlopen(req, timeout=20) as r:
                payload = json.load(r)
        else:
            payload = fetch(_url(cfg))
        forecast = parse(payload, now)  # junk must never replace a good cache
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"fetched_at": now, "payload": payload}))
        tmp.replace(path)
        return forecast
    except Exception as e:  # network, HTTP or JSON trouble: keep the old forecast
        print(f"weather: fetch failed ({e!r}); using cache" if cached else f"weather: fetch failed ({e!r})")
        if cached:
            return parse(cached["payload"], cached["fetched_at"])
        return None


class OpenMeteoForecast:
    """The ForecastProvider for Open-Meteo. `get_text(url)` is the transport
    (providers.cache.offline on the request path: cache only)."""

    name = "openmeteo"
    ttl = TTL_SECONDS

    def __init__(self, cfg, get_text=None):
        self.cfg = cfg
        self._get_text = get_text

    def forecast(self, now=None, ttl=TTL_SECONDS):
        fetch = None if self._get_text is None else (lambda url: json.loads(self._get_text(url)))
        return load(self.cfg, now=now, fetch=fetch, ttl=ttl)
