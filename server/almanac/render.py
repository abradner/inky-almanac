"""Compose the 800x480 poster as an indexed image whose indices are panel inks.

Left: the landscape photo of the day, dithered to the seven inks.
Right: date, the day's weather, an hourly temperature/rain strip, sun and moon,
and the photo's caption and credit. Everything on the right is drawn with solid inks
so text stays crisp.
"""

import re
from datetime import datetime, timedelta
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance, ImageFont

from . import astro, conditions as cond_mod, icons
from . import facts as facts_mod
from .facts import coming_hour
from .dither import atkinson, map_inks
from .domain import describe
from .palette import BLACK, BLUE, GREEN, INK_RGB, ORANGE, PNG_RGB, RED, WHITE, YELLOW, _flat, new_canvas, palette_image

W, H = 800, 480
PHOTO_W = 500
SIDE_X = PHOTO_W + 18
SIDE_W = W - SIDE_X - 16

_FONT_DIRS = ["/usr/share/fonts/truetype/dejavu", "/usr/share/fonts/dejavu", "/usr/local/share/fonts"]
_font_cache = {}


def font(name, size):
    key = (name, size)
    if key not in _font_cache:
        f = None
        for folder in _FONT_DIRS:
            p = Path(folder) / name
            if p.exists():
                f = ImageFont.truetype(str(p), size)
                break
        _font_cache[key] = f or ImageFont.load_default(size)
    return _font_cache[key]


def SANS(s):
    return font("DejaVuSans.ttf", s)


def BOLD(s):
    return font("DejaVuSans-Bold.ttf", s)


def COND(s):
    return font("DejaVuSansCondensed-Bold.ttf", s) if _has("DejaVuSansCondensed-Bold.ttf") else BOLD(s)


def ITALIC(s):
    return font("DejaVuSerif-Italic.ttf", s) if _has("DejaVuSerif-Italic.ttf") else font("DejaVuSerif.ttf", s)


def _has(name):
    return any((Path(d) / name).exists() for d in _FONT_DIRS)


def fit_text(d, text, fnt, width):
    if d.textlength(text, font=fnt) <= width:
        return text
    while text and d.textlength(text + "…", font=fnt) > width:
        text = text[:-1]
    return text.rstrip(" ,;:-") + "…"


def wrap(d, text, fnt, width, lines, first_indent=0):
    """Up to `lines` lines of `text`, each within `width` (the first within
    width - first_indent); the last is cut with an ellipsis if it must be."""
    # Split on ordinary spaces only, so a no-break space keeps "92 km/h" whole.
    words, out, cur = [w for w in re.split(r"[ \t\r\n]+", text) if w], [], ""
    for w in words:
        trial = (cur + " " + w).strip()
        if d.textlength(trial, font=fnt) <= width - (0 if out else first_indent):
            cur = trial
        else:
            if cur:
                out.append(cur)
            cur = w
    if cur:
        out.append(cur)
    if len(out) > lines:
        out = out[: lines - 1] + [" ".join(out[lines - 1 :])]
    return [fit_text(d, line, fnt, width - (first_indent if i == 0 else 0)) for i, line in enumerate(out)]


# ---------------------------------------------------------------- photo panel


