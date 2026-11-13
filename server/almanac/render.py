"""Compose the 800x480 poster as an indexed image whose indices are panel inks.

Left: the landscape photo of the day, dithered to the seven inks.
Right: date, the day's weather, an hourly temperature/rain strip, sun and moon,
and the photo's caption and credit. Everything on the right is drawn with solid inks
so text stays crisp.
"""

from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance, ImageFont

from . import astro, conditions as cond_mod, icons, weather
from .dither import atkinson, nearest
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


def wrap(d, text, fnt, width, lines):
    words, out, cur = text.split(), [], ""
    for w in words:
        trial = (cur + " " + w).strip()
        if d.textlength(trial, font=fnt) <= width:
            cur = trial
        else:
            if cur:
                out.append(cur)
            cur = w
    if cur:
        out.append(cur)
    if len(out) > lines:
        out = out[: lines - 1] + [fit_text(d, " ".join(out[lines - 1 :]), fnt, width)]
    return [fit_text(d, line, fnt, width) for line in out]


def time12(dt):
    return dt.strftime("%I:%M").lstrip("0") + dt.strftime(" %p").lower()


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


MAP_LAND_INK = None  # land stays white: the map's blue labels need the contrast


def draw_warning_map(canvas, warning):
    """A BOM warning's threat-area map in place of the photo: fitted whole
    (it is a map, so no cropping) and mapped to the nearest inks without
    dithering, which keeps its labels and borders crisp."""
    import io

    img = Image.open(io.BytesIO(warning["png"])).convert("RGB")
    a = img.width / img.height
    w, h = (PHOTO_W, int(PHOTO_W / a)) if a > PHOTO_W / H else (int(H * a), H)
    img = img.resize((w, h), Image.LANCZOS)
    canvas.paste(nearest(img, INK_RGB, _flat(PNG_RGB), water_ink=BLUE, land_ink=MAP_LAND_INK), ((PHOTO_W - w) // 2, (H - h) // 2))


def _placeholder_photo(d):
    """Concentric sunrise bands, for when no photo could be fetched."""
    bands = [BLUE, GREEN, YELLOW, ORANGE, RED]
    cx, cy = PHOTO_W // 2, H + 40
    for i, c in enumerate(bands):
        r = 520 - i * 90
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=c)


# ---------------------------------------------------------------- side panel


def draw_side(canvas, day_fc, *, now, poster_day, is_tomorrow, forecast, photo, cfg, warning=None):
    d = ImageDraw.Draw(canvas)
    x = SIDE_X
    y = 14
    if is_tomorrow:
        d.text((x, y), "TOMORROW", font=BOLD(14), fill=ORANGE)
        y += 18
    d.text((x, y), poster_day.strftime("%A"), font=BOLD(34), fill=BLACK)
    y += 40
    d.text((x, y), poster_day.strftime("%-d %B"), font=SANS(20), fill=BLACK)
    y += 30
    d.line([(x, y), (x + SIDE_W, y)], fill=BLACK, width=2)
    y += 10

    if day_fc is None:
        d.text((x, y + 10), "No forecast yet", font=BOLD(20), fill=RED)
        d.text((x, y + 40), "Waiting for Open-Meteo…", font=SANS(15), fill=BLACK)
        y += 230
    else:
        y = _weather_block(d, day_fc, x, y)
        y = _hourly_chart(d, day_fc, x, y, now=None if is_tomorrow else now)
        y = _sun_moon(d, day_fc, x, y, now=now, poster_day=poster_day, cfg=cfg)

    _caption(d, photo, x, now=now, forecast=forecast, cfg=cfg, poster_day=poster_day, warning=warning)


def _weather_block(d, fc, x, y):
    icons.weather(d, fc.icon, x - 4, y, 92)
    tx = x + 100
    hi = f"{round(fc.t_max)}°"
    d.text((tx, y - 6), hi, font=BOLD(54), fill=RED)
    hw = d.textlength(hi, font=BOLD(54))
    d.text((tx + hw + 6, y + 22), f"{round(fc.t_min)}°", font=BOLD(28), fill=BLUE)
    # BOM's précis can be a sentence ("Shower or two. Possible storm."): wrap it.
    lines = wrap(d, fc.label, SANS(15), SIDE_W - 100, 2)
    for i, line in enumerate(lines):
        d.text((tx, y + 56 + i * 18 - (9 if len(lines) == 1 else 0)), line, font=SANS(15 if len(lines) > 1 else 17), fill=BLACK)
    y += 98
    rain = f"Rain {fc.rain_prob}%"
    if fc.rain_text:  # BOM's range, e.g. "0 to 2 mm"
        if fc.rain_text not in ("0 mm", "0 to 0 mm"):
            rain += f" · {fc.rain_text}"
    elif fc.rain_mm >= 0.2:
        rain += f" · {fc.rain_mm:.0f} mm" if fc.rain_mm >= 1 else f" · {fc.rain_mm:.1f} mm"
    d.text((x, y), rain, font=BOLD(16), fill=BLUE if fc.rain_prob >= 30 else BLACK)
    rw = d.textlength(rain, font=BOLD(16))
    uv = f"UV {fc.uv:.0f} {weather.uv_label(fc.uv)}"
    if rw + 12 + d.textlength(uv, font=BOLD(16)) > SIDE_W:
        uv = f"UV {fc.uv:.0f}"
    uw = d.textlength(uv, font=BOLD(16))
    d.text((x + SIDE_W - uw, y), uv, font=BOLD(16), fill=ORANGE if fc.uv >= 6 else BLACK)
    return y + 28


def _hourly_chart(d, fc, x, y, now):
    hours = [h for h in fc.hours if 6 <= h.hour <= 21]
    top, height, width = y + 14, 78, SIDE_W
    base = top + height
    if len(hours) < 2:
        return base + 26
    n = len(hours)
    step = width / n
    temps = [h.temp for h in hours]
    lo, hi = min(temps), max(temps)
    span = max(4.0, hi - lo)
    lo -= (span - (hi - lo)) / 2

    def ty(t):
        return base - 8 - (t - lo) / span * (height - 26)

    for i, h in enumerate(hours):
        if h.rain_prob > 0:
            bh = max(2, h.rain_prob / 100 * (height - 4))
            x0 = x + i * step + 2
            d.rectangle([x0, base - bh, x0 + step - 4, base], fill=BLUE)
    pts = [(x + (i + 0.5) * step, ty(h.temp)) for i, h in enumerate(hours)]
    d.line(pts, fill=RED, width=4, joint="curve")
    # Label the warmest and coolest hour.
    i_hi = temps.index(max(temps))
    i_lo = temps.index(min(temps))
    i_now = None if now is None else now.hour - hours[0].hour
    # The warmest label goes below the line when the now-marker sits on it.
    for i, above in ((i_hi, i_hi != i_now), (i_lo, False)):
        label = f"{round(temps[i])}°"
        lw = d.textlength(label, font=BOLD(13))
        lx = min(max(pts[i][0] - lw / 2, x), x + width - lw)
        ly = pts[i][1] - 19 if above else pts[i][1] + 5
        if not above and ly + 14 > base and i != i_hi:
            ly = pts[i][1] - 19
        if i == i_lo and i_lo == i_hi:
            break
        d.text((lx, ly), label, font=BOLD(13), fill=BLACK)
    if now is not None and 6 <= now.hour <= 21:
        i = now.hour - hours[0].hour
        if 0 <= i < n:
            nx = x + (i + 0.5) * step
            for yy in range(int(top - 6), int(base), 6):
                d.line([(nx, yy), (nx, yy + 3)], fill=BLACK, width=2)
            d.polygon([(nx - 5, top - 10), (nx + 5, top - 10), (nx, top - 3)], fill=BLACK)
    d.line([(x, base), (x + width, base)], fill=BLACK, width=2)
    for i, h in enumerate(hours):
        if h.hour % 3 == 0:
            label = {0: "12a", 12: "12p"}.get(h.hour, f"{h.hour % 12}{'a' if h.hour < 12 else 'p'}")
            lw = d.textlength(label, font=SANS(12))
            d.text((x + (i + 0.5) * step - lw / 2, base + 3), label, font=SANS(12), fill=BLACK)
    return base + 24


def _sun_moon(d, fc, x, y, *, now, poster_day, cfg):
    d.line([(x, y), (x + SIDE_W, y)], fill=BLACK, width=1)
    y += 8
    icons.sun(d, x + 10, y + 10, 7, rays=False)
    d.text((x + 26, y), f"{time12(fc.sunrise)} – {time12(fc.sunset)}", font=BOLD(16), fill=BLACK)
    daylight = fc.sunset - fc.sunrise
    hrs, mins = divmod(int(daylight.total_seconds() // 60), 60)
    span = f"{hrs}h {mins:02d}m"
    d.text((x + SIDE_W - d.textlength(span, font=SANS(14)), y + 2), span, font=SANS(14), fill=BLACK)

    y += 24
    moment = datetime.combine(poster_day, datetime.min.time()).replace(hour=21, tzinfo=now.tzinfo)
    phase = astro.moon_phase(moment)
    icons.moon(d, x + 10, y + 10, 8, phase, southern=cfg.latitude < 0)
    pct = round(astro.illumination(phase) * 100)
    d.text((x + 26, y + 1), f"{astro.phase_name(phase)} · {pct}% lit", font=SANS(14), fill=BLACK)
    return y + 26


def _caption(d, photo, x, *, now, forecast, cfg, poster_day=None, warning=None):
    y = H - 62
    d.line([(x, y), (x + SIDE_W, y)], fill=BLACK, width=1)
    y += 6
    if warning:
        d.text((x, y), fit_text(d, warning["title"], BOLD(14), SIDE_W), font=BOLD(14), fill=RED)
        credit = ("Issued " + warning["issued"] + " · " if warning.get("issued") else "") + "Map: Bureau of Meteorology"
        d.text((x, y + 19), fit_text(d, credit, SANS(12), SIDE_W), font=SANS(12), fill=BLACK)
    elif photo is not None:
        d.text((x, y), fit_text(d, photo.title, ITALIC(15), SIDE_W), font=ITALIC(15), fill=BLACK)
        d.text((x, y + 19), fit_text(d, photo.credit, SANS(12), SIDE_W), font=SANS(12), fill=BLACK)
    # No clock time here: it would change the picture (and force a 40 s
    # refresh) every hour even when nothing else did.
    day_fc = forecast.day(poster_day) if forecast else None
    source = "BOM · Open-Meteo" if day_fc is not None and day_fc.source == "BOM" else "Open-Meteo"
    stamp = f"{cfg.place} · {source}"
    colour = BLACK
    if forecast is not None:
        age_h = (now.timestamp() - forecast.fetched_at) / 3600
        if age_h >= 3:
            # Coarse buckets: an hourly-changing "N h old" would force a 40 s
            # refresh every hour of an outage.
            age = "a day" if age_h >= 24 else "12 h" if age_h >= 12 else "3 h"
            stamp = f"{cfg.place} · forecast over {age} old"
            colour = RED
    d.text((x, y + 36), stamp, font=SANS(11), fill=colour)


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
    if h.source == "BOM":
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
    shown = cond.warning["title"] if getattr(cond, "warning", None) else None
    for h in [h for h in hazards if h.level == "emergency"]:
        out.append(_badge(hazard_text(h), *_AWS["emergency"]))
    if cond.fire_ban:
        out.append(_badge("TOTAL FIRE BAN", RED, WHITE, small=True))
    for h in [h for h in hazards if h.level != "emergency" and h.specific]:
        if h.source == "BOM":  # the warning system's colours don't apply to BOM warnings
            if h.where != shown:  # the map and caption already say this one
                out.append(_badge(hazard_text(h), WHITE, RED))
        else:
            out.append(_badge(hazard_text(h), *_AWS[h.level]))
    if cond.unavailable:  # say so, rather than look like a quiet day
        out.append(_badge("NO LIVE HAZARD DATA: " + ", ".join(cond.unavailable), WHITE, BLACK, small=True))
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


def render(*, now, poster_day, is_tomorrow, forecast, photo, cfg, cond=None):
    canvas = new_canvas((W, H), WHITE)
    warning = getattr(cond, "warning", None) if cond else None
    if warning:
        draw_warning_map(canvas, warning)
    else:
        draw_photo(canvas, photo)
    draw_badges(canvas, cond)
    day_fc = forecast.day(poster_day) if forecast else None
    draw_side(canvas, day_fc, now=now, poster_day=poster_day, is_tomorrow=is_tomorrow, forecast=forecast, photo=photo, cfg=cfg,
              warning=warning)
    return canvas
