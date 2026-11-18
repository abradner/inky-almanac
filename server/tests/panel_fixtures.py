"""Side-panel fixtures: the panel lab's presets (take A2's mockup) turned into
Open-Meteo payloads, conditions and synthetic pictures, so tests and the
stills script render exactly the same posters.

Every place here is central Sydney (the default Config), never a real home.
"""

import functools
import io
import math
import random
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw

from almanac import conditions, hazards as hz, schedule
from almanac.domain import MapImage
from almanac.providers import commons as photos, openmeteo as weather

SYD = ZoneInfo("Australia/Sydney")

# The mockup's PRESETS, verbatim where they apply. windFrom/windTo index
# N, NE, E, SE, S, SW, W, NW; "hazard" is the badge over the photo.
_DEFAULTS = {"windFrom": 1, "windTo": 1, "date": "2026-11-14", "longTitle": True}
PRESETS = {
    "worstPhoto": {"date": "2026-09-30", "hour": 17, "tMin": 29, "tMax": 46, "tMinT": 31, "tMaxT": 47, "cond": "hail",
                   "dayCond": "storm", "rainP": 100, "rainMm": 88, "since9": 248, "lastHr": 96, "wind": 98, "gust": 148,
                   "windFrom": 7, "windTo": 4, "uv": 13,
                   "precis": "Very hot. Severe thunderstorms with giant hail, damaging winds and heavy rain likely in the afternoon and evening",
                   "dayRain": 100, "rainRange": "40 to 100 mm", "mapMode": False, "longTitle": True, "hazard": "fire"},
    "worstMap": {"date": "2026-09-30", "hour": 17, "tMin": 29, "tMax": 46, "tMinT": 31, "tMaxT": 47, "cond": "hail",
                 "dayCond": "storm", "rainP": 100, "rainMm": 88, "since9": 248, "lastHr": 96, "wind": 98, "gust": 148,
                 "windFrom": 7, "windTo": 4, "uv": 13,
                 "precis": "Very hot. Severe thunderstorms with giant hail, damaging winds and heavy rain likely in the afternoon and evening",
                 "dayRain": 100, "rainRange": "40 to 100 mm", "mapMode": True, "longTitle": True, "hazard": "none"},
    "worstCold": {"date": "2026-06-24", "hour": 7, "tMin": -4, "tMax": 9, "tMinT": -3, "tMaxT": 8, "cond": "snow",
                  "dayCond": "snow", "rainP": 100, "rainMm": 12, "since9": 0, "lastHr": 3, "wind": 46, "gust": 92,
                  "windFrom": 4, "windTo": 6, "uv": 1,
                  "precis": "Snow showers above 800 metres. Bitterly cold with damaging westerly winds",
                  "dayRain": 100, "rainRange": "10 to 25 mm", "mapMode": False, "longTitle": True, "hazard": "none"},
    # A normal day (the lab's "Hot afternoon"), a quiet morning, and an evening.
    "afternoon": {"hour": 14, "tMin": 18, "tMax": 34, "tMinT": 20, "tMaxT": 31, "cond": "clear", "rainP": 5, "rainMm": 0,
                  "wind": 14, "gust": 24, "uv": 10, "dayCond": "clear", "since9": 0, "lastHr": 0, "precis": "Sunny. Hot",
                  "dayRain": 5, "mapMode": False, "hazard": "ban"},
    "morning": {"hour": 7, "tMin": 9, "tMax": 22, "tMinT": 11, "tMaxT": 24, "cond": "partly", "rainP": 10, "rainMm": 0,
                "wind": 8, "gust": 15, "uv": 2, "dayCond": "partly", "since9": 0, "lastHr": 0, "precis": "Partly cloudy",
                "dayRain": 10, "mapMode": False, "hazard": "none"},
    "night": {"hour": 21, "tMin": 16, "tMax": 30, "tMinT": 15, "tMaxT": 26, "cond": "cloudy", "rainP": 30, "rainMm": 0,
              "wind": 12, "gust": 20, "uv": 0, "dayCond": "showers", "since9": 0.4, "lastHr": 0,
              "precis": "Cloudy. Shower or two", "dayRain": 40, "rainRange": "0 to 2 mm", "mapMode": False,
              "hazard": "none"},
    # A hot afternoon with a southerly change seen at the coast, not yet
    # home, and a station's gauge for the rain so far (observed.Observed).
    "southerly": {"hour": 15, "tMin": 21, "tMax": 38, "tMinT": 18, "tMaxT": 25, "cond": "partly", "rainP": 20,
                  "rainMm": 0, "wind": 22, "gust": 35, "windFrom": 7, "windTo": 7, "uv": 9, "dayCond": "partly",
                  "since9": 0, "lastHr": 0, "precis": "Hot. Gusty southerly change late afternoon", "dayRain": 30,
                  "mapMode": False, "hazard": "none", "observed": "coast"},
    # A bitter winter morning for the README's extremes: −10° with sleet and
    # snow (no WMO sleet code, so the snow sky) and a damaging westerly.
    "bitter": {"date": "2026-07-15", "hour": 7, "tMin": -10, "tMax": 2, "tMinT": -8, "tMaxT": 4, "cond": "snow",
               "dayCond": "snow", "rainP": 90, "rainMm": 6, "since9": 0, "lastHr": 2, "wind": 55, "gust": 100,
               "windFrom": 6, "windTo": 6, "uv": 1,
               "precis": "Bitterly cold. Sleet and snow showers. Damaging westerly winds", "dayRain": 90,
               "rainRange": "5 to 15 mm", "mapMode": False, "longTitle": False, "hazard": "none"},
    # A stormy early afternoon to sit beside the recorded heavy-rain radar
    # frame (16 Nov 2026) and a warning map in the README posters.
    "storm": {"date": "2026-11-16", "hour": 13, "tMin": 19, "tMax": 27, "tMinT": 17, "tMaxT": 24, "cond": "storm",
              "dayCond": "storm", "rainP": 90, "rainMm": 9, "since9": 24, "lastHr": 8, "wind": 28, "gust": 52,
              "windFrom": 3, "windTo": 4, "uv": 3, "precis": "Showers and storms. Heavy falls possible",
              "dayRain": 95, "rainRange": "15 to 40 mm", "mapMode": False, "longTitle": False, "hazard": "none"},
}

