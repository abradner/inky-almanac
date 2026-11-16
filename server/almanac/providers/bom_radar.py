"""Rain radar adapter: Bureau of Meteorology radar frames over anonymous FTP.

BOM publishes each radar's frames as `IDR<site><range>.T.<yyyymmddHHMM>.png`
(the stamp is UTC) in ftp://ftp.bom.gov.au/anon/gen/radar/, one about every
5 minutes, about 25 kept. A frame is a 512x512 palette PNG holding only the
rain (index 0 is transparent), plus a grey copyright bar along the top and
a line of black text along the bottom. The map underneath comes separately,
as transparent layers in /anon/gen/radar_transparencies/
(`IDR714.background.png`, `.topography.png`, `.range.png`,
`.locations.png`, ...), so the rain never has to be subtracted from a
picture. Verified on 2026-11-16 against IDR714 and IDR713.

Rain colours (BOM's rain-rate scale, light to extreme) are matched by RGB,
not by palette index: indices differ from frame to frame. Levels 1-13 were
read from recorded frames' palettes; 14 and 15 are the two darkest reds of
BOM's published legend and were not seen in a recorded frame.

Geometry: the radar site is at the image centre (256, 256) and a pixel is
0.25 km at the 64 km range (IDR714: range rings at 20, 40 and 60 km sit at
80, 160 and 240 px; Penrith and Sydney Airport's location dots land within
3 px (under a kilometre) of where Terrey Hills' published position puts them). The 128 km range
(IDR713) has rings at 100 and 200 px, consistent with 0.5 km/px (50 and
100 km rings).

Politeness: the directory listing is ~1 MB, so this asks the server for
`IDR714.T.*` only (the server globs NLST), and only once the next frame is
due (the newest stamp plus the cadence the listing itself shows, plus a
minute for BOM to publish it). The layers are static; they are fetched
once and re-checked weekly. Anonymous login with ftplib's generic default
password; no personal details. BOM's anonymous products are for personal,
non-commercial use.
"""

import colorsys
import io
import json
import re
import threading
import time
from datetime import datetime, timezone

from ..domain import BASE_COAST, BASE_LABEL, BASE_RING, BASE_TOPO, RAIN_LEVELS, RadarImage
from .cache import CacheOnly

FTP_HOST = "ftp.bom.gov.au"
RADAR_DIR = "/anon/gen/radar"
LAYERS_DIR = "/anon/gen/radar_transparencies"
CREDIT = "Bureau of Meteorology"

# Radar sites by BOM's two-digit id: (name, latitude, longitude). The site
# sits at the image centre.
SITES = {"71": ("Terrey Hills", -33.701, 151.210)}
# The product's last digit: (km per pixel, range in km).
RANGES = {"4": (0.25, 64), "3": (0.5, 128)}

# BOM's rain-rate scale, light to extreme: level = position + 1.
RAIN_SCALE = (
    (245, 245, 255), (180, 180, 255), (120, 120, 255), (20, 20, 255),
    (0, 216, 195), (0, 150, 144), (0, 102, 102),
    (255, 255, 0), (255, 200, 0), (255, 150, 0), (255, 100, 0),
    (255, 0, 0), (200, 0, 0), (120, 0, 0), (40, 0, 0),
)
assert len(RAIN_SCALE) == RAIN_LEVELS
RAIN_MATCH = 6 ** 2 * 3  # squared RGB distance still counted as a scale colour (white is 200 from level 1)

LAYERS = ("background", "topography", "range", "locations")
LAYERS_TTL_S = 7 * 86400
DEFAULT_CADENCE_S = 5 * 60
CADENCE_RANGE_S = (2 * 60, 15 * 60)  # what a listing's own spacing may say
PUBLISH_LAG_S = 60  # a frame appears about a minute after its stamp
LIST_GAP_S = 60  # never list more often than this, even when a frame is late
RETRY_S, RETRY_MAX_S = 2 * 60, 15 * 60
MAX_PNG_BYTES = 300_000  # frames are ~15 KB, layers under 20 KB
SIZE = 512
STAMP = re.compile(r"^(IDR\d{3})\.T\.(\d{12})\.png$")


