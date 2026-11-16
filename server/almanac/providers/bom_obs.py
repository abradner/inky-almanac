"""Observations adapter: Bureau of Meteorology 10-minute station observations.

Reads a state's observations product from BOM's anonymous FTP service
(IDN60920 is New South Wales: every station, latest observation only,
reissued about every 10 minutes) and keeps a small rolling log of each
configured station's successive readings, as domain.Reading, in the cache
volume. Business logic (observed.py) differences the log for "rain in the
last hour" and looks through it for a southerly change.

Elements are read by their `type` attribute, never by position, and any of
them may be missing (wind-only stations such as Fort Denison have no
temperature or rain). The anonymous feeds are for personal, non-commercial
use; see the README.

Polling: `refresh` fetches at most every POLL_S (the product's own
cadence), backs off on failure, and is only ever called by the background
refresher; requests read the log on disk (`logs`), so a slow FTP server can
never hold up a render.
"""

import json
import threading
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime

from ..domain import Reading, StationLogs

URL = "ftp://ftp.bom.gov.au/anon/gen/fwo/{product}.xml"
SOURCE = "BOM"
POLL_S = 10 * 60  # the product is reissued about every 10 minutes
BACKOFF_MAX_S = 60 * 60
KEEP_S = 3 * 3600  # enough log for an hour's difference and the 9am reset
LOG_NAME = "bom-obs-log.json"
TIMEOUT_S = 60

# 16-point compass -> degrees, for a report with wind_dir but no wind_dir_deg.
_COMPASS = {
    p: i * 22.5
    for i, p in enumerate("N NNE NE ENE E ESE SE SSE S SSW SW WSW W WNW NW NNW".split())
}


def _num(el):
    if el is None or el.text is None:
        return None
    try:
        return float(el.text.strip())
    except ValueError:
        return None


def _when(el, attr):
    v = None if el is None else el.get(attr)
    try:
        return datetime.fromisoformat(v) if v else None
    except ValueError:
        return None


def parse(xml_text, stations=None):
    """{wmo id: Reading} for `stations` (all stations if None)."""
    root = ET.fromstring(xml_text)
    out = {}
    for st in root.iter("station"):
        wmo = st.get("wmo-id")
        if not wmo or (stations is not None and wmo not in stations):
            continue
        period = st.find("period")
        at = _when(period, "time-local")
        if at is None or at.tzinfo is None:
            continue
        el = {}
        for e in period.iter("element"):
            el.setdefault(e.get("type"), e)
        name = (st.get("description") or st.get("stn-name") or wmo).strip()
        credit = name if st.get("type") != "AWS" or name.endswith("AWS") else f"{name} AWS"
        wind_dir = _num(el.get("wind_dir_deg"))
        if wind_dir is None and el.get("wind_dir") is not None:
            wind_dir = _COMPASS.get((el["wind_dir"].text or "").strip().upper())
        rain, prev = el.get("rainfall"), el.get("rainfall_24hr")
        out[wmo] = Reading(
            station=wmo,
            name=name,
            credit=credit,
            at=at,
            temp=_num(el.get("air_temperature")),
            apparent=_num(el.get("apparent_temp")),
            dew_point=_num(el.get("dew_point")),
            humidity=_num(el.get("rel-humidity")),
            wind_dir=wind_dir,
            wind=_num(el.get("wind_spd_kmh")),
            gust=_num(el.get("gust_kmh")),
            max_gust=_num(el.get("maximum_gust_kmh")),
            rain=_num(rain),
            rain_start=_when(rain, "start-time-local"),
            rain_prev_period=_num(prev),
            rain_prev_end=_when(prev, "end-time-local"),
        )
    return out


# ------------------------------------------------------------------ the log on disk

_TIMES = ("at", "rain_start", "rain_prev_end")


def _to_json(r):
    d = dict(r.__dict__)
    for k in _TIMES:
        d[k] = d[k].isoformat() if d[k] else None
    return d


def _from_json(d):
    d = dict(d)
    for k in _TIMES:
        d[k] = datetime.fromisoformat(d[k]) if d.get(k) else None
    return Reading(**d)


def merge(logs, readings, now_ts, keep_s=KEEP_S):
    """Append each new reading (one newer than the station's last) and drop
    readings older than keep_s. `logs` is {wmo: [Reading]}, oldest first."""
    out = {k: list(v) for k, v in logs.items()}
    for wmo, r in readings.items():
        log = out.setdefault(wmo, [])
        if not log or r.at > log[-1].at:
            log.append(r)
    for wmo in list(out):
        out[wmo] = [r for r in out[wmo] if now_ts - r.at.timestamp() <= keep_s]
        if not out[wmo]:
            del out[wmo]
    return out


class BomObservations:
    """The ObservationsProvider for BOM's state observation products."""

    name = "bom"

    def __init__(self, cfg, get_text=None, clock=time.time):
        self.cfg = cfg
        self.home = cfg.obs_station
        self.crosscheck = tuple(cfg.obs_crosscheck)
        self.leading = tuple(cfg.obs_leading)
        self.stations = {self.home, *self.crosscheck, *self.leading}
        self.url = URL.format(product=cfg.obs_product)
        self._get_text = get_text or self._fetch
        self._clock = clock
        self._lock = threading.Lock()
        self._next_try = 0.0
        self._failures = 0

    def _fetch(self, url):
        # A plain anonymous FTP RETR (FTP carries no User-Agent; ftplib's
        # anonymous password is its generic default, nothing personal).
        req = urllib.request.Request(url, headers={"User-Agent": self.cfg.user_agent})
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
            return r.read().decode("utf-8")

    @property
    def path(self):
        return self.cfg.cache_dir / LOG_NAME

    def _read(self):
        try:
            raw = json.loads(self.path.read_text())
            return raw.get("fetched_at", 0), {k: [_from_json(d) for d in v] for k, v in raw["logs"].items()}
        except (OSError, ValueError, KeyError, TypeError) as e:
            if not isinstance(e, FileNotFoundError):
                print(f"observations: log unreadable ({e!r}); starting afresh")
            return 0, {}

    def refresh(self, now=None):
        """Fetch the product if POLL_S has passed since the last success and
        no backoff is pending. True if new data was stored."""
        now = self._clock() if now is None else now
        with self._lock:
            fetched_at, logs = self._read()
            if now < self._next_try or now - fetched_at < POLL_S:
                return False
            try:
                readings = parse(self._get_text(self.url), self.stations)
                if not readings:
                    raise ValueError("none of the configured stations is in the product")
            except Exception as e:
                self._failures += 1
                wait = min(POLL_S * 2 ** self._failures, BACKOFF_MAX_S)
                self._next_try = now + wait
                print(f"observations: fetch failed ({e!r}); next try in {wait // 60:.0f} min")
                return False
            self._failures = 0
            self._next_try = now + POLL_S
            logs = merge(logs, readings, now)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"fetched_at": now, "logs": {k: [_to_json(r) for r in v] for k, v in logs.items()}}))
            tmp.replace(self.path)
            return True

    def logs(self, now=None):
        """The stored logs as domain.StationLogs, or None before the first
        successful fetch. Never touches the network."""
        _, logs = self._read()
        if not logs:
            return None
        return StationLogs(logs=logs, home=self.home, crosscheck=self.crosscheck, leading=self.leading, source=SOURCE)