WORST = ("worstPhoto", "worstMap", "worstCold")

# The mockup's sky words -> WMO codes. "sunstorm" (sunny, storms later) is
# a clear coming hour with storms forecast later in the day.
CODES = {"clear": 0, "partly": 2, "cloudy": 3, "fog": 45, "haze": 1, "drizzle": 53, "showers": 80, "rain": 63,
         "snow": 73, "sunstorm": 1, "storm": 95, "hail": 96}


def preset(name, map_mode=None):
    s = dict(_DEFAULTS, **PRESETS[name])
    if map_mode is not None:
        s["mapMode"] = map_mode
    return s


def temp_at(s, h):
    """The mockup's tempAt: minimum at 5 am, maximum at 3 pm (h counts hours
    from today's midnight; 24+ is tomorrow)."""
    day = (s["tMin"], s["tMax"]) if h < 24 else (s["tMinT"], s["tMaxT"])
    hh = h % 24
    if 5 <= hh <= 15:
        return day[0] + (day[1] - day[0]) * (1 - math.cos(math.pi * (hh - 5) / 10)) / 2
    next_min = (s["tMinT"] if hh > 15 else s["tMin"]) if h < 24 else s["tMinT"]
    frm = day[1] if hh > 15 else (s["tMax"] if h < 24 else s["tMaxT"])
    t = (hh - 15) / 14 if hh > 15 else (hh + 9) / 14
    return frm + (next_min - frm) * (1 - math.cos(math.pi * t)) / 2


def now_of(s):
    d = date.fromisoformat(s["date"])
    return datetime(d.year, d.month, d.day, s["hour"], 1, tzinfo=SYD)


