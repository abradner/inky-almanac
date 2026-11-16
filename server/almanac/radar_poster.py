"""Draw a rain radar frame (domain.RadarImage) in the picture area.

Fixed ink mapping, no error diffusion, so the picture is the same every time
for the same frame and nothing shimmers between redraws:

- the base map is quiet: white land and sea, a green coastline, dotted
  green range rings, black place names. Topography is left out: any fill
  there would compete with the light-rain stipple.
- rain follows the radar scale with the inks the panel has: light rain is a
  pale blue stipple on white, thickening to solid blue; moderate rain is
  green; heavy is yellow, orange, then red; the two extreme levels add
  black to red. Mixed levels are an ordered 4x4 stipple of two inks.
- place names are drawn over the rain, as on the source's own maps, and
  home gets a small ring.
"""

from datetime import datetime

from PIL import Image, ImageDraw

from . import radar as radar_mod
from .domain import BASE_COAST, BASE_LABEL, BASE_RING, RAIN_LEVELS
from .palette import BLACK, BLUE, GREEN, ORANGE, RED, WHITE, YELLOW

# level -> (base ink, stipple ink, sixteenths of stipple)
RAIN_INKS = {
    1: (WHITE, BLUE, 3),
    2: (WHITE, BLUE, 6),
    3: (WHITE, BLUE, 10),
    4: (BLUE, BLUE, 0),
    5: (BLUE, GREEN, 8),
    6: (GREEN, BLUE, 4),
    7: (GREEN, GREEN, 0),
    8: (YELLOW, YELLOW, 0),
    9: (YELLOW, ORANGE, 8),
    10: (ORANGE, ORANGE, 0),
    11: (ORANGE, RED, 8),
    12: (RED, RED, 0),
    13: (RED, RED, 0),
    14: (RED, BLACK, 4),
    15: (RED, BLACK, 8),
}
assert set(RAIN_INKS) == set(range(1, RAIN_LEVELS + 1))
BAYER4 = ((0, 8, 2, 10), (12, 4, 14, 6), (3, 11, 1, 9), (15, 7, 13, 5))


def rain_ink(level, x, y):
    """The ink for one rain pixel at (x, y) of the picture."""
    base, stipple, n = RAIN_INKS[level]
    return stipple if BAYER4[y & 3][x & 3] < n else base


def _tables():
    """For each (y % 4, x % 4): a 256-entry translate table from a combined
    byte (rain level, or 32 + base class when dry) to an ink."""
    tables = {}
    for py in range(4):
        for px in range(4):
            t = bytearray([WHITE]) * 256
            for level in RAIN_INKS:
                t[level] = rain_ink(level, px, py)
            t[32 + BASE_COAST] = GREEN
            t[32 + BASE_RING] = GREEN if (px + py) % 2 == 0 else WHITE  # dotted
            t[32 + BASE_LABEL] = BLACK
            tables[(py, px)] = bytes(t)
    return tables


_TABLES = None


def ink_plane(radar, box):
    """The radar cropped to `box` (x0, y0, x1, y1 in image pixels) as ink
    bytes, row-major. Pixels outside the image are white."""
    global _TABLES
    if _TABLES is None:
        _TABLES = _tables()
    x0, y0, x1, y1 = box
    w = x1 - x0
    out = bytearray()
    W, H = radar.width, radar.height
    for y in range(y0, y1):
        if not 0 <= y < H:
            out += bytes([WHITE]) * w
            continue
        lo, hi = max(0, x0), min(W, x1)
        rain = radar.rain[y * W + lo:y * W + hi]
        base = radar.base[y * W + lo:y * W + hi]
        # One combined byte per pixel: the rain level, else 32 + base class.
        merged = bytes(r if r else 32 + (b if b in (BASE_COAST, BASE_RING, BASE_LABEL) else 0)
                       for r, b in zip(rain, base))
        # Labels win over rain.
        merged = bytes(32 + BASE_LABEL if b == BASE_LABEL else m for m, b in zip(merged, base))
        row = bytearray(hi - lo)
        for phase in range(4):
            t = _TABLES[(y & 3, (lo + phase) & 3)]
            row[phase::4] = merged[phase::4].translate(t)
        out += bytes([WHITE]) * (lo - x0) + row + bytes([WHITE]) * (x1 - hi)
    return bytes(out)


def crop_box(radar, size):
    """The size-sized window of the image centred on the radar site."""
    w, h = size
    x0, y0 = (radar.width - w) // 2, (radar.height - h) // 2
    return x0, y0, x0 + w, y0 + h


def draw(canvas, radar, *, size, home, tz, now, font):
    """Fill the picture area (size, at the canvas origin) with the radar, or
    a "waiting" note when there is no frame yet. `font(px)` gives the bold
    face for the label."""
    w, h = size
    d = ImageDraw.Draw(canvas)
    d.rectangle([0, 0, w - 1, h - 1], fill=WHITE)
    if radar is None:
        f = font(18)
        d.text((w // 2, h // 2), "Radar: waiting for the first frame", font=f, fill=BLACK, anchor="mm")
        return
    box = crop_box(radar, size)
    plane = Image.frombytes("P", size, ink_plane(radar, box))
    canvas.paste(plane, (0, 0))
    hx, hy = radar_mod.pixel_of(radar, *home)
    hx, hy = hx - box[0], hy - box[1]
    if 0 <= hx < w and 0 <= hy < h:
        for r, ink, width in ((8, WHITE, 5), (7, BLACK, 3), (6, RED, 1)):
            d.ellipse([hx - r, hy - r, hx + r, hy + r], outline=ink, width=width)
    # "RADAR · 12:14" pill, bottom left (badges sit top left).
    issued = datetime.fromtimestamp(radar.issued_at, tz)
    old = now.timestamp() - radar.issued_at > radar_mod.FRESH_S
    text = f"RADAR · {issued:%H:%M}" + (" · OLD" if old else "")
    f = font(15)
    tw = int(d.textlength(text, font=f)) + 18
    x, y = 12, h - 12 - 28
    d.rounded_rectangle([x - 3, y - 3, x + tw + 3, y + 31], radius=16, fill=WHITE)
    d.rounded_rectangle([x, y, x + tw, y + 28], radius=14, fill=RED if old else WHITE, outline=BLACK, width=2)
    d.text((x + 9, y + 5), text, font=f, fill=WHITE if old else BLACK)


def caption(radar, tz):
    """(title, credit) for the side panel's caption, in place of the photo's."""
    if radar is None:
        return "Rain radar", "Waiting for the first frame"
    issued = datetime.fromtimestamp(radar.issued_at, tz)
    where = f"{radar.name} {radar.range_km} km" if radar.range_km else radar.name
    return f"Rain radar · {issued:%H:%M}", " · ".join(p for p in (where, radar.credit) if p)
