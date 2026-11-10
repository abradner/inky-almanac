"""Landscape photo of the day from Wikimedia Commons' Featured Pictures.

Featured Pictures are Commons' best-of: sharp, well-exposed, freely licensed.
The pool is drawn from a handful of nature categories (mountains, glaciers,
forests, lakes, ...). Each candidate is also screened for colour before use:
the panel has seven muted inks, so misty, grey or night scenes come out as mud,
while a saturated, contrasty alpine scene comes out well.

The day's pick is a stable hash of the date, so every render on one day shows
the same photo; candidates that fail the screen are remembered and skipped.

Wikimedia rate-limits anonymous API clients hard and asks for a descriptive
User-Agent, so the pool is fetched in batches, slowly, once a fortnight.
"""

import hashlib
import html
import io
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from PIL import Image, ImageStat

API = "https://commons.wikimedia.org/w/api.php"
POOL_TTL = 14 * 86400
KEEP_IMAGES = 40
THUMB_WIDTH = 960  # one of Commons' standard thumbnail steps
MAX_TRIES = 8

CATEGORIES = [
    "Featured pictures of mountains",
    "Featured pictures of the Alps",
    "Featured pictures of the Himalayas",
    "Featured pictures of Glacier National Park",
    "Featured pictures of Glacier Bay National Park",
    "Featured pictures of Denali National Park",
    "Featured pictures of Kenai Fjords National Park",
    "Featured pictures of North Cascades National Park",
    "Featured pictures of Lake Clark National Park",
    "Featured pictures of Yosemite National Park",
    "Featured pictures of Olympic National Park",
    "Featured pictures of Crater Lake National Park",
    "Featured pictures of forests",
    "Featured pictures of jungles",
    "Featured pictures of lakes",
    "Featured pictures of waterfalls",
    "Featured pictures of volcanoes",
    "Featured pictures of landscapes",
]


@dataclass
class Photo:
    id: str
    title: str
    credit: str
    image: Image.Image


# ------------------------------------------------------------------ network


def _api(params, ua, opener=None):
    q = dict(params, format="json", formatversion=2, maxlag=5)
    req = urllib.request.Request(API + "?" + urllib.parse.urlencode(q), headers={"User-Agent": ua})
    with (opener or urllib.request.urlopen)(req, timeout=30) as r:
        data = json.load(r)
    if "error" in data:  # e.g. maxlag: don't mistake it for an empty category
        raise RuntimeError(f"Commons API error: {data['error'].get('code')}")
    return data


def _get(url, ua):
    req = urllib.request.Request(url, headers={"User-Agent": ua})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


# ------------------------------------------------------------------ metadata


def _strip_html(s):
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", s or "")).split())


def nice_title(file_title, object_name=""):
    name = _strip_html(object_name)
    if not name or name.lower().startswith("file:"):
        name = file_title
    name = re.sub(r"^File:", "", name)
    name = re.sub(r"\.(jpe?g|png|tiff?)$", "", name, flags=re.I)
    name = name.replace("_", " ")
    # Camera-roll style names ("IMG 1234", "DSC01234") say nothing.
    if re.fullmatch(r"(IMG|DSC|DSCF|P)[ _-]?\d+.*", name, flags=re.I):
        return "Untitled"
    return " ".join(name.split())


def usable(info):
    """A landscape-ish JPEG big enough to look good."""
    if info.get("mime") != "image/jpeg":
        return False
    w, h = info.get("width") or 0, info.get("height") or 0
    if w < 1200 or h < 700:
        return False
    return 0.8 <= w / h <= 1.7  # wider would be letterboxed rather than cropped


def fetch_pool(ua, api=None, pause=1.0):
    api = api or (lambda p: _api(p, ua))
    titles = []
    for cat in CATEGORIES:
        params = {"action": "query", "list": "categorymembers", "cmtitle": "Category:" + cat, "cmtype": "file", "cmlimit": 500}
        while True:
            data = api(params)
            titles += [m["title"] for m in data.get("query", {}).get("categorymembers", [])]
            cont = data.get("continue")
            time.sleep(pause)
            if not cont:
                break
            params = dict(params, **cont)
    titles = list(dict.fromkeys(titles))  # de-duplicate, keep order
    pool = []
    for i in range(0, len(titles), 50):
        batch = titles[i : i + 50]
        data = api(
            {
                "action": "query",
                "prop": "imageinfo",
                "titles": "|".join(batch),
                "iiprop": "url|size|mime|extmetadata",
                "iiurlwidth": THUMB_WIDTH,
                "iiextmetadatafilter": "Artist|LicenseShortName|ObjectName",
            }
        )
        for page in data.get("query", {}).get("pages", []):
            ii = (page.get("imageinfo") or [None])[0]
            if not ii or not usable(ii):
                continue
            meta = ii.get("extmetadata", {})
            artist = _strip_html(meta.get("Artist", {}).get("value", "")) or "Unknown"
            licence = _strip_html(meta.get("LicenseShortName", {}).get("value", ""))
            pool.append(
                {
                    "id": str(page["pageid"]),
                    "title": nice_title(page["title"], meta.get("ObjectName", {}).get("value", "")),
                    "credit": f"Photo: {artist}" + (f" · {licence}" if licence else ""),
                    "thumb": ii["thumburl"],
                }
            )
        time.sleep(pause)
    return pool


