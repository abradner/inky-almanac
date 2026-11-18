"""Radar mode: button C swaps the photo for the rain radar for a while.

The rules (all pure, tested in tests/test_radar.py):

- Entering: button C on the frame (POST /mode?set=radar), or the status
  page. Pressing C again (set=normal) leaves.
- Leaving on its own, at whichever comes first:
  * local midnight after it was entered;
  * at least MIN_RADAR_S (2 h) in, the newest frame (no older than
    FRESH_S) shows no rain anywhere (see "What counts as rain" below).
    Re-checked every time a frame arrives; a stale or missing frame never
    ends it early;
  * a *new* specific hazard: one whose (kind, level, where) wasn't in force
    while radar mode was on, so the poster goes back to the warning map.
    A reissue of a warning already shown keeps the same key (the alert id
    itself changes on every reissue, so it can't tell the two apart).
- Cadence: with rain within RAIN_NEAR_KM of home the frame may redraw every
  FAST_GAP_S (every new frame: BOM issues one about every 5 minutes);
  otherwise every NORMAL_GAP_S, the frame's usual cap on alert redraws.
- Roughly the same: a new frame changes the alert id (and so redraws) only
  when the rain near home changed enough to see (worth_redrawing): rain
  arrives within RAIN_NEAR_KM or leaves it, the nearest rain moves more than
  NEAREST_CHANGE_KM, the rain area there changes by more than AREA_CHANGE,
  or the heaviest band there changes. Otherwise the id is held; the hourly
  poster redraws everything anyway.
- What counts as rain (for every decision; the poster still draws it all):
  only levels from RAIN_COUNT_FROM_LEVEL up, and only in clusters of at
  least CLUSTER_MIN_PIXELS connected pixels. On a clear night (18 Nov 2026)
  ground clutter west of Sydney painted 7,000-9,000 pixels of level 1 and
  scattered blobs of level 2-14 no bigger than 76 pixels, frame after frame;
  real rain (16 Nov) forms clusters of thousands.
- The state lives in the cache volume (radar-mode.json), so a restart keeps
  it.
"""

import json
import math
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

RADAR, NORMAL = "radar", "normal"
MODES = (RADAR, NORMAL)
MIN_RADAR_S = 2 * 3600
FRESH_S = 20 * 60  # an older frame can't prove the rain has gone
RAIN_NEAR_KM = 64
RAIN_MIN_PIXELS = 12  # fewer counted pixels than this within RAIN_NEAR_KM is no rain near
# Level 1 is BOM's faintest colour (245,245,255): drawn, never counted.
RAIN_COUNT_FROM_LEVEL = 2
CLUSTER_MIN_PIXELS = 160  # 10 km2 at 0.25 km/px; clear-night clutter blobs reached 76
NEAREST_CHANGE_KM = 5  # the nearest rain moved this far: worth a redraw
AREA_CHANGE = 0.25  # the rain area near home changed by this fraction: worth a redraw
# Heaviest-rain bands, as the poster draws them: blue, green, yellow/orange, red.
BANDS = ((2, 1), (5, 2), (8, 3), (12, 4))  # (from level, band)
FAST_GAP_S = 4 * 60  # below BOM's ~5 min cadence, so every new frame draws
NORMAL_GAP_S = 10 * 60  # the frame's own cap on alert redraws


# ------------------------------------------------------------------ geometry


def pixel_of(radar, lat, lon):
    """(x, y) of a place on a radar image: a flat projection centred on the
    radar site, which is fine to a pixel at these ranges."""
    kx = 111.32 * math.cos(math.radians(radar.site_lat))
    ky = 110.57
    x = radar.width / 2 + (lon - radar.site_lon) * kx / radar.km_per_px
    y = radar.height / 2 - (lat - radar.site_lat) * ky / radar.km_per_px
    return x, y


def _circle_rows(radar, lat, lon, km):
    """(y, x0, x1) spans of the image inside `km` of (lat, lon). The part of
    the circle off the image can't be seen and isn't included."""
    w, h = radar.width, radar.height
    cx, cy = pixel_of(radar, lat, lon)
    r = km / radar.km_per_px
    for y in range(max(0, math.ceil(cy - r)), min(h, math.floor(cy + r) + 1)):
        half = math.sqrt(max(0.0, r * r - (y - cy) ** 2))
        x0, x1 = max(0, math.ceil(cx - half)), min(w, math.floor(cx + half) + 1)
        if x1 > x0:
            yield y, x0, x1