def payload(s):
    """An Open-Meteo response for the preset: today, tomorrow and the day after."""
    start = date.fromisoformat(s["date"])
    hour = s["hour"]
    nxt = hour + 1
    times, temps, probs, codes, mm, wind, gust, wdir, uv = ([] for _ in range(9))
    since_hours = list(range(10, hour + 1))  # hourly totals that make up "since 9am"
    for k in range(72):
        h = k % 24
        times.append((datetime.combine(start, datetime.min.time()) + timedelta(hours=k)).strftime("%Y-%m-%dT%H:%M"))
        temps.append(round(temp_at(s, min(k, 47)), 1))
        p = round(max(0, min(100, s["dayRain"] + 30 * math.sin((h - 10) / 3))))
        code = CODES[s["dayCond"]]
        rain, w, g, direction = 0.0, s["wind"] * 0.6, s["gust"] * 0.6, s["windFrom"] * 45
        if k < 24 and s["since9"] and h in since_hours:
            rest = s["since9"] - s["lastHr"]
            rain = s["lastHr"] if h == hour else rest / max(1, len(since_hours) - 1)
        if k == hour:
            w, g = s["wind"] * 0.8, s["gust"] * 0.8
        if k == nxt:
            p, code, rain, w, g = s["rainP"], CODES[s["cond"]], s["rainMm"], s["wind"], s["gust"]
            direction = s["windTo"] * 45
        if s["cond"] == "sunstorm" and k > nxt and k < 24 and 14 <= h <= 19:
            code = 95
        u = s["uv"] if k == nxt else max(0.0, s["uv"] * math.sin(math.pi * (h - 6) / 13)) if 6 <= h <= 19 else 0.0
        probs.append(p)
        codes.append(code)
        mm.append(round(rain, 2))
        wind.append(round(w))
        gust.append(round(g))
        wdir.append(direction)
        uv.append(round(u, 1))
    days = [(start + timedelta(days=i)).isoformat() for i in range(3)]
    return {
        "current": {"temperature_2m": temps[hour], "weather_code": codes[hour]},
        "hourly": {"time": times, "temperature_2m": temps, "precipitation_probability": probs, "weather_code": codes,
                   "precipitation": mm, "wind_speed_10m": wind, "wind_gusts_10m": gust, "wind_direction_10m": wdir,
                   "uv_index": uv},
        "daily": {
            "time": days,
            "weather_code": [CODES[s["dayCond"]]] * 3,
            "temperature_2m_max": [s["tMax"], s["tMaxT"], s["tMaxT"]],
            "temperature_2m_min": [s["tMin"], s["tMinT"], s["tMinT"]],
            "sunrise": [f"{d}T05:45" for d in days],
            "sunset": [f"{d}T19:35" for d in days],
            "uv_index_max": [s["uv"]] * 3,
            "precipitation_probability_max": [s["dayRain"]] * 3,
            "precipitation_sum": [s["since9"]] * 3,
        },
    }


def forecast(s):
    fc = weather.parse(payload(s), now_of(s).timestamp() - 600)
    for day in fc.days.values():  # BOM's précis and rain, as apply_outlook would set them
        day.label_override = s["precis"]
        day.rain_chance = s["dayRain"]
        day.rain_text = s.get("rainRange")
        day.source = "BOM · Open-Meteo"
    if s["dayCond"] == "sunstorm":
        for day in fc.days.values():
            day.icon_override = "storm"
    return fc


@functools.lru_cache(maxsize=1)
def landscape(w=750, h=500, seed=7):
    """A synthetic sky / ridge / forest / meadow scene (the mockup's
    makePhoto, at photo resolution) for the dithered picture."""
    img = Image.new("RGB", (w, h))
    px = img.load()
    rnd = random.Random(seed)
    for y in range(h):
        for x in range(w):
            fx, fy = x * 250 / w, y * 240 / h
            ridge = 120 + 30 * math.sin(fx / 23) + 14 * math.sin(fx / 7 + 1)
            ridge2 = 150 + 20 * math.sin(fx / 31 + 2)
            if fy < ridge:
                t = fy / ridge
                c = (int(90 + 120 * t), int(150 + 80 * t), 235)
            elif fy < ridge2:
                c = (70, 90, 150)
            elif fy < 190:
                c = (30, int(110 - (fy - ridge2)), 50)
            else:
                c = (90 + int(40 * math.sin(fx / 9)), 150, 50)
            n = rnd.randint(-12, 12)
            px[x, y] = tuple(max(0, min(255, v + n)) for v in c)
    return img


def photo(s):
    title = ("Front view of a wooden footbridge over a lagoon, trees and mountains in Vang Vieng"
             if s.get("longTitle") else "Moraine Lake")
    return photos.Photo("fixture", title, "Photo: Basile Morin · CC BY-SA 4.0", landscape())


def commons_photo():
    """A real photo for the README posters: "Dülmen, Umland -- 2014 -- 7056"
    by Dietmar Rabich, Wikimedia Commons, CC BY-SA 4.0. Committed already
    dithered to the inks (cut from an earlier README poster), at the photo
    area's 500 x 480, so it re-renders unchanged."""
    from pathlib import Path

    path = Path(__file__).resolve().parent / "fixtures" / "photos" / "commons-duelmen-umland.png"
    return photos.Photo("commons-duelmen-umland", "Dülmen, Umland", "Photo: Dietmar Rabich · CC BY-SA 4.0",
                        Image.open(path).convert("RGB"))