_crawl_lock = threading.Lock()


def load_pool(cfg, now=None, fetcher=None, refresh=False):
    """The cached pool. Requests (refresh=False) accept a stale pool and crawl
    only when there is none at all; the background thread passes refresh=True
    and re-crawls once the pool is older than POOL_TTL - 12 h."""
    now = time.time() if now is None else now
    path = cfg.cache_dir / "photo-pool.json"

    def read():
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError):
            return None

    def good_enough(c):
        return c and c["pool"] and (not refresh or now - c["fetched_at"] < POOL_TTL - 12 * 3600)

    cached = read()
    if good_enough(cached):
        return cached["pool"]
    with _crawl_lock:  # one crawl at a time (thread and a first-boot request)
        cached = read()  # someone may have crawled while we waited
        if good_enough(cached):
            return cached["pool"]
        try:
            pool = (fetcher or (lambda: fetch_pool(cfg.user_agent)))()
            if pool:
                tmp = path.with_suffix(".tmp")
                tmp.write_text(json.dumps({"fetched_at": now, "pool": pool}))
                tmp.replace(path)
                return pool
        except Exception as e:
            print(f"photo: pool fetch failed ({e!r})")
    return cached["pool"] if cached else []


# ------------------------------------------------------------------ choosing


def order(pool, day, shuffle=0):
    """Pool entries in this day's preference order (stable per day+shuffle)."""

    def rank(e):
        return hashlib.sha256(f"{day.isoformat()}/{shuffle}/{e['id']}".encode()).digest()

    return sorted(pool, key=rank)


def colourful_enough(img):
    """Screen out scenes the inks can't render: grey, misty, dark or flat.

    Thresholds are on a 0-255 scale: mean saturation, mean brightness and the
    spread of brightness (contrast).
    """
    small = img.copy()
    small.thumbnail((200, 200))
    h, s, v = small.convert("HSV").split()
    sat = ImageStat.Stat(s).mean[0]
    val = ImageStat.Stat(v)
    return sat >= 70 and 70 <= val.mean[0] <= 215 and val.stddev[0] >= 40


def _rejects_path(cfg):
    return cfg.cache_dir / "photo-rejects.json"


def _load_rejects(cfg):
    try:
        return set(json.loads(_rejects_path(cfg).read_text()))
    except (OSError, ValueError):
        return set()


def _prune(folder):
    files = sorted(folder.glob("*.jpg"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in files[KEEP_IMAGES:]:
        old.unlink(missing_ok=True)


def load_image(cfg, entry, get=_get):
    folder = cfg.cache_dir / "photos"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{entry['id']}.jpg"
    if not path.exists():
        data = get(entry["thumb"], cfg.user_agent)
        Image.open(io.BytesIO(data)).verify()  # refuse to cache a broken download
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)
        _prune(folder)
    path.touch()
    return Image.open(path).convert("RGB")


def photo_for(cfg, day, shuffle=0, fetcher=None, get=_get, screen=colourful_enough):
    """The day's photo; on network trouble the most recently cached one; else None."""
    pool = load_pool(cfg, fetcher=fetcher)
    rejects = _load_rejects(cfg) & {e["id"] for e in pool}  # forget ids no longer pooled
    tries = 0
    for entry in order([e for e in pool if e["id"] not in rejects], day, shuffle):
        if tries >= MAX_TRIES:
            break
        tries += 1
        try:
            img = load_image(cfg, entry, get)
        except urllib.error.HTTPError as e:
            if e.code in (404, 410):  # deleted on Commons: skip it for good (not 403/429)
                print(f"photo: {entry['id']} gone ({e.code})")
                rejects.add(entry["id"])
                _rejects_path(cfg).write_text(json.dumps(sorted(rejects)))
                continue
            print(f"photo: download failed ({e!r})")
            break
        except Exception as e:
            print(f"photo: download failed ({e!r})")
            break  # probably offline; don't burn through the pool
        if screen(img):
            return Photo(entry["id"], entry["title"], entry["credit"], img)
        rejects.add(entry["id"])
        _rejects_path(cfg).write_text(json.dumps(sorted(rejects)))
        (cfg.cache_dir / "photos" / f"{entry['id']}.jpg").unlink(missing_ok=True)
    # Fall back to whatever we showed last.
    folder = cfg.cache_dir / "photos"
    cached = sorted(folder.glob("*.jpg"), key=lambda p: p.stat().st_mtime, reverse=True) if folder.exists() else []
    by_id = {e["id"]: e for e in pool}
    for path in cached:
        e = by_id.get(path.stem)
        if e:
            try:
                return Photo(e["id"], e["title"], e["credit"], Image.open(path).convert("RGB"))
            except Exception:
                continue
    return None
