"""Safety conditions worth a glance: NSW fire danger and smoke.

- Fire danger rating and Total Fire Ban for the poster's day, from the NSW Rural
  Fire Service's public feed (one district, e.g. "Greater Sydney Region").
  The Australian Fire Danger Rating System's colours (green, yellow, orange,
  red) happen to be inks this panel has.
- Air quality: Open-Meteo's US AQI forecast. Shown only when it's unhealthy
  (smoke or haze days), so on a normal day the poster stays uncluttered.

Both are cached on disk and fail quietly: a badge is better missing than wrong,
so data older than STALE_AFTER is ignored rather than shown.
"""

import json
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

from . import bom, hazards as hz
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

RFS_FEED = "https://www.rfs.nsw.gov.au/feeds/fdrToban.xml"
AQ_API = "https://air-quality-api.open-meteo.com/v1/air-quality"
TTL = 30 * 60
STALE_AFTER = 12 * 3600
AQI_SHOW_FROM = 101  # "unhealthy for sensitive groups" and worse

RATINGS = ("MODERATE", "HIGH", "EXTREME", "CATASTROPHIC")


@dataclass
class Conditions:
    fire_rating: str = None  # one of RATINGS, or None (no rating / unknown)
    fire_ban: bool = False
    aqi: int = None  # worst daytime US AQI, only if >= AQI_SHOW_FROM
    hazards: list = field(default_factory=list)  # hazards.Hazard, most urgent first
    unavailable: list = field(default_factory=list)  # hazard sources with no usable data
    alert: str = ""  # identity of the specific hazards in force (hazards.alert_id)
    warning: dict = None  # {"png": bytes, "title": str, "issued": str} for a warning with a map


# ------------------------------------------------------------------ parsing


def parse_rfs(xml_text, district, tomorrow):
    """(rating, ban) for `district` from the RFS feed; (None, False) if absent."""
    root = ET.fromstring(xml_text)
    want = district.strip().lower()
    for d in root.iter("District"):
        fields = {c.tag: (c.text or "").strip() for c in d}
        if fields.get("Name", "").lower() != want:
            continue
        rating = fields.get("DangerLevelTomorrow" if tomorrow else "DangerLevelToday", "").upper()
        ban = fields.get("FireBanTomorrow" if tomorrow else "FireBanToday", "").lower() == "yes"
        return (rating if rating in RATINGS else None), ban
    return None, False


def worst_daytime_aqi(payload, day, first_hour=6, last_hour=22):
    hourly = payload.get("hourly", {})
    worst = None
    for t, v in zip(hourly.get("time", []), hourly.get("us_aqi", [])):
        ts = datetime.fromisoformat(t)
        if v is None or ts.date() != day or not first_hour <= ts.hour <= last_hour:
            continue
        worst = v if worst is None else max(worst, v)
    return None if worst is None else int(round(worst))


# ------------------------------------------------------------------ caching


def _cached(cfg, name, fetch, now, ttl=TTL, check=None, stale_after=STALE_AFTER):
    """Fetch through a disk cache; on failure use the cache if not too stale.

    Returns (data, fetched_at) or (None, None). `check(data)` must not raise
    for data to be cached: a 200 with junk must never replace a good cache.
    """
    path = cfg.cache_dir / name
    cached = None
    if path.exists():
        try:
            cached = json.loads(path.read_text())
        except ValueError:
            cached = None
    if cached and now - cached["fetched_at"] < ttl:
        return cached["data"], cached["fetched_at"]
    try:
        data = fetch()
        if check:
            check(data)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"fetched_at": now, "data": data}))
        tmp.replace(path)
        return data, now
    except Exception as e:
        print(f"conditions: {name} fetch failed ({e!r})")
        if cached and now - cached["fetched_at"] < stale_after:
            return cached["data"], cached["fetched_at"]
        return None, None


def offline(url):
    """get_text for the request path: never touch the network, use the cache
    the background refresher keeps warm (a slow feed must not make the
    frame's 60 s poll time out)."""
    raise OSError("request path is cache-only")


def _get_text(url, ua):
    req = urllib.request.Request(url, headers={"User-Agent": ua})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read().decode("utf-8")


def _check_rfs(xml_text):
    if not any(True for _ in ET.fromstring(xml_text).iter("District")):
        raise ValueError("RFS feed has no districts")


def _check_aq(payload):
    h = payload["hourly"]
    if len(h["time"]) != len(h["us_aqi"]):
        raise ValueError("air quality arrays differ in length")
    for t in h["time"]:
        datetime.fromisoformat(t)