def map_png(w=700, h=750, label=True):
    """A synthetic warning map in BOM's colours (fitted to 448 x 480).
    `label=False` leaves out the agency name, for illustrative stills that
    must not pass for a real issued map."""
    img = Image.new("RGB", (w, h), (255, 242, 211))
    d = ImageDraw.Draw(img)
    d.polygon([(w, 0), (int(w * 0.74), 0), (int(w * 0.67), int(h * 0.62)), (int(w * 0.56), h), (w, h)], fill=(219, 237, 255))
    d.ellipse([200, 100, 420, 520], fill=(255, 220, 0))
    d.ellipse([280, 280, 400, 420], fill=(250, 170, 30))
    d.ellipse([300, 320, 350, 380], fill=(230, 50, 50))
    for x1, y1, x2, y2 in ((0, 250, 470, 220), (90, 0, 190, 750), (0, 520, 440, 560)):
        d.line([(x1, y1), (x2, y2)], fill=(128, 128, 128), width=2)
    if label:
        d.text((10, 10), "Bureau of Meteorology", fill=(0, 0, 0))
    for name, x, y in (("Chatswood", 356, 410), ("Ashfield", 325, 456), ("Sydney", 360, 515), ("Strathfield", 234, 437)):
        d.rectangle([x - 3, y - 3, x + 2, y + 2], fill=(0, 0, 255))
        d.text((x + 6, y - 6), name, fill=(0, 0, 255))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def conds(s):
    """Conditions for the preset. Map mode carries the worst-case warnings
    list: the mapped warning, two more specific hazards, broad notices, a fire
    ban and a feed outage, so the caption has to drop lines."""
    if s["mapMode"]:
        w = MapImage(png=map_png(), title="Severe Thunderstorm Warning", area="Sydney Metropolitan and Hawkesbury",
             issued="5:38 pm", credit="Map: Bureau of Meteorology")
        hs = [
            hz.Hazard("storm", "watch", w.title, None, "BOM", ident="BOM:storm", detail=w.area, titled=True),
            hz.Hazard("fire", "emergency", "Lane Cove National Park", 6, "RFS", ident="RFS:1"),
            hz.Hazard("flood", "watch", "Cooks River at Canterbury", 0, "SES", ident="SES:1"),
            hz.Hazard("storm", "advice", "Western Sydney and Blue Mountains", None, "SES",
                      specific=False, ident="SES:2"),
            hz.Hazard("flood", "advice", "Flood Watch", None, "BOM", specific=False, ident="BOM:fw",
                      detail="Hawkesbury-Nepean", titled=True),
        ]
        return conditions.Conditions(hazards=hs, alert=hz.alert_id(hs), warning=w, fire_ban=True, unavailable=["SES"])
    if s["hazard"] == "ban":
        return conditions.Conditions(fire_ban=True)
    if s["hazard"] == "fire":
        hs = [hz.Hazard("fire", "watch", "Lane Cove", 6, "RFS", ident="RFS:1")]
        return conditions.Conditions(hazards=hs, alert=hz.alert_id(hs), fire_ban=True)
    return conditions.Conditions()


def observations(s):
    """observed.Observed for a preset that has them, else None."""
    from almanac import observed

    if not s.get("observed"):
        return None
    now = now_of(s)
    change = observed.Southerly("Kurnell", now - timedelta(minutes=20), 315.0, 190.0, 7.5, 63.0)
    return observed.Observed("Horsley Park", "Horsley Park AWS", now - timedelta(minutes=1), since9_mm=1.4,
                             last_hr_mm=0.2, southerly_coast=change if s["observed"] == "coast" else None,
                             southerly_here=change if s["observed"] == "here" else None, source="BOM")


def render_preset(name, cfg, map_mode=None, layout=None):
    """(canvas, layout dict) for one preset."""
    from almanac.render import render

    s = preset(name, map_mode)
    now = now_of(s)
    day, is_tomorrow = schedule.poster_day(now, cfg.tomorrow_from_hour)
    layout = {} if layout is None else layout
    img = render(now=now, poster_day=day, is_tomorrow=is_tomorrow, forecast=forecast(s), photo=photo(s), cfg=cfg,
                 cond=conds(s), layout=layout, observed=observations(s))
    return img, layout


# (preset, map mode, still name) for every case the tests and stills cover.
CASES = [(n, m, f"{n}-{'map' if m else 'photo'}") for n in WORST for m in (False, True)] + [
    ("afternoon", False, "afternoon-photo"),
    ("morning", False, "morning-photo"),
    ("night", False, "evening-photo"),
    ("southerly", False, "southerly-photo"),
]
