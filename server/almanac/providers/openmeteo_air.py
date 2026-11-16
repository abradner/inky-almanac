"""Air quality adapter: Open-Meteo's US AQI forecast, as domain.AirQualityHour."""

import json
import urllib.parse
from datetime import datetime

from ..domain import AirQualityHour
from . import cache

AQ_API = "https://air-quality-api.open-meteo.com/v1/air-quality"


def _check_aq(payload):
    h = payload["hourly"]
    if len(h["time"]) != len(h["us_aqi"]):
        raise ValueError("air quality arrays differ in length")
    for t in h["time"]:
        datetime.fromisoformat(t)


def hours(payload):
    hourly = payload.get("hourly", {})
    return [AirQualityHour(datetime.fromisoformat(t), v) for t, v in zip(hourly.get("time", []), hourly.get("us_aqi", []))]


class OpenMeteoAir:
    name = "openmeteo"

    def __init__(self, cfg, get_text=None):
        self.cfg = cfg
        self.get_text = get_text or (lambda url: cache.get_text(url, cfg.user_agent))

    def _url(self):
        c = self.cfg
        q = {"latitude": c.latitude, "longitude": c.longitude, "timezone": c.timezone, "hourly": "us_aqi", "forecast_days": 3}
        return AQ_API + "?" + urllib.parse.urlencode(q)

    def air_quality(self, now):
        aq, _ = cache.cached(self.cfg, "air.json", lambda: json.loads(self.get_text(self._url())), now, ttl=60 * 60,
                             check=_check_aq)
        return hours(aq) if aq else None