def rain_pixels(radar, lat=None, lon=None, km=None, plane=None):
    """How many pixels of `plane` (default: the raw rain) are non-zero:
    anywhere on the image, or within `km` of (lat, lon)."""
    rain = radar.rain if plane is None else plane
    if km is None:
        return len(rain) - rain.count(0)
    w, total = radar.width, 0
    for y, x0, x1 in _circle_rows(radar, lat, lon, km):
        row = rain[y * w + x0:y * w + x1]
        total += len(row) - row.count(0)
    return total


def counted_rain(radar):
    """The rain that counts for decisions: levels from RAIN_COUNT_FROM_LEVEL
    up, in 8-connected clusters of at least CLUSTER_MIN_PIXELS. Same shape
    as radar.rain: the level where it counts, else 0."""
    w, h, rain = radar.width, radar.height, radar.rain
    keep = bytes([0] * RAIN_COUNT_FROM_LEVEL + [1] * (256 - RAIN_COUNT_FROM_LEVEL))
    strong = rain.translate(keep)
    out = bytearray(w * h)
    seen = bytearray(w * h)
    start = strong.find(1)
    while start != -1:
        if not seen[start]:
            seen[start] = 1
            stack, cluster = [start], []
            while stack:
                i = stack.pop()
                cluster.append(i)
                x, y = i % w, i // w
                for yy in (y - 1, y, y + 1):
                    if 0 <= yy < h:
                        for xx in (x - 1, x, x + 1):
                            if 0 <= xx < w:
                                j = yy * w + xx
                                if strong[j] and not seen[j]:
                                    seen[j] = 1
                                    stack.append(j)
            if len(cluster) >= CLUSTER_MIN_PIXELS:
                for i in cluster:
                    out[i] = rain[i]
        start = strong.find(1, start + 1)
    return bytes(out)


def band(level):
    """Heaviest-rain band of a level (0 for none or uncounted)."""
    out = 0
    for lo, b in BANDS:
        if level >= lo:
            out = b
    return out


@dataclass(frozen=True)
class Analysis:
    issued_at: float
    visible: bool  # counted rain anywhere on the image
    near: bool  # counted rain within RAIN_NEAR_KM of home
    near_px: int = 0  # counted rain pixels within RAIN_NEAR_KM
    nearest_km: Optional[float] = None  # distance from home to the nearest of them
    heaviest: int = 0  # the heaviest band among them


def analyse(radar, home):
    counted = counted_rain(radar)
    w, (lat, lon) = radar.width, home
    hx, hy = pixel_of(radar, lat, lon)
    near_px, nearest, heaviest = 0, None, 0
    for y, x0, x1 in _circle_rows(radar, lat, lon, RAIN_NEAR_KM):
        row = counted[y * w + x0:y * w + x1]
        n = len(row) - row.count(0)
        if not n:
            continue
        near_px += n
        heaviest = max(heaviest, band(max(row)))
        for i, v in enumerate(row):
            if v:
                d = math.hypot(x0 + i - hx, y - hy) * radar.km_per_px
                nearest = d if nearest is None else min(nearest, d)
    near = near_px >= RAIN_MIN_PIXELS
    return Analysis(
        issued_at=radar.issued_at,
        visible=(len(counted) - counted.count(0)) >= CLUSTER_MIN_PIXELS,
        near=near,
        near_px=near_px if near else 0,
        nearest_km=nearest if near else None,
        heaviest=heaviest if near else 0,
    )


def worth_redrawing(drawn, current):
    """Has the rain near home changed enough since `drawn` to redraw for?"""
    if drawn.near != current.near:
        return True  # rain arrived within RAIN_NEAR_KM, or left
    if not current.near:
        return False
    if abs(current.nearest_km - drawn.nearest_km) > NEAREST_CHANGE_KM:
        return True
    if abs(current.near_px - drawn.near_px) > AREA_CHANGE * max(drawn.near_px, 1):
        return True
    return current.heaviest != drawn.heaviest


def min_gap_s(analysis):
    """How soon the frame may redraw for a new radar frame."""
    return FAST_GAP_S if analysis is not None and analysis.near else NORMAL_GAP_S


# ------------------------------------------------------------------ the rules


def hazard_keys(hazards):
    """Reissue-proof identities of the specific hazards in force."""
    return sorted({f"{h.kind}|{h.level}|{h.where}" for h in hazards or [] if h.specific})


def enter(now, hazards):
    """A fresh radar-mode state. `hazards` None means unknown yet: the first
    evaluation that knows them takes them as already shown."""
    return {"mode": RADAR, "since": now.timestamp(), "hazards": None if hazards is None else hazard_keys(hazards)}


