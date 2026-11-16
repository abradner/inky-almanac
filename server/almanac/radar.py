"""Radar mode: button C swaps the photo for the rain radar for a while.

The rules (all pure, tested in tests/test_radar.py):

- Entering: button C on the frame (POST /mode?set=radar), or the status
  page. Pressing C again (set=normal) leaves.
- Leaving on its own, at whichever comes first:
  * local midnight after it was entered;
  * at least MIN_RADAR_S (2 h) in, the newest frame (no older than
    FRESH_S) shows no rain anywhere (fewer than RAIN_MIN_PIXELS rain
    pixels, so a speck of clutter can't hold it open). Re-checked every
    time a frame arrives; a stale or missing frame never ends it early;
  * a *new* specific hazard: one whose (kind, level, where) wasn't in force
    while radar mode was on, so the poster goes back to the warning map.
    A reissue of a warning already shown keeps the same key (the alert id
    itself changes on every reissue, so it can't tell the two apart).
- Cadence: with rain within RAIN_NEAR_KM of home the frame may redraw every
  FAST_GAP_S (every new frame: BOM issues one about every 5 minutes);
  otherwise every NORMAL_GAP_S, the frame's usual cap on alert redraws.
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
RAIN_MIN_PIXELS = 12  # fewer rain pixels than this is clutter, not rain
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


def rain_pixels(radar, lat=None, lon=None, km=None):
    """How many pixels show rain: anywhere on the image, or within `km` of
    (lat, lon). The part of that circle off the image can't be seen and
    doesn't count."""
    w, h, rain = radar.width, radar.height, radar.rain
    if km is None:
        return len(rain) - rain.count(0)
    cx, cy = pixel_of(radar, lat, lon)
    r = km / radar.km_per_px
    total = 0
    for y in range(max(0, math.ceil(cy - r)), min(h, math.floor(cy + r) + 1)):
        half = math.sqrt(max(0.0, r * r - (y - cy) ** 2))
        x0, x1 = max(0, math.ceil(cx - half)), min(w, math.floor(cx + half) + 1)
        if x1 > x0:
            row = rain[y * w + x0:y * w + x1]
            total += len(row) - row.count(0)
    return total


@dataclass(frozen=True)
class Analysis:
    issued_at: float
    visible: bool  # rain anywhere on the image
    near: bool  # rain within RAIN_NEAR_KM of home


def analyse(radar, home):
    return Analysis(
        issued_at=radar.issued_at,
        visible=rain_pixels(radar) >= RAIN_MIN_PIXELS,
        near=rain_pixels(radar, home[0], home[1], RAIN_NEAR_KM) >= RAIN_MIN_PIXELS,
    )


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
    """What a radar-mode poster needs."""

    radar: object  # domain.RadarImage, or None while no frame has arrived
    analysis: Optional[Analysis]
    min_gap_s: int

    @property
    def alert(self):
        """Joins the alert id: changes with every new frame, so the frame's
        /alert.json poll redraws under min_gap_s."""
        return "radar:%d" % self.analysis.issued_at if self.analysis else "radar:none"


class RadarMode:
    """The persisted mode, and the newest frame's analysis (cached per frame)."""

    def __init__(self, path, home, loader):
        self.path, self.home, self.loader = path, home, loader
        self._lock = threading.Lock()
        self._analysis = None  # (issued_at, Analysis)

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
        return View(radar=radar, analysis=a, min_gap_s=min_gap_s(a))
