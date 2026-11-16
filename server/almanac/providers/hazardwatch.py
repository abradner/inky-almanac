"""Hazard adapter: NSW SES warnings (floods, storms, tsunami) from HazardWatch.

HazardWatch is the platform behind Hazards Near Me. SES offers no open API,
so this reads the alerts HazardWatch embeds in its own page (CAP-AU alerts
plus their polygons). It is the fragile one: if the page changes shape it
yields nothing, quietly. RFS alerts are skipped here because the RFS feed
already covers them.
"""

import json
import re
from datetime import datetime, timezone

from ..hazards import AWS_LEVELS, Hazard, distance_km
from .base import CachedHazardFeed

HAZARDWATCH = "https://www.hazardwatch.gov.au/"


def hazardwatch_data(html):
    m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html, re.S)
    if not m:
        raise ValueError("HazardWatch page has no __NEXT_DATA__")
    data = json.loads(m.group(1))["props"]["pageProps"]["alertData"]
    if not isinstance(data.get("alerts"), list):
        raise ValueError("HazardWatch alertData has no alerts list")
    return data


_KIND = (("flood", "flood"), ("storm", "storm"), ("tsunami", "tsunami"), ("wind", "storm"), ("fire", "fire"))


def _kind(event):
    e = (event or "").lower()
    for word, kind in _KIND:
        if word in e:
            return kind
    return "hazard"


def _expired(info, now_utc):
    exp = info.get("expires")
    if not exp or now_utc is None:
        return False
    try:
        return datetime.fromisoformat(exp.replace("Z", "+00:00")) <= now_utc
    except ValueError:
        return False


def ses_hazards(alert_data, lat, lon, radius_km, skip_agencies=("NSWRFS",), now_utc=None):
    """SES (and other non-RFS) alerts near home that haven't expired. RFS
    alerts are skipped here because the RFS feed already covers them."""
    geo = {}
    for f in (alert_data.get("geojson") or {}).get("features") or []:
        ident = ((f or {}).get("properties") or {}).get("alertIdentifier")
        if ident:
            geo[ident] = f.get("geometry")
    out = []
    for a in alert_data.get("alerts") or []:
        try:  # one malformed alert must not hide the rest
            if a.get("agencyCode") in skip_agencies or a.get("status") != "Actual":
                continue
            info = a.get("info") or {}
            if _expired(info, now_utc):
                continue
            param = info.get("parameter") or {}
            level = AWS_LEVELS.get((param.get("AustralianWarningSystem:WarningLevel") or "").strip().lower())
            if not level:
                continue
            km = distance_km(geo.get(a.get("identifier")), lat, lon)
            if km is None or km > radius_km:
                continue
            where = param.get("AffectedLocation") or " ".join((info.get("headline") or "").split()) or "Unnamed"
            out.append(Hazard(_kind(info.get("event")), level, where.strip(), round(km), a.get("agencyCode") or "SES",
                              specific=level != "advice",
                              ident="SES:%s:%s" % (a.get("identifier"), level)))
        except Exception as e:
            print(f"hazards: skipped an SES alert ({e!r})")
    return out


class HazardWatch(CachedHazardFeed):
    name = "SES"
    cache_name = "hazardwatch.json"

    def fetch(self):
        return hazardwatch_data(self.get_text(HAZARDWATCH))

    def parse(self, data, now):
        c = self.cfg
        now_utc = datetime.fromtimestamp(now, timezone.utc)
        return ses_hazards(data, c.latitude, c.longitude, c.hazard_radius_km, now_utc=now_utc)
