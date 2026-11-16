"""Hazard and warning-map adapter: Bureau of Meteorology warnings for NSW.

Warnings come from BOM's public RSS, filtered to the areas that cover home
(Sydney Metropolitan, Hawkesbury-Nepean, ...). The map of the most specific
warning in force comes from its detailed warning page, which embeds the
threat-area map as a base64 PNG (there is no separate image URL).
"""

import base64
import hashlib
import io
import re
import xml.etree.ElementTree as ET

from ..domain import MapImage
from ..hazards import Hazard
from . import cache
from .base import HAZARD_TTL, CachedHazardFeed

BOM_WARNINGS_NSW = "https://reg.bom.gov.au/fwo/IDZ00054.warnings_nsw.xml"
MAP_CREDIT = "Map: Bureau of Meteorology"
MAP_STALE_AFTER = 10 * 60  # a storm map older than this is history, not a forecast
# The detailed severe thunderstorm warning: its map shows the storm cells
# and their 30-minute track, so it is preferred over any other map.
DETAILED_STORM = "IDN21035"


# BOM warnings that pinpoint a threat (and usually come with a map) as
# opposed to region-wide notices.
SPECIFIC_BOM = ("severe thunderstorm warning", "severe weather warning", "flood warning")


def bom_hazards(rss_xml, areas, ignore):
    """BOM NSW warnings whose title names one of `areas` and none of `ignore`."""
    out = []
    root = ET.fromstring(rss_xml)
    for item in root.iter("item"):
        raw = " ".join((item.findtext("title") or "").split())
        # "10/04:40 EDT Severe Thunderstorm Warning for ..." -> drop the stamp
        title = re.sub(r"^\d{1,2}/\d{2}:\d{2}\s+\w+\s+", "", raw)
        low = title.lower()
        if not any(a.lower() in low for a in areas) or any(i.lower() in low for i in ignore):
            continue
        if low.startswith(("final", "cancellation")):
            continue
        parts = re.split(r"\s+for\s+", title, maxsplit=1)  # "Severe Thunderstorm Warning" | area
        name = parts[0].strip()
        area = re.sub(r"^(people in |the )", "", parts[1].strip()) if len(parts) > 1 else ""
        kind = "flood" if "flood" in low else "storm" if "thunderstorm" in low else "weather"
        level = "advice" if "watch" in name.lower() or "minor" in name.lower() else "watch"
        specific = any(s in name.lower() for s in SPECIFIC_BOM) and "watch" not in name.lower()
        link = (item.findtext("link") or "").strip() or None
        # The raw title carries the issue time, so a reissue is a new alert.
        out.append(Hazard(kind, level, name, None, "BOM", specific=specific, ident="BOM:" + raw,
                          url=link, detail=area, titled=True))
    return out


def page_map(html):
    """(png bytes, issued text) from a BOM warning page, or (None, issued).

    BOM's detailed warning pages embed their threat-area map as a base64 PNG
    right in the page; there is no separate image URL to fetch."""
    issued = None
    if re.search(r"\bcancel(?:lation|led)\b", re.sub(r"<[^>]+>", " ", html[:20000]), re.I):
        return None, None  # a cancellation's map shows nothing current
    m = re.search(r"Issued at ([^<]+?)\.\s*<", html)
    if m:
        issued = " ".join(m.group(1).split())
        t = re.match(r"(\d{1,2}:\d{2} ?[ap]m)", issued)  # "5:13 pm Friday, 13 November 2026" -> "5:13 pm"
        issued = t.group(1) if t else issued
    found = re.findall(r"""<img[^>]+src=["']data:image/png;base64,([A-Za-z0-9+/=\s]+)["']""", html)
    if not found:
        return None, issued
    # The map is by far the largest embedded image (logos and icons are tiny).
    data = base64.b64decode(re.sub(r"\s", "", max(found, key=len)))
    return (data if valid_map_png(data) else None), issued


MAX_MAP_PIXELS = 4_000_000  # BOM maps are ~700x750; refuse anything absurd


def valid_map_png(data):
    """A complete, sane PNG: checked before it is cached, so a truncated or
    hostile image can never reach the renderer."""
    from PIL import Image

    if not data or not data.startswith(b"\x89PNG"):
        return False
    try:
        img = Image.open(io.BytesIO(data))
        if img.width * img.height > MAX_MAP_PIXELS:
            return False
        img.verify()
        return True
    except Exception:
        return False


def check_rss(xml_text):
    if ET.fromstring(xml_text).find("channel") is None:
        raise ValueError("not an RSS document")


class BomWarnings(CachedHazardFeed):
    name = "BOM"
    cache_name = "bom-warnings.json"

    def fetch(self):
        return self.get_text(BOM_WARNINGS_NSW)

    def check(self, data):
        check_rss(data)

    def parse(self, data, now):
        return bom_hazards(data, self.cfg.bom_warning_areas, self.cfg.bom_warning_ignore)


def load_warning_map(cfg, hazards, now, get_text):
    """The map of the most specific BOM warning in force, if its page has one."""
    candidates = [h for h in hazards if h.source == "BOM" and h.specific and h.url]
    candidates.sort(key=lambda h: (0 if DETAILED_STORM in h.url else 1, h.rank))
    for h in candidates:
        # Keyed by this issue of the warning, not just its page: BOM reuses
        # product pages (IDN21035) across events, and an old storm track
        # must never sit under a new warning.
        key = hashlib.sha256((h.url + "|" + h.ident).encode()).hexdigest()[:12]

        def fetch(url=h.url):
            png, issued = page_map(get_text(url))
            return {"png": base64.b64encode(png).decode() if png else None, "issued": issued}

        data, _ = cache.cached(cfg, f"warnmap-{key}.json", fetch, now, ttl=HAZARD_TTL, stale_after=MAP_STALE_AFTER)
        if data and data.get("png"):
            return MapImage(png=base64.b64decode(data["png"]), title=h.where, area=h.detail,
                            issued=data.get("issued") or "", credit=MAP_CREDIT)
    return None


class BomWarningMaps:
    """The WarningMapProvider for BOM warning pages."""

    name = "bom"

    def __init__(self, cfg, get_text=None):
        self.cfg = cfg
        self.get_text = get_text or (lambda url: cache.get_text(url, cfg.user_agent))

    def warning_map(self, hazards, now):
        return load_warning_map(self.cfg, hazards, now, self.get_text)
