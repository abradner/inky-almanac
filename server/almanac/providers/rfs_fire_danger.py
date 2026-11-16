"""Fire danger adapter: NSW RFS fire danger ratings and Total Fire Bans.

The RFS's public feed gives each fire district a rating and a ban for
"today" and "tomorrow". The Australian Fire Danger Rating System's colours
(green, yellow, orange, red) happen to be inks this panel has.
"""

import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from ..domain import FireDanger
from . import cache

RFS_FEED = "https://www.rfs.nsw.gov.au/feeds/fdrToban.xml"
RATINGS = ("MODERATE", "HIGH", "EXTREME", "CATASTROPHIC")


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


def _check_rfs(xml_text):
    if not any(True for _ in ET.fromstring(xml_text).iter("District")):
        raise ValueError("RFS feed has no districts")


class RfsFireDanger:
    """The FireDangerProvider for one RFS district (cfg.fire_district; empty
    turns it off)."""

    name = "rfs"

    def __init__(self, cfg, get_text=None):
        self.cfg = cfg
        self.get_text = get_text or (lambda url: cache.get_text(url, cfg.user_agent))

    def fire_danger(self, day, now):
        cfg, out = self.cfg, FireDanger()
        if not cfg.fire_district:
            return out
        xml_text, fetched_at = cache.cached(cfg, "rfs.json", lambda: self.get_text(RFS_FEED), now, check=_check_rfs)
        if not xml_text:
            return out
        # The feed says "today" and "tomorrow" relative to when it was
        # fetched. After an offline night a cached copy's "today" is
        # yesterday, so map by date and show nothing rather than guess.
        # Until first_hour, treat a fetch as yesterday's: we don't know
        # when RFS rolls the feed over, and nobody looks before 06:00.
        fetched = datetime.fromtimestamp(fetched_at, ZoneInfo(cfg.timezone))
        feed_day = fetched.date() - timedelta(days=1 if fetched.hour < cfg.first_hour else 0)
        offset = (day - feed_day).days
        if offset in (0, 1):
            try:
                out.rating, out.ban = parse_rfs(xml_text, cfg.fire_district, tomorrow=offset == 1)
            except ET.ParseError as e:
                print(f"conditions: bad RFS feed ({e!r})")
        return out