def dither_photo(img, size, method="atkinson"):
    """Fit, boost and dither a photo into an index image of `size`.

    The inks are muted, so a little extra saturation and contrast before
    dithering brings back what a screen would show.
    """
    tw, th = size
    a, t = img.width / img.height, tw / th
    # Crop to fill when that loses at most 40% of the frame (a 3:2 landscape
    # loses ~31%); otherwise letterbox it on a white mat.
    loss = 1 - min(a, t) / max(a, t)
    if loss <= 0.40:
        if a > t:
            nw = int(img.height * t)  # centre crop
            img = img.crop(((img.width - nw) // 2, 0, (img.width - nw) // 2 + nw, img.height))
        else:
            nh = int(img.width / t)
            top = max(0, int((img.height - nh) * 0.4))  # keep a little more sky than floor
            img = img.crop((0, top, img.width, top + nh))
        box = (0, 0, tw, th)
    else:
        margin = 14
        if a > t:
            w = tw - 2 * margin
            h = int(w / a)
        else:
            h = th - 2 * margin
            w = int(h * a)
        box = ((tw - w) // 2, (th - h) // 2, (tw - w) // 2 + w, (th - h) // 2 + h)
    bw, bh = box[2] - box[0], box[3] - box[1]
    img = img.resize((bw, bh), Image.LANCZOS)
    img = ImageEnhance.Color(img).enhance(1.3)
    img = ImageEnhance.Contrast(img).enhance(1.1)
    img = ImageEnhance.Brightness(img).enhance(1.06)  # Atkinson darkens shadows
    if method == "atkinson":
        dithered = atkinson(img, INK_RGB, _flat(PNG_RGB))
    else:
        dithered = img.quantize(palette=palette_image(), dither=Image.Dither.FLOYDSTEINBERG)
    return dithered, box


def draw_photo(canvas, photo):
    d = ImageDraw.Draw(canvas)
    if photo is None:
        _placeholder_photo(d)
        return
    dithered, box = dither_photo(photo.image, (PHOTO_W, H))
    canvas.paste(dithered, box[:2])
    if box != (0, 0, PHOTO_W, H):
        d.rectangle([box[0] - 3, box[1] - 3, box[2] + 2, box[3] + 2], outline=BLACK, width=2)


def draw_warning_map(canvas, warning):
    """A warning's threat-area map (domain.MapImage) in place of the photo: fitted whole
    (it is a map, so no cropping), each of BOM's map colours given a fixed
    ink (dither.map_ink), no dithering, so labels and borders stay crisp."""
    import io

    img = Image.open(io.BytesIO(warning.png)).convert("RGB")
    a = img.width / img.height
    w, h = (PHOTO_W, int(PHOTO_W / a)) if a > PHOTO_W / H else (int(H * a), H)
    img = img.resize((w, h), Image.LANCZOS)
    canvas.paste(map_inks(img, _flat(PNG_RGB)), (0, (H - h) // 2))
    return w


def _placeholder_photo(d):
    """Concentric sunrise bands, for when no photo could be fetched."""
    bands = [BLUE, GREEN, YELLOW, ORANGE, RED]
    cx, cy = PHOTO_W // 2, H + 40
    for i, c in enumerate(bands):
        r = 520 - i * 90
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=c)


# ---------------------------------------------------------------- side panel

# Narrow type for the side panel: the condensed cut fits ~15% more per line,
# which is the difference between a forecast or title that fits and one that
# gets clipped.
def C(s):
    return font("DejaVuSansCondensed.ttf", s) if _has("DejaVuSansCondensed.ttf") else SANS(s)


def CB(s):
    return COND(s)


def CI(s):
    return font("DejaVuSerifCondensed-Italic.ttf", s) if _has("DejaVuSerifCondensed-Italic.ttf") else ITALIC(s)


def set_layout(photo_w):
    """Split the poster between the picture and the text panel. A warning map
    is narrower than a photo, so the text panel gets the space it frees."""
    global PHOTO_W, SIDE_X, SIDE_W
    PHOTO_W = photo_w
    SIDE_X = photo_w + 16
    SIDE_W = W - SIDE_X - 14


def _points(xy):
    flat = []
    for p in xy:
        if isinstance(p, (tuple, list)):
            flat.extend(p)
        else:
            flat.append(p)
    return flat[0::2], flat[1::2]


class Recorder:
    """An ImageDraw stand-in that draws, and also notes the bounding box of
    everything drawn under the current `section` name. draw_side returns those
    boxes, so tests can prove nothing spills out of the panel or into the
    caption."""

    def __init__(self, canvas):
        self._d = ImageDraw.Draw(canvas)
        self.canvas = canvas
        self.section = None
        self.boxes = {}
        self.texts = []  # (section, text, ink), in drawing order
        self.marks = {}  # positions worth testing (the chart's now-marker)

    def __getattr__(self, name):  # textlength, textbbox, ...
        return getattr(self._d, name)

    def note(self, x0, y0, x1, y1):
        if self.section is None:
            return
        b = self.boxes.get(self.section)
        self.boxes[self.section] = (x0, y0, x1, y1) if b is None else (
            min(b[0], x0), min(b[1], y0), max(b[2], x1), max(b[3], y1))

    def text(self, xy, text, fill=None, font=None, anchor=None, **kw):
        self._d.text(xy, text, fill=fill, font=font, anchor=anchor, **kw)
        self.texts.append((self.section, text, fill))
        if text.strip():
            self.note(*self._d.textbbox(xy, text, font=font, anchor=anchor, stroke_width=kw.get("stroke_width", 0)))

    def _shape(self, name, xy, kw):
        getattr(self._d, name)(xy, **kw)
        xs, ys = _points(xy)
        # Shapes cover their end pixels (+1 makes the box exclusive, like a
        # text box). A thick line only grows across its run: a level rule
        # gets taller, not longer.
        half = (kw.get("width") or 1) / 2 if name == "line" else 0
        grow_x = 0 if name == "line" and min(xs) != max(xs) and min(ys) == max(ys) else half
        grow_y = 0 if name == "line" and min(ys) != max(ys) and min(xs) == max(xs) else half
        self.note(min(xs) - grow_x, min(ys) - grow_y, max(xs) + 1 + grow_x, max(ys) + 1 + grow_y)

    def line(self, xy, **kw):
        self._shape("line", xy, kw)

    def rectangle(self, xy, **kw):
        self._shape("rectangle", xy, kw)

    def rounded_rectangle(self, xy, **kw):
        self._shape("rounded_rectangle", xy, kw)

    def ellipse(self, xy, **kw):
        self._shape("ellipse", xy, kw)

    def polygon(self, xy, **kw):
        self._shape("polygon", xy, kw)

    def point(self, xy, **kw):
        self._shape("point", xy, kw)


def _ink_top(fnt, text):
    """How far below the draw position a text's ink starts."""
    return fnt.getbbox(text)[1]


def _deg(t):
    return f"{round(t)}°"


def _clock(dt):
    """'5:45am'."""
    return dt.strftime("%I:%M").lstrip("0") + dt.strftime("%p").lower()


CHART_MIN, CHART_MAX = 48, 130
SUN_ROW_H = 32


def draw_side(canvas, day_fc, *, now, poster_day, is_tomorrow, forecast, photo, cfg, warning=None, cond=None,
              observed=None):
    """The text panel, top to bottom: header (day, date, the day's icon and
    one max/min pair), the forecast text, the coming hour and its facts, the
    hourly chart (which takes whatever height is left), sun and moon, and the
    caption (laid out bottom-up).

    Returns the layout it used: the box of each section, the caption's top,
    the chart's height and the ranked facts."""
    d = Recorder(canvas)
    x, y = SIDE_X, 10
    y = _header(d, x, y, now=now, forecast=forecast, day_fc=day_fc, is_tomorrow=is_tomorrow, cfg=cfg)
    d.section = "forecast"
    if day_fc is None:
        d.text((x, y + 4), "No forecast yet", font=CB(20), fill=RED)
        d.text((x, y + 30), "Waiting for the forecast…", font=C(15), fill=BLACK)
        y += 52
    else:
        y = _forecast_line(d, day_fc, x, y, is_tomorrow)
    d.section = "rule"
    d.line([(x, y + 5), (x + SIDE_W, y + 5)], fill=BLACK, width=2)
    y += 13

    coming = coming_hour(forecast, now, poster_day, is_tomorrow, observed=observed)
    if coming is not None:
        y = _hero(d, coming, x, y)
        y = _facts_table(d, coming.facts[1:5], x, y + 4)

    # Bottom-up: the caption first, then the sun row above it; the chart gets
    # what is left between them and the facts, clamped to CHART_MIN..CHART_MAX.
    lines = _caption_lines(d, photo, now=now, forecast=forecast, cfg=cfg, poster_day=poster_day, warning=warning, cond=cond,
                           observed=observed)
    sun_h = SUN_ROW_H + 6 if day_fc is not None else 0
    chart_min = CHART_MIN + 8 if day_fc is not None else 0
    kept = _fit_caption(lines, y + chart_min + sun_h)
    if warning and sun_h and len(kept) < len(lines):
        sun_h = 0  # in map mode the warnings outrank sunrise times
        kept = _fit_caption(lines, y + chart_min)
    cap_top = H - 6 - sum(f.size + 3 for _, f, _ in kept) - 4  # the caption's rule
    bottom = cap_top - 5
    if sun_h:
        bottom -= sun_h
        _sun_moon(d, day_fc, x, bottom + 5, now=now, poster_day=poster_day, cfg=cfg)
    chart_h = 0
    if day_fc is not None:
        chart_h = max(CHART_MIN, min(CHART_MAX, int(bottom - (y + 8))))
        _hourly_chart(d, day_fc, x, y + 8, SIDE_W, chart_h, now=None if is_tomorrow else now)
    d.section = "caption"
    _draw_caption(d, kept, x, cap_top)
    return {"boxes": d.boxes, "texts": d.texts, "marks": d.marks, "caption_top": cap_top, "chart_h": chart_h, "sun_row": bool(sun_h),
            "facts": coming.facts if coming else [], "caption_lines": len(kept), "caption_wanted": len(lines)}


# ---------------------------------------------------------------- header

DAY_FONTS = [(BOLD, 30), (BOLD, 28), (BOLD, 26), (CB, 28), (CB, 26), (CB, 24), (CB, 22), (CB, 20)]


def header_icon_size(day_name):
    """The day's icon steps down as the day name grows."""
    return 40 if len(day_name) >= 9 else 48 if len(day_name) >= 8 else 56


def lead_pair(now, forecast, day_fc, is_tomorrow, high_leads_from=9):
    """((ink, temp, LABEL), (ink, temp, label) or None): the one max/min pair
    the header shows. The big one is whichever extreme comes next."""
    if is_tomorrow:  # from 6 pm the forecast is tomorrow's
        return (BLUE, day_fc.t_min, "LOW TONIGHT"), (RED, day_fc.t_max, "high tomorrow")
    if now.hour < high_leads_from:
        return (BLUE, day_fc.t_min, "OVERNIGHT LOW"), (RED, day_fc.t_max, "high today")
    tonight = forecast.day(now.date() + timedelta(days=1)) if forecast else None
    return (RED, day_fc.t_max, "HIGH TODAY"), (None if tonight is None else (BLUE, tonight.t_min, "low tonight"))


def _header(d, x, y, *, now, forecast, day_fc, is_tomorrow, cfg):
    """Day name and date on the left (always today's: the date never moves in
    the evening); on the right the forecast day's icon beside the big
    extreme, and the other extreme in small type underneath both."""
    d.section = "header"
    name, date_s = now.strftime("%A"), now.strftime("%-d %B")
    right = x + SIDE_W
    block_w = other_w = 0
    bottom = y
    if day_fc is not None:
        (ink, t, label), other = lead_pair(now, forecast, day_fc, is_tomorrow, cfg.high_leads_from)
        size = header_icon_size(name)
        num = _deg(t)
        tw = max(d.textlength(label, font=CB(10)), d.textlength(num, font=BOLD(34)))
        block_w = size + 4 + tw
        d.section = "header.pair"
        icons.weather(d, day_fc.icon, right - block_w, y, size)
        d.text((right, y), label, font=CB(10), fill=ink, anchor="ra")
        d.text((right, y + 13 - _ink_top(BOLD(34), num)), num, font=BOLD(34), fill=ink, anchor="ra")
        oy = y + max(size, 13 + 36) + 2
        if other is not None:
            o_ink, o_t, o_label = other
            val = _deg(o_t)
            vw = d.textlength(val, font=CB(12))
            lab = o_label + " "
            other_w = vw + d.textlength(lab, font=C(12))
            d.text((right - vw, oy), val, font=CB(12), fill=o_ink)
            d.text((right - other_w, oy), lab, font=C(12), fill=BLACK)
        bottom = oy + 15
    d.section = "header.day"
    avail = SIDE_W - max(block_w, other_w) - 6 if block_w else SIDE_W
    face, size = next(((f, s) for f, s in DAY_FONTS if d.textlength(name, font=f(s)) <= avail), DAY_FONTS[-1])
    d.text((x, y), fit_text(d, name, face(size), avail), font=face(size), fill=BLACK)
    dy = y + size + 4
    d.text((x, dy), fit_text(d, date_s, C(18), avail), font=C(18), fill=BLACK)
    return max(bottom, dy + 22) + 1


def _forecast_line(d, fc, x, y, is_tomorrow):
    """The day's précis across the full width, two lines at most; from 6 pm it is
    tomorrow's, and says so."""
    prefix = "Tomorrow: " if is_tomorrow else ""
    indent = d.textlength(prefix, font=CB(14)) if prefix else 0
    lines = wrap(d, fc.label, C(14), SIDE_W, 2, first_indent=indent)
    if prefix:
        d.text((x, y), prefix.strip(), font=CB(14), fill=ORANGE)
    for i, line in enumerate(lines):
        d.text((x + (indent if i == 0 else 0), y + i * 17), line, font=C(14), fill=BLACK)
    return y + max(1, len(lines)) * 17


# ---------------------------------------------------------------- coming hour


def _hour_label(h):
    def f(hr):
        hr %= 24
        return ("12" if hr % 12 == 0 else str(hr % 12)), ("am" if hr < 12 else "pm")

    (a, ap), (b, bp) = f(h), f(h + 1)
    return f"{a}{'' if ap == bp else ap}–{b}{bp}"


def hour_icon(code):
    return "hail" if code in facts_mod.HAIL else describe(code)[1]


HERO_ICON = 62


def glue_units(text):
    """No-break spaces before units, so a wrapped value never strands "mm"."""
    return re.sub(r" (mm|km/h)\b", "\u00a0\\1", text)


def _hero(d, coming, x, y):
    """The coming hour's icon (never shrunk), its label over its temperature,
    and the top fact right-aligned: value large, label small underneath."""
    d.section = "hero"
    hour = coming.hour
    label = _hour_label(coming.label_hour).upper()
    temp = _deg(hour.temp)
    col_w = max(d.textlength(label, font=CB(13)), d.textlength(temp, font=BOLD(50)))
    avail = SIDE_W - HERO_ICON - 4 - col_w - 8
    if coming.facts:
        top = coming.facts[0]
        vf = CB(18 if top.score >= 50 else 16)
        values = wrap(d, glue_units(top.value), vf, avail, 3)
        small = top.label.lower() + (" · " + top.sub if top.sub else "")
        notes = wrap(d, glue_units(small), C(12), avail, 2)
        ink = top.ink
    else:
        vf = CB(16)
        values, notes, ink = [describe(hour.code)[0]], ["nothing unusual"], BLACK
    vh = vf.size + 3
    right_h = len(values) * vh + len(notes) * 14
    col_h = 13 + 4 + 37
    h = max(HERO_ICON, right_h, col_h)
    icons.weather(d, hour_icon(hour.code), x, y + (h - HERO_ICON) // 2, HERO_ICON)
    cx, cy = x + HERO_ICON + 4, y + (h - col_h) // 2
    d.text((cx, cy - _ink_top(CB(13), label)), label, font=CB(13), fill=BLACK)
    d.text((cx, cy + 17 - _ink_top(BOLD(50), temp)), temp, font=BOLD(50), fill=BLACK)
    d.section = "hero.fact"
    ry = y + (h - right_h) // 2
    for v in values:
        d.text((x + SIDE_W, ry), v, font=vf, fill=ink, anchor="ra")
        ry += vh
    for n in notes:
        d.text((x + SIDE_W, ry), n, font=C(12), fill=BLACK, anchor="ra")
        ry += 14
    return y + h


def _facts_table(d, rows, x, y):
    """Two columns: label | value (· sub). Hazards read name | when."""
    d.section = "facts"
    right = x + SIDE_W
    for f in rows:
        base = y + 14  # one baseline per row, whatever the fonts
        if f.hazard:
            d.text((x, base), f.value, font=CB(15), fill=f.ink, anchor="ls")
            d.text((right, base), f.label, font=C(13), fill=BLACK, anchor="rs")
            y += 18
            continue
        vw = d.textlength(f.value, font=CB(15))
        sub = f" · {f.sub}" if f.sub else ""
        sw = d.textlength(sub, font=C(12)) if sub else 0
        lw = d.textlength(f.label, font=C(13))
        own_line = sub and lw + 8 + vw + sw > SIDE_W  # the sub-text drops to a line of its own
        vx = right - vw - (0 if own_line else sw)
        d.text((x, base), fit_text(d, f.label, C(13), vx - x - 8), font=C(13), fill=BLACK, anchor="ls")
        d.text((vx, base), f.value, font=CB(15), fill=f.ink, anchor="ls")
        if sub and not own_line:
            d.text((vx + vw, base), sub, font=C(12), fill=BLACK, anchor="ls")
        y += 18
        if own_line:
            d.text((right, y + 11), sub.lstrip(" ·"), font=C(12), fill=BLACK, anchor="rs")
            y += 14
    return y


# ---------------------------------------------------------------- chart


def _hourly_chart(d, fc, x, y, width, height, now):
    """6 am to 9 pm: rain-chance bars under a temperature line. The top 22 px
    are kept for labels; the bottom 16 px hold the hour labels."""
    d.section = "chart"
    hours = [h for h in fc.hours if 6 <= h.hour <= 21]
    if len(hours) < 2:
        return
    n, step = len(hours), width / len(hours)
    base = y + height - 16
    temps = [h.temp for h in hours]
    probs = [max(0, min(100, h.rain_prob)) for h in hours]
    lo, hi = min(temps), max(temps)
    span = max(4.0, hi - lo)

    def ty(t):
        return y + height - 18 - (t - lo) / span * (height - 40)

    def bar_top(p):
        return base - (p / 100 * (height - 38) if p > 4 else 0)

    def cx(i):
        return x + (i + 0.5) * step

    for i, p in enumerate(probs):
        if p > 4:
            x0 = x + i * step + 2
            d.rectangle([x0, bar_top(p), x0 + step - 5, base], fill=BLUE)
    d.line([(cx(i), ty(t)) for i, t in enumerate(temps)], fill=RED, width=4, joint="curve")
    i_now = next((i for i, h in enumerate(hours) if now is not None and h.hour == now.hour), None)
    if i_now is not None:
        # Dashed, black over the paper and the line but white over a bar:
        # pick the ink per pixel from what is already underneath.
        nx = int(cx(i_now))
        d.marks["now"] = (nx, int(y + 4), int(base))
        for yy in range(int(y + 4), int(base)):
            if (yy - int(y + 4)) % 6 < 3:
                for xx in (nx - 1, nx):
                    d.point((xx, yy), fill=WHITE if d.canvas.getpixel((xx, yy)) == BLUE else BLACK)
        d.polygon([(nx - 5, y), (nx + 5, y), (nx, y + 7)], fill=BLACK)
    d.line([(x, base), (x + width, base)], fill=BLACK, width=2)
    for i, h in enumerate(hours):
        if h.hour % 3 == 0:
            label = "12p" if h.hour == 12 else f"{h.hour % 12}{'a' if h.hour < 12 else 'p'}"
            d.text((_clamp_x(d, cx(i), label, C(12), x, width), y + height - 3), label, font=C(12), fill=BLACK, anchor="ms")

    # Labels: the extremes first (they matter most), then the rain labels.
    # Each tries its own hour, then half a bar and then a whole bar to
    # either side, wherever it is free. A rain label with
    # nowhere free is left out. An extreme is never left out: boxed in above
    # its point (the now-marker's arrowhead shares the top band), it tries
    # the same steps just below the point, and failing that stays put,
    # drawn last and outlined in white.
    avoid = []
    if i_now is not None:  # the marker's dashes and arrowhead
        avoid += [(nx - 2, y, nx + 3, base), (nx - 6, y, nx + 7, y + 8)]
    d.marks["labels"] = placed = []

    def place(i, text, fnt, yy):
        def box_at(px):
            return d.textbbox((px, yy), text, font=fnt, anchor="ms", stroke_width=2)

        home = _clamp_x(d, cx(i), text, fnt, x, width)
        if not any(_overlap(box_at(home), o) for o in avoid):
            return home, False
        for shift in (0.5, 1.0):
            for sign in (1, -1):  # whichever side is free
                px = home + sign * shift * step
                b = box_at(px)
                if b[0] >= x and b[2] <= x + width + 1 and not any(_overlap(b, o) for o in avoid):
                    return px, True
        return None, True

    def keep(text, fnt, px, yy):
        b = d.textbbox((px, yy), text, font=fnt, anchor="ms", stroke_width=2)
        avoid.append(b)
        placed.append((text, b))

    i_lo, i_hi = temps.index(lo), temps.index(hi)
    extremes = []
    for i, t, yy, ink in ((i_lo, _deg(lo), ty(lo) - 6, BLUE), (i_hi, _deg(hi), ty(hi) - 6, RED)):
        px, _ = place(i, t, CB(13), yy)
        if px is None:
            below = yy + 6 + 19  # the label's top just under the line
            px, _ = place(i, t, CB(13), below)
            yy = below if px is not None else yy
        px = _clamp_x(d, cx(i), t, CB(13), x, width) if px is None else px
        keep(t, CB(13), px, yy)
        extremes.append((t, px, yy, ink))
    # Rain: the wettest and the driest hour, each only where a bar is drawn.
    p_max, p_min = max(probs), min(probs)
    wanted = [probs.index(p_max)] if p_max > 4 else []
    if p_min > 4 and probs.index(p_min) not in wanted:
        wanted.append(probs.index(p_min))
    for i in wanted:
        p = probs[i]
        text = f"{p}%"
        inside = base - bar_top(p) >= 14  # white inside the bar's top, else blue above it
        yy = bar_top(p) + 10 if inside else bar_top(p) - 3
        px, moved = place(i, text, CB(10), yy)
        if px is None:
            continue
        keep(text, CB(10), px, yy)
        if inside and not moved:
            # White on its bar. A label is wider than a bar, so a blue halo
            # carries the white past the bar's edges instead of losing them.
            d.text((px, yy), text, font=CB(10), fill=WHITE, anchor="ms", stroke_width=2, stroke_fill=BLUE)
        else:  # above its bar, or nudged off it: blue, outlined so it reads on paper or bar
            d.text((px, yy), text, font=CB(10), fill=BLUE, anchor="ms", stroke_width=2, stroke_fill=WHITE)
    for t, px, yy, ink in extremes:
        d.text((px, yy), t, font=CB(13), fill=ink, anchor="ms", stroke_width=2, stroke_fill=WHITE)


def _overlap(a, b):
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _clamp_x(d, centre, text, fnt, x, width):
    """A centred label's centre, nudged so the label stays inside [x, x + width]."""
    half = d.textlength(text, font=fnt) / 2 + 2
    return min(max(centre, x + half), x + width - half)


# ---------------------------------------------------------------- sun and moon


def _sun_moon(d, fc, x, y, *, now, poster_day, cfg):
    """A thin rule, then '↑5:45am ↓7:35pm' over the day length on the left,
    and the moon's phase on the right, drawn as it looks from this hemisphere."""
    d.section = "sun"
    d.line([(x, y), (x + SIDE_W, y)], fill=BLACK, width=1)
    y += 5
    # DejaVu has a ☀, but as a glyph it can only be one ink: draw the sun.
    icons.sun(d, x + 6, y + 8, 4, ray=(2, 5), ray_w=2, outline=1)
    d.text((x + 16, y), f"↑{_clock(fc.sunrise)} ↓{_clock(fc.sunset)}", font=CB(14), fill=BLACK)
    hrs, mins = divmod(int((fc.sunset - fc.sunrise).total_seconds() // 60), 60)
    d.text((x, y + 17), f"{hrs}h {mins:02d}m daylight", font=C(12), fill=BLACK)
    moment = datetime.combine(poster_day, datetime.min.time()).replace(hour=21, tzinfo=now.tzinfo)
    phase = astro.moon_phase(moment)
    r = 13
    icons.moon(d, x + SIDE_W - r - 1, y + 14, r, phase, southern=cfg.latitude < 0)
    for i, word in enumerate(astro.phase_name(phase).split(" ", 1)):
        d.text((x + SIDE_W - 2 * r - 8, y + 1 + i * 14), word, font=C(13), fill=BLACK, anchor="ra")


# ---------------------------------------------------------------- caption


def _caption_lines(d, photo, *, now, forecast, cfg, poster_day=None, warning=None, cond=None, observed=None):
    """(text, font, ink) lines for the picture's caption, the source stamp,
    and in map mode the list of warnings in force (instead of badges over the
    map)."""
    lines = []
    if warning:
        for t in wrap(d, warning.title, CB(15), SIDE_W, 2):
            lines.append((t, CB(15), RED))
        for t in wrap(d, warning.area or "", C(13), SIDE_W, 2):
            lines.append((t, C(13), BLACK))
        credit = ("Issued " + warning.issued + " · " if warning.issued else "") + warning.credit
        lines.append((fit_text(d, credit, C(12), SIDE_W), C(12), BLACK))
        for text, ink in warning_lines(cond, warning):
            for t in wrap(d, text, C(13), SIDE_W, 2):
                lines.append((t, C(13), ink))
    elif photo is not None:
        for t in wrap(d, photo.title, CI(15), SIDE_W, 2):
            lines.append((t, CI(15), BLACK))
        lines.append((fit_text(d, photo.credit, C(12), SIDE_W), C(12), BLACK))
    # No clock time here: it would change the picture (and force a 40 s
    # refresh) every hour even when nothing else did.
    day_fc = forecast.day(poster_day) if forecast else None
    source = day_fc.source if day_fc is not None and day_fc.source else (forecast.source if forecast else "")
    station = observed.credit if observed is not None else ""  # whose gauge the rain figures come from
    stamp, colour = " · ".join(p for p in (cfg.place, source, station) if p), BLACK
    if forecast is not None:
        age_h = (now.timestamp() - forecast.fetched_at) / 3600
        if age_h >= 3:
            # Coarse buckets: an hourly-changing "N h old" would force a 40 s
            # refresh every hour of an outage.
            age = "a day" if age_h >= 24 else "12 h" if age_h >= 12 else "3 h"
            stamp, colour = f"{cfg.place} · forecast over {age} old", RED
    lines.append((fit_text(d, stamp, C(11), SIDE_W), C(11), colour))
    return lines


def _fit_caption(lines, y_min):
    """Bottom-aligned; drop lines from the end of the warnings list (never
    the stamp) rather than overlap the panel above y_min."""
    lines = list(lines)
    while len(lines) > 2 and H - 6 - sum(f.size + 3 for _, f, _ in lines) - 4 < y_min + 4:
        del lines[-2]
    return lines


def _draw_caption(d, lines, x, rule_y):
    d.line([(x, rule_y), (x + SIDE_W, rule_y)], fill=BLACK, width=1)
    y = rule_y + 4
    for text, f, ink in lines:
        d.text((x, y), text, font=f, fill=ink)
        y += f.size + 3



_LEVEL_WORDS = {"emergency": "emergency warning", "watch": "watch and act", "advice": "advice"}


def list_text(h):
    """Sentence-case line for the text panel: "Storm advice: Western Sydney"."""
    if h.titled:
        return h.where + (f": {h.detail}" if h.detail else "")
    where = h.where if h.km in (None, 0) else f"{h.where} ({h.km:.0f} km)"
    return f"{h.kind.capitalize()} {_LEVEL_WORDS[h.level]}: {where}"


def warning_lines(cond, warning):
    """Text lines for the warnings in force besides the mapped one, specific
    first, then a one-line digest of the broad notices."""
    if cond is None:
        return []
    out = []
    for h in cond.hazards or []:
        if h.specific and h.where != warning.title:
            out.append(("• " + list_text(h), RED if h.level == "emergency" else BLACK))
    broad = [list_text(h) for h in (cond.hazards or []) if not h.specific]
    if cond.fire_ban:
        broad.insert(0, "Total fire ban")
    if cond.unavailable:
        broad.append("no live data: " + ", ".join(cond.unavailable))
    if broad:
        out.append(("Also: " + " · ".join(broad), BLACK))
    return out


# ---------------------------------------------------------------- badges

# AFDRS colours: (fill, text)
_FIRE = {
    "MODERATE": (GREEN, WHITE),
    "HIGH": (YELLOW, BLACK),
    "EXTREME": (ORANGE, BLACK),
    "CATASTROPHIC": (RED, WHITE),
}


# Australian Warning System colours: (fill, text)
_AWS = {"emergency": (RED, WHITE), "watch": (ORANGE, BLACK), "advice": (YELLOW, BLACK)}
_AWS_WORDS = {"emergency": "EMERGENCY", "watch": "WATCH AND ACT", "advice": "ADVICE"}
MAX_BADGES = 4


def hazard_text(h):
    if h.titled:
        return h.where.upper()  # e.g. "SEVERE THUNDERSTORM WARNING"
    where = h.where if h.km in (None, 0) else f"{h.where} {h.km:.0f} km"
    return f"{h.kind.upper()} · {_AWS_WORDS[h.level]} · {where}"


class Badge(tuple):
    """(text, fill, ink); .small marks a region-wide notice, drawn smaller."""

    small = False


def _badge(text, fill, ink, small=False):
    b = Badge((text, fill, ink))
    b.small = small
    return b


def badges_for(cond):
    """[(text, fill, ink)] for the conditions worth showing, most urgent first.

    Emergency warnings, then a total fire ban, then the other hazards by level
    and distance, then fire danger (only from HIGH: MODERATE is most of the
    season), then smoke. At most MAX_BADGES; the last says how many more."""
    out = []
    if cond is None:
        return out
    hazards = list(cond.hazards or [])
    shown = cond.warning.title if getattr(cond, "warning", None) else None
    for h in [h for h in hazards if h.level == "emergency"]:
        out.append(_badge(hazard_text(h), *_AWS["emergency"]))
    # Specific hazards before every broad notice, so a fire ban can never push
    # a nearby fire out of the (short, over a map) list.
    for h in [h for h in hazards if h.level != "emergency" and h.specific]:
        if h.titled:  # the warning system's colours don't apply to titled warnings
            if h.where != shown:  # the map and caption already say this one
                out.append(_badge(hazard_text(h), WHITE, RED))
        else:
            out.append(_badge(hazard_text(h), *_AWS[h.level]))
    if cond.unavailable:  # say so, rather than look like a quiet day
        out.append(_badge("NO LIVE HAZARD DATA: " + ", ".join(cond.unavailable), WHITE, BLACK, small=True))
    if cond.fire_ban:
        out.append(_badge("TOTAL FIRE BAN", RED, WHITE, small=True))
    for h in [h for h in hazards if h.level != "emergency" and not h.specific]:
        out.append(_badge(hazard_text(h), WHITE, BLACK, small=True))  # region-wide notices
    if cond.fire_rating and cond.fire_rating != "MODERATE":
        fill, ink = _FIRE[cond.fire_rating]
        out.append(_badge(f"FIRE DANGER  {cond.fire_rating}", fill, ink, small=True))
    if cond.aqi is not None:
        fill = RED if cond.aqi >= 151 else ORANGE
        # Category only: the exact number drifts every fetch and would force refreshes.
        out.append(_badge(f"AIR QUALITY {cond_mod.aqi_label(cond.aqi).upper()}", fill, WHITE if fill == RED else BLACK, small=True))
    cap = 2 if shown else MAX_BADGES  # over a map, keep most of it visible
    if len(out) > cap:
        extra = len(out) - (cap - 1)
        out = out[: cap - 1] + [_badge(f"+{extra} MORE · HAZARDS NEAR ME", WHITE, BLACK, small=True)]
    return out


def draw_badges(canvas, cond, x=12, y=12):
    """Pills over the photo's top-left corner, with a white halo so they read
    against any dithered background."""
    d = ImageDraw.Draw(canvas)
    if getattr(cond, "warning", None):
        y = H - 12 - 2 * 34  # over a map, sit at the bottom-left, clear of its title
    for badge in badges_for(cond):
        text, fill, ink = badge
        f = BOLD(12) if badge.small else BOLD(15)
        h = 22 if badge.small else 28
        text = fit_text(d, text, f, PHOTO_W - 2 * x - 22)
        w = int(d.textlength(text, font=f)) + 18
        d.rounded_rectangle([x - 3, y - 3, x + w + 3, y + h + 3], radius=h // 2 + 2, fill=WHITE)
        d.rounded_rectangle([x, y, x + w, y + h], radius=h // 2, fill=fill, outline=BLACK, width=2)
        d.text((x + 9, y + (3 if badge.small else 5)), text, font=f, fill=ink)
        y += h + (6 if badge.small else 10)


def render(*, now, poster_day, is_tomorrow, forecast, photo, cfg, cond=None, layout=None, observed=None,
           radar=False):
    """The poster. Pass a dict as `layout` to get back what draw_side used
    (section boxes, the caption's top, the facts). In radar mode `radar` is
    the domain.RadarImage (or None while waiting for one): it takes the
    picture area, with the badges over it, and the photo isn't shown."""
    canvas = new_canvas((W, H), WHITE)
    warning = getattr(cond, "warning", None) if cond else None
    set_layout(500)
    if radar is not False:
        photo, warning = _draw_radar(canvas, radar, cond, cfg, now), None
    elif warning:
        try:
            set_layout(draw_warning_map(canvas, warning))
        except Exception as e:  # a bad map must never take the poster down
            print(f"warning map unusable ({e!r}); showing the photo", flush=True)
            warning = None
            cond.warning = None
            set_layout(500)
    if not warning and radar is False:
        draw_photo(canvas, photo)
        draw_badges(canvas, cond)  # over a map, warnings are listed in the text panel instead
    day_fc = forecast.day(poster_day) if forecast else None
    used = draw_side(canvas, day_fc, now=now, poster_day=poster_day, is_tomorrow=is_tomorrow, forecast=forecast, photo=photo, cfg=cfg,
              warning=warning, cond=cond, observed=observed)
    if layout is not None:
        layout.update(used, side=(SIDE_X, SIDE_W), photo_w=PHOTO_W)
    return canvas


def _draw_radar(canvas, radar, cond, cfg, now):
    """Radar mode's picture: the radar, the badges over it (a warning's map
    gives way to the radar, so its badge shows), and a stand-in photo whose
    title and credit caption the radar."""
    from dataclasses import replace
    from types import SimpleNamespace
    from zoneinfo import ZoneInfo

    from . import radar_poster

    tz = ZoneInfo(cfg.timezone)
    radar_poster.draw(canvas, radar, size=(PHOTO_W, H), home=(cfg.latitude, cfg.longitude), tz=tz, now=now, font=BOLD)
    draw_badges(canvas, replace(cond, warning=None) if cond is not None else None)
    title, credit = radar_poster.caption(radar, tz)
    return SimpleNamespace(title=title, credit=credit)