def parse_stamp(name, product):
    """Unix time of a frame file name's UTC stamp, or None if it isn't one of
    `product`'s frames."""
    m = STAMP.match(name.rsplit("/", 1)[-1])
    if not m or m.group(1) != product:
        return None
    try:
        return datetime.strptime(m.group(2), "%Y%m%d%H%M").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def cadence(stamps):
    """Seconds between frames, from the listing's own spacing (the median of
    the last few gaps), clamped to something sane."""
    s = sorted(set(stamps))[-6:]
    gaps = sorted(b - a for a, b in zip(s, s[1:]))
    if not gaps:
        return DEFAULT_CADENCE_S
    lo, hi = CADENCE_RANGE_S
    return max(lo, min(hi, gaps[len(gaps) // 2]))


def _png(data):
    from PIL import Image

    if not data or not data.startswith(b"\x89PNG") or len(data) > MAX_PNG_BYTES:
        raise ValueError("not a radar PNG")
    img = Image.open(io.BytesIO(data))
    if img.size != (SIZE, SIZE) or img.mode != "P":
        raise ValueError(f"unexpected radar image {img.size} {img.mode}")
    img.load()
    return img


def _alpha(img):
    """Per palette index: True where the index is fully transparent."""
    t = img.info.get("transparency")
    if isinstance(t, int):
        return lambda i: i == t
    if isinstance(t, (bytes, bytearray)):
        return lambda i: i < len(t) and t[i] == 0
    return lambda i: False


def _colours(img):
    pal = img.getpalette() or []
    return [tuple(pal[i * 3:i * 3 + 3]) if len(pal) >= i * 3 + 3 else (0, 0, 0) for i in range(256)]


def rain_level(rgb):
    """BOM rain colour -> level 1..15, or 0 for anything else (the copyright
    bar, the caption text, a stray colour)."""
    best, level = None, 0
    for i, c in enumerate(RAIN_SCALE):
        d = sum((a - b) ** 2 for a, b in zip(rgb, c))
        if best is None or d < best:
            best, level = d, i + 1
    return level if best <= RAIN_MATCH else 0


def rain_levels(data):
    """A frame's PNG bytes -> one rain level byte per pixel."""
    img = _png(data)
    transparent, colours = _alpha(img), _colours(img)
    table = bytes(0 if transparent(i) else rain_level(colours[i]) for i in range(256))
    return img.tobytes().translate(table)


def _hsv(rgb):
    return colorsys.rgb_to_hsv(*(c / 255 for c in rgb))


def _layer_table(name, img):
    """Palette index -> BASE_* class (0 for nothing) for one background layer."""
    transparent, colours = _alpha(img), _colours(img)
    used = sorted(set(img.tobytes()))
    table = [0] * 256
    if name == "topography":
        # Elevation bands, lightest (lowest) first; white is the empty sea.
        bands = sorted((i for i in used if not transparent(i) and colours[i] != (255, 255, 255)),
                       key=lambda i: -sum(colours[i]))
        for n, i in enumerate(bands):
            table[i] = BASE_TOPO + n
        return bytes(table)
    for i in used:
        if transparent(i):
            continue
        h, s, v = _hsv(colours[i])
        if name == "background":
            # Land and sea are pale fills; the coastline is the saturated brown.
            table[i] = BASE_COAST if s >= 0.5 else 0
        elif name == "range":
            table[i] = BASE_RING if colours[i] != (255, 255, 255) else 0
        elif name == "locations":
            # Black place names and town dots, and the coastline again in
            # the same brown as the background's; its sea fill is transparent.
            table[i] = BASE_LABEL if v < 0.3 else BASE_COAST if s >= 0.5 else 0
    return bytes(table)


def base_classes(layers):
    """{layer name: PNG bytes} -> one BASE_* byte per pixel. Later layers win:
    topography, then coast, range rings, place names. A missing or broken
    layer is skipped."""
    from PIL import Image

    base = Image.new("L", (SIZE, SIZE), 0)
    for name in LAYERS:
        data = layers.get(name)
        if not data:
            continue
        try:
            img = _png(data)
        except Exception as e:
            print(f"radar: layer {name} unusable ({e!r})", flush=True)
            continue
        classes = img.tobytes().translate(_layer_table(name, img))
        plane = Image.frombytes("L", (SIZE, SIZE), classes)
        mask = Image.frombytes("L", (SIZE, SIZE), classes.translate(bytes([0] + [255] * 255)))
        base.paste(plane, (0, 0), mask)
    return base.tobytes()


class FtpTransport:
    """BOM's anonymous FTP: list a directory by glob, fetch a file."""

    def __init__(self, host=FTP_HOST, timeout=20):
        self.host, self.timeout = host, timeout

    def _session(self):
        import ftplib

        ftp = ftplib.FTP(self.host, timeout=self.timeout)
        ftp.login()  # anonymous, ftplib's generic password
        return ftp

    def list(self, directory, pattern):
        ftp = self._session()
        try:
            ftp.cwd(directory)
            try:
                return ftp.nlst(pattern)
            except Exception as e:  # "550 No files found" when nothing matches
                if "550" in str(e):
                    return []
                raise
        finally:
            ftp.close()

    def get(self, directory, name, limit=MAX_PNG_BYTES):
        ftp = self._session()
        buf = bytearray()

        def take(chunk):
            buf.extend(chunk)
            if len(buf) > limit:
                raise ValueError(f"{name} larger than {limit} bytes")

        try:
            ftp.cwd(directory)
            ftp.retrbinary("RETR " + name, take)
            return bytes(buf)
        finally:
            ftp.close()


class _Offline:
    def list(self, directory, pattern):
        raise CacheOnly("request path is cache-only")

    get = list


class BomRadar:
    """The RadarProvider for BOM's radar frames. `refresh` (background,
    radar mode only) fetches the newest frame once it is due; `latest` reads
    what refresh stored under the cache volume."""

    name = "bom"

    def __init__(self, cfg, get_text=None, transport=None):
        self.cfg = cfg
        self.product = cfg.radar_product
        if not re.fullmatch(r"IDR\d{3}", self.product or "") or self.product[3:5] not in SITES \
                or self.product[5] not in RANGES:
            raise ValueError(f"unsupported radar product {self.product!r} (known sites: {', '.join(SITES)})")
        # get_text is the convention for "which transport": a caller passing
        # one (the request path's cache.offline) wants no network at all.
        self.transport = transport or (_Offline() if get_text is not None else FtpTransport())
        self.dir = cfg.cache_dir / "radar"
        self._lock = threading.Lock()
        self._next_try = 0.0
        self._failures = 0
        self._parsed = None  # (key, RadarImage)

    # -- store
    def _path(self, name):
        return self.dir / name

    def _meta(self):
        try:
            m = json.loads(self._path(f"{self.product}.json").read_text())
            return m if isinstance(m, dict) else {}
        except (OSError, ValueError):
            return {}

    def _write(self, name, data):
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self._path(name + ".%d.tmp" % threading.get_ident())
        tmp.write_bytes(data)
        tmp.replace(self._path(name))

    # -- network (background only)
    def refresh(self, now=None):
        now = time.time() if now is None else now
        with self._lock:
            if now < self._next_try:
                return
            try:
                self._refresh_layers(now)
                self._next_try = self._refresh_frame(now)
                self._failures = 0
            except CacheOnly:
                return
            except Exception as e:
                self._failures += 1
                wait = min(RETRY_MAX_S, RETRY_S * 2 ** (self._failures - 1))
                self._next_try = now + wait
                print(f"radar: fetch failed ({e!r}); next try in {wait // 60} min", flush=True)

    def _refresh_layers(self, now):
        meta = self._meta()
        layers = meta.get("layers") or {}
        for layer in LAYERS:
            if now - layers.get(layer, 0) < LAYERS_TTL_S and self._path(f"{self.product}.{layer}.png").exists():
                continue
            try:  # a missing layer costs a quieter map, never the rain
                data = self.transport.get(LAYERS_DIR, f"{self.product}.{layer}.png")
                _png(data)  # junk never replaces a good layer
            except CacheOnly:
                raise
            except Exception as e:
                print(f"radar: layer {layer} fetch failed ({e!r})", flush=True)
                continue
            self._write(f"{self.product}.{layer}.png", data)
            layers[layer] = now
            meta["layers"] = layers
            self._write(f"{self.product}.json", json.dumps(meta).encode())

    def _refresh_frame(self, now):
        """Fetch the newest frame if it is newer than the stored one. Returns
        when to look again: when the next frame is due, at least LIST_GAP_S
        from now."""
        names = self.transport.list(RADAR_DIR, f"{self.product}.T.*.png")
        stamped = {}
        for n in names:
            t = parse_stamp(n, self.product)
            if t is not None and t <= now + 120:  # nothing from the future
                stamped[t] = n.rsplit("/", 1)[-1]
        if not stamped:
            return now + RETRY_S
        newest = max(stamped)
        meta = self._meta()
        if newest > meta.get("issued_at", 0):
            data = self.transport.get(RADAR_DIR, stamped[newest])
            rain_levels(data)  # parse before storing: a bad frame never replaces a good one
            self._write(f"{self.product}.frame.png", data)
            meta = self._meta()
            meta.update(issued_at=newest, fetched_at=now, file=stamped[newest])
            self._write(f"{self.product}.json", json.dumps(meta).encode())
        due = newest + cadence(stamped) + PUBLISH_LAG_S
        return max(due, now + LIST_GAP_S)

    # -- request path
    def latest(self, now=None):
        meta = self._meta()
        issued = meta.get("issued_at")
        frame = self._path(f"{self.product}.frame.png")
        if not issued or not frame.exists():
            return None
        layer_times = tuple(sorted((meta.get("layers") or {}).items()))
        key = (issued, layer_times)
        cached = self._parsed
        if cached and cached[0] == key:
            return cached[1]
        try:
            rain = rain_levels(frame.read_bytes())
        except Exception as e:
            print(f"radar: stored frame unusable ({e!r})", flush=True)
            return None
        layers = {}
        for layer in LAYERS:
            try:
                layers[layer] = self._path(f"{self.product}.{layer}.png").read_bytes()
            except OSError:
                pass
        name, lat, lon = SITES[self.product[3:5]]
        km_per_px, range_km = RANGES[self.product[5]]
        radar = RadarImage(width=SIZE, height=SIZE, rain=rain, base=base_classes(layers), issued_at=float(issued),
                           km_per_px=km_per_px, site_lat=lat, site_lon=lon, name=name, range_km=range_km,
                           credit=CREDIT)
        self._parsed = (key, radar)
        return radar