def load(cfg, day, tomorrow, now=None, get_text=None, from_hour=None):
    """Conditions for poster day `day`. `from_hour` (today's view) ignores AQI
    hours already past, so morning smoke doesn't linger on the badge all day."""
    now = time.time() if now is None else now
    get_text = get_text or (lambda url: _get_text(url, cfg.user_agent))
    tz = ZoneInfo(cfg.timezone)
    c = Conditions()

    if cfg.fire_district:
        xml_text, fetched_at = _cached(cfg, "rfs.json", lambda: get_text(RFS_FEED), now, check=_check_rfs)
        if xml_text:
            # The feed says "today" and "tomorrow" relative to when it was
            # fetched. After an offline night a cached copy's "today" is
            # yesterday, so map by date and show nothing rather than guess.
            # Until first_hour, treat a fetch as yesterday's: we don't know
            # when RFS rolls the feed over, and nobody looks before 06:00.
            fetched = datetime.fromtimestamp(fetched_at, tz)
            feed_day = fetched.date() - timedelta(days=1 if fetched.hour < cfg.first_hour else 0)
            offset = (day - feed_day).days
            if offset in (0, 1):
                try:
                    c.fire_rating, c.fire_ban = parse_rfs(xml_text, cfg.fire_district, tomorrow=offset == 1)
                except ET.ParseError as e:
                    print(f"conditions: bad RFS feed ({e!r})")

    q = urllib.parse.urlencode(
        {
            "latitude": cfg.latitude,
            "longitude": cfg.longitude,
            "timezone": cfg.timezone,
            "hourly": "us_aqi",
            "forecast_days": 3,
        }
    )
    c.hazards, c.unavailable = load_hazards(cfg, now, get_text)
    c.alert = hz.alert_id(c.hazards)
    c.warning = load_warning_map(cfg, c.hazards, now, get_text)

    aq, _ = _cached(cfg, "air.json", lambda: json.loads(get_text(AQ_API + "?" + q)), now, ttl=60 * 60, check=_check_aq)
    if aq:
        first = cfg.first_hour if from_hour is None else max(cfg.first_hour, from_hour)
        worst = worst_daytime_aqi(aq, day, first, cfg.last_hour)
        if worst is not None and worst >= AQI_SHOW_FROM:
            c.aqi = worst
    return c


HAZARD_TTL = 90  # refreshed every 2 min in the background; requests read the cache
HAZARD_STALE_AFTER = 90 * 60  # an old "Advice" may have become an Emergency, or been cleared


def load_hazards(cfg, now, get_text):
    """(hazards near home, names of sources with no usable data).

    RFS fires, SES warnings and BOM warnings. Each source fails on its own,
    in fetching and in parsing: one broken feed never hides the others. Data
    older than HAZARD_STALE_AFTER counts as unavailable, so the poster can
    say "no live hazard data" instead of looking like a quiet day."""
    found, missing = [], []
    lat, lon, r = cfg.latitude, cfg.longitude, cfg.hazard_radius_km
    now_utc = datetime.fromtimestamp(now, timezone.utc)
    sources = (
        ("RFS", "rfs-incidents.json", lambda: json.loads(get_text(hz.RFS_INCIDENTS)), hz.check_geojson,
         lambda d: hz.rfs_hazards(d, lat, lon, r)),
        ("SES", "hazardwatch.json", lambda: hz.hazardwatch_data(get_text(hz.HAZARDWATCH)), None,
         lambda d: hz.ses_hazards(d, lat, lon, r, now_utc=now_utc)),
        ("BOM", "bom-warnings.json", lambda: get_text(hz.BOM_WARNINGS_NSW), hz.check_rss,
         lambda d: hz.bom_hazards(d, cfg.bom_warning_areas, cfg.bom_warning_ignore)),
    )
    for name, cache, fetch, check, parse in sources:
        data, _ = _cached(cfg, cache, fetch, now, ttl=HAZARD_TTL, check=check, stale_after=HAZARD_STALE_AFTER)
        if data is None:
            missing.append(name)
            continue
        try:
            found += parse(data)
        except Exception as e:
            print(f"conditions: {name} hazards unusable ({e!r})")
            missing.append(name)
    return hz.sort_and_dedupe(found), missing


def load_warning_map(cfg, hazards, now, get_text):
    """The map of the most specific BOM warning in force, if its page has one.

    The detailed severe thunderstorm warning (IDN21035) is preferred: its map
    shows the storm cells and their 30-minute track."""
    import base64
    import hashlib

    candidates = [h for h in hazards if h.source == "BOM" and h.specific and h.url]
    candidates.sort(key=lambda h: (0 if "IDN21035" in h.url else 1, h.rank))
    for h in candidates:
        key = hashlib.sha256(h.url.encode()).hexdigest()[:12]

        def fetch(url=h.url):
            png, issued = hz.page_map(get_text(url))
            return {"png": base64.b64encode(png).decode() if png else None, "issued": issued}

        data, _ = _cached(cfg, f"warnmap-{key}.json", fetch, now, ttl=HAZARD_TTL, stale_after=HAZARD_STALE_AFTER)
        if data and data.get("png"):
            return {"png": base64.b64decode(data["png"]), "title": h.where, "issued": data.get("issued") or ""}
    return None


def load_bom_days(cfg, now=None, get_text=None):
    """{date: bom.BomDay} for cfg.bom_town, or {} if BOM can't be reached."""
    now = time.time() if now is None else now
    get_text = get_text or (lambda url: bom.fetch(cfg.user_agent))
    xml_text, _ = _cached(cfg, "bom-precis.json", lambda: get_text(bom.PRECIS_NSW), now, ttl=30 * 60,
                          check=lambda x: bom.parse(x, cfg.bom_town))
    if not xml_text:
        return {}
    try:
        days = bom.parse(xml_text, cfg.bom_town)
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


def aqi_label(aqi):
    if aqi >= 301:
        return "hazardous"
    if aqi >= 201:
        return "very unhealthy"
    if aqi >= 151:
        return "unhealthy"
    return "poor"