def leave(now, why):
    return {"mode": NORMAL, "left": now.timestamp(), "why": why}


def decide(state, now, *, keys=None, analysis=None):
    """(new state, why it left radar mode or None). `now` is an aware local
    datetime; `keys` the current hazard_keys (None: unknown); `analysis` the
    newest frame's (None: no frame)."""
    if state.get("mode") != RADAR:
        return state, None
    try:
        since = float(state["since"])
    except (KeyError, TypeError, ValueError):
        return leave(now, "bad state"), "bad state"
    t = now.timestamp()
    if now.date() > datetime.fromtimestamp(since, now.tzinfo).date():
        return leave(now, "end of day"), "end of day"
    if keys is not None:
        known = state.get("hazards")
        if known is None:
            state = dict(state, hazards=list(keys))
        elif set(keys) - set(known):
            return leave(now, "new warning"), "new warning"
        # Known keys are never dropped: a warning that clears and comes back
        # is not new.
    if (t - since >= MIN_RADAR_S and analysis is not None and t - analysis.issued_at <= FRESH_S
            and analysis.issued_at >= since and not analysis.visible):
        return leave(now, "no rain"), "no rain"
    return state, None


@dataclass(frozen=True)
class View:
    """What a radar-mode poster needs. `alert` joins the alert id: it changes
    when a new frame is worth redrawing for (see RadarMode.alert), so the
    frame's /alert.json poll redraws under min_gap_s."""

    radar: object  # domain.RadarImage, or None while no frame has arrived
    analysis: Optional[Analysis]
    min_gap_s: int
    alert: str = "radar:none"


class RadarMode:
    """The persisted mode, and the newest frame's analysis (cached per frame)."""

    def __init__(self, path, home, loader):
        self.path, self.home, self.loader = path, home, loader
        self._lock = threading.Lock()
        self._analysis = None  # (issued_at, Analysis)
        self._held = None  # (since, alert id, drawn Analysis, slot): see alert()

    def state(self):
        try:
            s = json.loads(self.path.read_text())
            return s if isinstance(s, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self, s):
        tmp = self.path.with_suffix(".%d.tmp" % threading.get_ident())
        tmp.write_text(json.dumps(s))
        tmp.replace(self.path)

    def set(self, mode, now, hazards=None):
        if mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}")
        with self._lock:
            self._save(enter(now, hazards) if mode == RADAR else leave(now, "button"))

    def active(self, now):
        """In radar mode (midnight applied): what the background fetcher asks."""
        return decide(self.state(), now)[0].get("mode") == RADAR

    def analysis(self, radar):
        if radar is None:
            return None
        cached = self._analysis
        if cached and cached[0] == radar.issued_at:
            return cached[1]
        a = analyse(radar, self.home)
        self._analysis = (radar.issued_at, a)
        return a

    def alert(self, since, analysis, now, persist=True):
        """The radar part of the alert id. A new frame takes a new id only
        when worth_redrawing says the rain near home changed enough since the
        frame last drawn for; otherwise the id is held. The hourly poster
        draws the newest frame anyway, so the first evaluation in a new hour
        takes the current frame as the drawn one."""
        if analysis is None:
            return "radar:none"
        slot = now.strftime("%Y-%m-%dT%H")
        with self._lock:
            held = self._held
            if held and held[0] == since and not worth_redrawing(held[2], analysis):
                if held[3] != slot and persist:
                    self._held = (since, held[1], analysis, slot)
                return held[1]
            new = "radar:%d" % analysis.issued_at
            if persist:
                self._held = (since, new, analysis, slot)
            return new

    def view(self, now, hazards, persist=True):
        """The radar-mode View for `now`, or None in normal mode. Applies the
        exit rules, and saves a change when `persist` (a render for another
        moment, ?at=, must not end the real mode)."""
        state = self.state()
        if state.get("mode") != RADAR:
            return None
        try:
            radar = self.loader(now)
        except Exception as e:  # the radar is optional; never let it take the poster down
            print(f"radar failed: {e!r}", flush=True)
            radar = None
        a = self.analysis(radar)
        keys = None if hazards is None else hazard_keys(hazards)
        new, why = decide(state, now, keys=keys, analysis=a)
        if new != state and persist:
            with self._lock:
                if self.state() == state:  # nobody pressed a button meanwhile
                    self._save(new)
            if why:
                print(f"radar mode ended: {why}", flush=True)
        if new.get("mode") != RADAR:
            return None
        return View(radar=radar, analysis=a, min_gap_s=min_gap_s(a),
                    alert=self.alert(new.get("since"), a, now, persist=persist))
