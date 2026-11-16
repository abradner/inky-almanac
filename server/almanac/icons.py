"""Weather and moon glyphs, drawn with solid inks (no anti-aliasing: e-ink has
no greys, and AA edges dither into speckle)."""

import math

from . import astro
from .palette import BLACK, BLUE, ORANGE, WHITE, YELLOW


def sun(d, cx, cy, r, rays=True, ray=(5, 13), ray_w=4, outline=3):
    """A sun of radius r; rays run from r + ray[0] to r + ray[1]."""
    if rays:
        for k in range(8):
            a = k * math.pi / 4
            x1, y1 = cx + math.cos(a) * (r + ray[0]), cy + math.sin(a) * (r + ray[0])
            x2, y2 = cx + math.cos(a) * (r + ray[1]), cy + math.sin(a) * (r + ray[1])
            d.line([(x1, y1), (x2, y2)], fill=ORANGE, width=ray_w)
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=YELLOW, outline=ORANGE, width=outline)


def cloud(d, x, y, w, fill=WHITE, outline=BLACK, stroke=3):
    """A cloud whose bounding box starts at (x, y) and is w wide (about w*0.6 tall)."""
    h = w * 0.6
    parts = [
        ("e", x + w * 0.05, y + h * 0.40, x + w * 0.45, y + h * 0.95),
        ("e", x + w * 0.22, y + h * 0.10, x + w * 0.62, y + h * 0.70),
        ("e", x + w * 0.48, y + h * 0.25, x + w * 0.95, y + h * 0.95),
        ("r", x + w * 0.25, y + h * 0.60, x + w * 0.75, y + h * 0.95),
    ]
    for grow, colour in ((stroke, outline), (0, fill)):
        for kind, x0, y0, x1, y1 in parts:
            box = [x0 - grow, y0 - grow, x1 + grow, y1 + grow]
            if kind == "e":
                d.ellipse(box, fill=colour)
            else:
                d.rectangle(box, fill=colour)


def _drops(d, x, y, w, n, length):
    for k in range(n):
        dx = x + w * (0.2 + 0.6 * k / max(1, n - 1))
        d.line([(dx, y), (dx - length * 0.35, y + length)], fill=BLUE, width=4)


def weather(d, kind, x, y, size):
    """Draw a weather icon in the size x size box at (x, y)."""
    s = size
    if kind == "clear":
        sun(d, x + s / 2, y + s / 2, s * 0.24)
    elif kind == "partly":
        sun(d, x + s * 0.62, y + s * 0.34, s * 0.18)
        cloud(d, x + s * 0.04, y + s * 0.38, s * 0.72)
    elif kind == "cloudy":
        cloud(d, x + s * 0.30, y + s * 0.14, s * 0.62)
        cloud(d, x + s * 0.02, y + s * 0.36, s * 0.78)
    elif kind == "fog":
        cloud(d, x + s * 0.12, y + s * 0.08, s * 0.76)
        for k in range(3):
            yy = y + s * (0.66 + 0.11 * k)
            d.line([(x + s * (0.10 + 0.05 * k), yy), (x + s * (0.90 - 0.05 * k), yy)], fill=BLACK, width=3)
    elif kind in ("drizzle", "rain", "showers"):
        if kind == "showers":
            sun(d, x + s * 0.68, y + s * 0.24, s * 0.15, rays=False)
        cloud(d, x + s * 0.08, y + s * 0.10, s * 0.80)
        n, length = {"drizzle": (3, s * 0.12), "rain": (4, s * 0.22), "showers": (3, s * 0.20)}[kind]
        _drops(d, x + s * 0.12, y + s * 0.68, s * 0.70, n, length)
    elif kind in ("storm", "hail"):
        cloud(d, x + s * 0.08, y + s * 0.06, s * 0.80)
        bolt = [(0.52, 0.52), (0.36, 0.78), (0.48, 0.78), (0.40, 0.98), (0.66, 0.68), (0.53, 0.68), (0.62, 0.52)]
        d.polygon([(x + s * a, y + s * b) for a, b in bolt], fill=YELLOW, outline=BLACK, width=2)
        if kind == "hail":  # two hailstones either side of the bolt
            for cx in (0.24, 0.76):
                r = s * 0.06
                d.ellipse([x + s * cx - r, y + s * 0.88 - r, x + s * cx + r, y + s * 0.88 + r], fill=WHITE, outline=BLUE, width=3)
    elif kind == "snow":
        cloud(d, x + s * 0.08, y + s * 0.08, s * 0.80)
        for k in range(3):
            cx, cy, r = x + s * (0.28 + 0.22 * k), y + s * 0.82, s * 0.06
            for a in range(3):
                ang = a * math.pi / 3
                d.line([(cx - math.cos(ang) * r, cy - math.sin(ang) * r), (cx + math.cos(ang) * r, cy + math.sin(ang) * r)], fill=BLUE, width=2)
    else:
        cloud(d, x + s * 0.08, y + s * 0.2, s * 0.8)


def moon(d, cx, cy, r, phase, southern):
    """Moon disc: lit part yellow, dark part black, as seen from this hemisphere."""
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=BLACK)
    for row in range(-r, r + 1):
        half = math.sqrt(max(0.0, r * r - row * row))
        span = astro.lit_span(phase, half, southern)
        if span:
            lo, hi = span
            if hi - lo >= 1:
                d.line([(cx + lo, cy + row), (cx + hi, cy + row)], fill=YELLOW)
    d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=BLACK, width=2)
