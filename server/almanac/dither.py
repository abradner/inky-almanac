"""Atkinson error diffusion onto a small fixed palette.

Floyd–Steinberg pushes all of a pixel's error onward, which on seven muted inks
turns dark midtones into red/green confetti. Atkinson passes on only 6/8 of it:
shadows go cleanly black, highlights clean white, and what is left reads as a
print rather than noise. Colour matching is done in CIELAB, so a pale sky is
matched as "light blue" (white + blue) rather than by raw RGB distance, which
drifts towards green.

Pure Python: ~240k pixels, a few seconds on a Pi, once an hour.
"""

from PIL import Image

def _lin(c):
    c /= 255
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def lab(rgb):
    r, g, b = (_lin(float(c)) for c in rgb)
    x = (0.4124 * r + 0.3576 * g + 0.1805 * b) / 0.95047
    y = 0.2126 * r + 0.7152 * g + 0.0722 * b
    z = (0.0193 * r + 0.1192 * g + 0.9505 * b) / 1.08883

    def f(t):
        return t ** (1 / 3) if t > 0.008856 else 7.787 * t + 16 / 116

    fx, fy, fz = f(x), f(y), f(z)
    return 116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)


def _nearest_table(palette):
    """Nearest-ink lookup on a 32-level-per-channel grid (32k entries)."""
    table = bytearray(32 * 32 * 32)
    inks = [lab(p) for p in palette]
    for r5 in range(32):
        for g5 in range(32):
            for b5 in range(32):
                L, A, B = lab((r5 * 255 / 31, g5 * 255 / 31, b5 * 255 / 31))
                best, bi = None, 0
                for i, (pl, pa, pb) in enumerate(inks):
                    dist = (L - pl) ** 2 + (A - pa) ** 2 + (B - pb) ** 2
                    if best is None or dist < best:
                        best, bi = dist, i
                table[(r5 << 10) | (g5 << 5) | b5] = bi
    return table


_TABLES = {}


def map_ink(r, g, b):
    """Ink for one pixel of a BOM warning map.

    BOM draws these maps from a small palette, so each colour gets a fixed,
    deliberate ink rather than its nearest match:
      land (255,242,211) and white boxes   -> white
      sea (219,237,255)                    -> blue
      warning area (255,220,0)             -> yellow
      immediate threat (250,170,30)        -> orange
      severe cells (230,50,50), (210,10,10)-> red
      town labels and dots (0,0,255)       -> black
      region names (51,51,51), black text  -> black
      boundary lines (128,128,128) and the
      anti-aliased greys around them       -> green
    """
    from colorsys import rgb_to_hsv

    h, s, v = rgb_to_hsv(r / 255, g / 255, b / 255)
    h *= 360
    if s >= 0.45 and v >= 0.35:  # saturated: the warning colours and labels
        if 190 <= h <= 270:
            return 0  # blue labels -> black
        if h < 20 or h >= 330:
            return 4  # red
        if h < 45:
            return 6  # orange
        if h < 75:
            return 5  # yellow
        if h < 170:
            return 2  # green
        return 0
    if v < 0.45:
        return 0  # black and dark grey text (also once downscaling has greyed it)
    if 180 <= h <= 260 and s >= 0.08 and v >= 0.9:
        return 3  # pale blue sea -> blue
    if v >= 0.86:
        return 1  # land, white
    return 2  # greys: boundaries -> green


_MAP_TABLE = None


def map_inks(img, out_palette):
    """A BOM warning map as panel inks (see map_ink), no dithering."""
    global _MAP_TABLE
    if _MAP_TABLE is None:
        _MAP_TABLE = bytes(map_ink(r5 * 255 // 31, g5 * 255 // 31, b5 * 255 // 31)
                           for r5 in range(32) for g5 in range(32) for b5 in range(32))
    src = img.convert("RGB").tobytes()
    out = bytes(_MAP_TABLE[((src[i] >> 3) << 10) | ((src[i + 1] >> 3) << 5) | (src[i + 2] >> 3)]
                for i in range(0, len(src), 3))
    result = Image.frombytes("P", img.size, out)
    result.putpalette(out_palette)
    return result


def atkinson(img, palette, out_palette):
    """Dither an RGB image to indices into `palette` (list of RGB tuples).

    Returns a P-mode image whose palette is `out_palette` (flat 768 list).
    """
    key = tuple(palette)
    if key not in _TABLES:
        _TABLES[key] = _nearest_table(palette)
    table = _TABLES[key]
    w, h = img.size
    src = img.convert("RGB").tobytes()
    # Float working buffers per channel, with 2 rows and 2 columns of slack.
    stride = w + 4
    R = [0.0] * (stride * (h + 2))
    G = [0.0] * (stride * (h + 2))
    B = [0.0] * (stride * (h + 2))
    for y in range(h):
        row = y * stride + 2
        o = y * w * 3
        for x in range(w):
            R[row + x] = src[o]
            G[row + x] = src[o + 1]
            B[row + x] = src[o + 2]
            o += 3
    out = bytearray(w * h)
    offsets = (1, 2, stride - 1, stride, stride + 1, 2 * stride)
    for y in range(h):
        base = y * stride + 2
        for x in range(w):
            p = base + x
            r, g, b = R[p], G[p], B[p]
            rc = 0 if r < 0 else 31 if r > 255 else int(r * 31 / 255 + 0.5)
            gc = 0 if g < 0 else 31 if g > 255 else int(g * 31 / 255 + 0.5)
            bc = 0 if b < 0 else 31 if b > 255 else int(b * 31 / 255 + 0.5)
            i = table[(rc << 10) | (gc << 5) | bc]
            out[y * w + x] = i
            pr, pg, pb = palette[i]
            er, eg, eb = (r - pr) / 8, (g - pg) / 8, (b - pb) / 8
            for off in offsets:
                q = p + off
                R[q] += er
                G[q] += eg
                B[q] += eb
    result = Image.frombytes("P", (w, h), bytes(out))
    result.putpalette(out_palette)
    return result
