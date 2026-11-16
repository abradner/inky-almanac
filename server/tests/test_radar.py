"""Radar mode: the BOM radar adapter (palette, layers, cadence, JIT), the
rules (radar.py), the radar poster, and the app's endpoints.

The recorded frame is a real IDR714 (Sydney, Terrey Hills, 64 km) frame from
2026-11-16 01:14 UTC, a morning with heavy rain across Sydney, plus that
product's four background layers: all public images. Home in these tests is
central Sydney (the Config defaults)."""

import json
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from almanac import radar as R
from almanac.app import Almanac
from almanac.conditions import Conditions
from almanac.config import Config
from almanac.domain import BASE_COAST, BASE_LABEL, BASE_RING, RAIN_LEVELS, RadarImage
from almanac.hazards import Hazard
from almanac.palette import BLACK, BLUE, GREEN, ORANGE, RED, WHITE, YELLOW
from almanac.providers import bom_radar, cache
from almanac import radar_poster
from almanac.render import H, render

SYD = ZoneInfo("Australia/Sydney")
FIX = Path(__file__).resolve().parent / "fixtures" / "radar"
FRAME = "IDR714.T.202611160114.png"
ISSUED = datetime(2026, 11, 16, 1, 14, tzinfo=timezone.utc).timestamp()
HOME = (-33.87, 151.21)  # central Sydney

# The recorded frame's palette, read with Pillow: index -> (colour, pixels).
# Index 0 is transparent; 1 is the copyright bar's grey, 2 the black text.
FRAME_PALETTE = {
    1: ((192, 192, 192), 6484), 2: ((0, 0, 0), 2998),
    3: ((245, 245, 255), 39686), 4: ((180, 180, 255), 19682), 5: ((120, 120, 255), 11286),
    6: ((20, 20, 255), 10772), 7: ((0, 216, 195), 8984), 8: ((0, 102, 102), 3933),
    9: ((0, 150, 144), 5541), 10: ((255, 200, 0), 2048), 11: ((255, 255, 0), 2656),
    12: ((200, 0, 0), 68), 13: ((255, 150, 0), 808), 14: ((255, 100, 0), 380), 15: ((255, 0, 0), 136),
}


def cfg(tmp_path, **kw):
    return Config(cache_dir=tmp_path, **kw)


class FakeFtp:
    """Serves the recorded fixtures; records every call."""

    def __init__(self, frames=(FRAME,), fail=None):
        self.frames = list(frames)
        self.calls = []
        self.fail = fail

    def list(self, directory, pattern):
        self.calls.append(("list", directory, pattern))
        if self.fail:
            raise self.fail
        return list(self.frames)

    def get(self, directory, name, limit=None):
        self.calls.append(("get", directory, name))
        if self.fail:
            raise self.fail
        return (FIX / (FRAME if ".T." in name else name)).read_bytes()

    def gets(self, kind=".T."):
        return [c for c in self.calls if c[0] == "get" and kind in c[2]]

    def lists(self):
        return [c for c in self.calls if c[0] == "list"]


def recorded(tmp_path):
    """The recorded frame as the adapter delivers it."""
    b = bom_radar.BomRadar(cfg(tmp_path), transport=FakeFtp())
    b.refresh(ISSUED + 120)
    return b.latest()


def synthetic(blobs=(), w=512, h=512, level=5, issued=ISSUED):
    """A RadarImage at Terrey Hills' geometry with square rain blobs at
    (x, y, half-size)."""
    rain = bytearray(w * h)
    for x, y, r in blobs:
        for yy in range(max(0, y - r), min(h, y + r + 1)):
            for xx in range(max(0, x - r), min(w, x + r + 1)):
                rain[yy * w + xx] = level
    return RadarImage(width=w, height=h, rain=bytes(rain), base=bytes(w * h), issued_at=issued, km_per_px=0.25,
                      site_lat=-33.701, site_lon=151.210, name="Terrey Hills", range_km=64, credit="BOM")


# ------------------------------------------------------------------ palette and layers


def test_every_scale_colour_maps_to_its_level_and_nothing_else_is_rain():
    for i, rgb in enumerate(bom_radar.RAIN_SCALE):
        assert bom_radar.rain_level(rgb) == i + 1
    for not_rain in [(192, 192, 192), (0, 0, 0), (255, 255, 255), (224, 216, 184), (192, 128, 0)]:
        assert bom_radar.rain_level(not_rain) == 0


def test_recorded_frame_levels_match_its_palette():
    levels = Counter(bom_radar.rain_levels((FIX / FRAME).read_bytes()))
    want = Counter()
    for colour, n in FRAME_PALETTE.values():
        if colour in bom_radar.RAIN_SCALE:
            want[bom_radar.RAIN_SCALE.index(colour) + 1] += n
    assert {k: v for k, v in levels.items() if k} == dict(want)
    # the copyright bar and the caption text are not rain
    assert levels[0] == 512 * 512 - sum(want.values())
    assert sum(want.values()) == 105980


def test_layers_become_quiet_line_classes():
    layers = {n: (FIX / f"IDR714.{n}.png").read_bytes() for n in bom_radar.LAYERS}
    base = Counter(bom_radar.base_classes(layers))
    assert base[BASE_LABEL] == 5424  # every black place-name and town-dot pixel
    assert base[BASE_RING] > 3000 and base[BASE_COAST] > 2000
    # The land and sea fills are not drawn: most of the map stays empty.
    assert base[0] > 0.7 * 512 * 512


def test_a_missing_or_broken_layer_is_skipped():
    layers = {"locations": (FIX / "IDR714.locations.png").read_bytes(), "range": b"junk"}
    base = Counter(bom_radar.base_classes(layers))
    assert base[BASE_LABEL] == 5424 and base[BASE_RING] == 0


def test_junk_frames_are_rejected():
    for junk in [b"", b"<html>", b"\x89PNG not really"]:
        with pytest.raises(Exception):
            bom_radar.rain_levels(junk)


def test_rain_inks_light_is_pale_blue_heavy_is_yellow_orange_red():
    def inks(level):
        return {radar_poster.rain_ink(level, x, y) for x in range(4) for y in range(4)}

    for level in (1, 2, 3):
        assert inks(level) == {WHITE, BLUE}  # pale: blue stipple on white
    assert inks(4) == {BLUE}
    for level in range(8, RAIN_LEVELS + 1):
        assert inks(level) <= {YELLOW, ORANGE, RED, BLACK} and inks(level) & {YELLOW, ORANGE, RED}

    def blue_share(level):
        return sum(radar_poster.rain_ink(level, x, y) == BLUE for x in range(4) for y in range(4))

    assert 0 < blue_share(1) < blue_share(2) < blue_share(3) < blue_share(4) == 16


def test_ink_plane_of_the_recorded_frame(tmp_path):
    radar = recorded(tmp_path)
    plane = Counter(radar_poster.ink_plane(radar, radar_poster.crop_box(radar, (500, 480))))
    assert set(plane) <= set(range(7))
    assert plane[WHITE] > plane[BLUE] > 0 and plane[YELLOW] and plane[ORANGE] and plane[RED]
    assert plane[GREEN] and plane[BLACK]  # coast, rings, moderate rain; place names
    # The crop starts below the copyright bar (rows 0-15 of the image).
    assert radar_poster.crop_box(radar, (500, 480))[1] == 16


def test_place_names_are_drawn_over_the_rain():
    r = synthetic([(256, 256, 4)], level=12)  # heavy rain
    base = bytearray(r.base)
    base[256 * 512 + 256] = BASE_LABEL
    base[200 * 512 + 200] = BASE_LABEL  # and one on dry ground
    plane = radar_poster.ink_plane(replace(r, base=bytes(base)), (0, 0, 512, 512))
    assert plane[256 * 512 + 256] == BLACK and plane[200 * 512 + 200] == BLACK
    assert plane[256 * 512 + 257] == RED


# ------------------------------------------------------------------ geometry and rain near home


def test_location_dots_land_where_the_geometry_puts_them(tmp_path):
    radar = recorded(tmp_path)
    # Town dots (4x4) read from IDR714.locations.png: Sydney Airport, Penrith.
    for (lat, lon), dot in [((-33.946, 151.177), (243.5, 364.5)), ((-33.751, 150.694), (67.5, 277.5))]:
        x, y = R.pixel_of(radar, lat, lon)
        assert abs(x - dot[0]) <= 3 and abs(y - dot[1]) <= 3  # under a kilometre


def test_rain_within_64_km_of_home():
    hx, hy = R.pixel_of(synthetic(), *HOME)
    px_per_km = 4
    inside = synthetic([(round(hx), round(hy - 60 * px_per_km), 2)])  # 60 km north of home
    outside = synthetic([(round(hx), round(hy - 70 * px_per_km), 2)])  # 70 km north (still on the image)
    assert 0 <= hy - 70 * px_per_km  # the probe is on the image, so "outside" really tests the radius
    assert R.rain_pixels(inside, *HOME, 64) == 25 and R.analyse(inside, HOME).near
    assert R.rain_pixels(outside, *HOME, 64) == 0 and not R.analyse(outside, HOME).near
    assert R.analyse(outside, HOME).visible  # still rain on the overlay


def test_the_radius_is_a_circle_not_a_square():
    hx, hy = R.pixel_of(synthetic(), *HOME)
    px = 4  # per km
    corner = synthetic([(round(hx - 50 * px), round(hy - 50 * px), 1)])  # 50 km west and north: 71 km
    inside = synthetic([(round(hx - 40 * px), round(hy - 40 * px), 1)])  # 57 km
    assert R.rain_pixels(corner, *HOME, 64) == 0
    assert R.rain_pixels(inside, *HOME, 64) == 9


def test_specks_are_clutter_not_rain():
    hx, hy = R.pixel_of(synthetic(), *HOME)
    speck = synthetic([(round(hx), round(hy), 1)])  # 9 pixels
    assert R.rain_pixels(speck) == 9 < R.RAIN_MIN_PIXELS
    a = R.analyse(speck, HOME)
    assert not a.visible and not a.near


def test_recorded_frame_has_rain_near_central_sydney(tmp_path):
    a = R.analyse(recorded(tmp_path), HOME)
    assert a.near and a.visible and a.issued_at == ISSUED


def test_cadence_choice():
    near = R.Analysis(ISSUED, True, True)
    far = R.Analysis(ISSUED, True, False)
    assert R.min_gap_s(near) == R.FAST_GAP_S == 240
    assert R.min_gap_s(far) == R.NORMAL_GAP_S == 600
    assert R.min_gap_s(None) == 600


# ------------------------------------------------------------------ exit rules

T0 = datetime(2026, 11, 16, 14, 0, tzinfo=SYD)
STORM = Hazard("storm", "watch", "Severe Thunderstorm Warning", source="BOM", ident="BOM:16/14:00 EDT Severe",
               titled=True)
FLOOD = Hazard("flood", "watch", "Flood Warning", source="BOM", ident="BOM:16/14:30 EDT Flood", titled=True)
NOTICE = Hazard("weather", "advice", "Fire Weather Warning", specific=False, ident="x")


def rainy(t, visible=True):
    return R.Analysis(t.timestamp() - 60, visible, visible)


def test_midnight_ends_radar_mode():
    s = R.enter(T0.replace(hour=23), [])
    assert R.decide(s, T0.replace(hour=23, minute=59), analysis=rainy(T0))[0]["mode"] == "radar"
    new, why = R.decide(s, T0.replace(hour=23) + timedelta(hours=1, minutes=1), analysis=rainy(T0))
    assert new["mode"] == "normal" and why == "end of day"


def test_no_rain_ends_it_only_after_two_hours():
    s = R.enter(T0, [])
    before = T0 + timedelta(hours=1, minutes=59)
    assert R.decide(s, before, analysis=rainy(before, visible=False))[1] is None
    after = T0 + timedelta(hours=2)
    new, why = R.decide(s, after, analysis=rainy(after, visible=False))
    assert new["mode"] == "normal" and why == "no rain"


def test_rain_still_visible_keeps_it_going_until_midnight():
    s = R.enter(T0, [])
    late = T0.replace(hour=23, minute=50)
    assert R.decide(s, late, analysis=rainy(late, visible=True))[1] is None


def test_a_stale_or_missing_frame_never_ends_it_early():
    s = R.enter(T0, [])
    later = T0 + timedelta(hours=3)
    stale = R.Analysis(later.timestamp() - R.FRESH_S - 60, False, False)
    assert R.decide(s, later, analysis=stale)[1] is None
    assert R.decide(s, later, analysis=None)[1] is None


def test_a_new_specific_warning_ends_it():
    s = R.enter(T0, [STORM])
    new, why = R.decide(s, T0 + timedelta(minutes=5), keys=R.hazard_keys([STORM, FLOOD]))
    assert new["mode"] == "normal" and why == "new warning"


def test_a_reissue_or_a_broad_notice_does_not():
    s = R.enter(T0, [STORM])
    reissued = replace(STORM, ident="BOM:16/14:40 EDT Severe")  # new alert id, same warning
    new, why = R.decide(s, T0 + timedelta(minutes=40), keys=R.hazard_keys([reissued, NOTICE]))
    assert why is None and new["mode"] == "radar"
    from almanac.hazards import alert_id

    assert alert_id([reissued]) != alert_id([STORM])  # the alert id alone couldn't tell


def test_an_escalation_counts_as_new():
    s = R.enter(T0, [STORM])
    worse = replace(STORM, level="emergency")
    assert R.decide(s, T0 + timedelta(minutes=5), keys=R.hazard_keys([worse]))[1] == "new warning"


def test_hazards_unknown_at_entry_are_learned_not_treated_as_new():
    s = R.enter(T0, None)
    new, why = R.decide(s, T0 + timedelta(minutes=2), keys=R.hazard_keys([STORM]))
    assert why is None and new["hazards"] == R.hazard_keys([STORM])
    assert R.decide(new, T0 + timedelta(minutes=4), keys=R.hazard_keys([STORM, FLOOD]))[1] == "new warning"


def test_a_warning_that_cleared_and_came_back_is_not_new():
    s = R.enter(T0, [STORM])
    s, _ = R.decide(s, T0 + timedelta(minutes=5), keys=[])
    assert R.decide(s, T0 + timedelta(minutes=10), keys=R.hazard_keys([STORM]))[1] is None


def test_normal_mode_is_left_alone():
    s = R.leave(T0, "button")
    assert R.decide(s, T0 + timedelta(days=2), keys=["x"], analysis=None) == (s, None)


# ------------------------------------------------------------------ the adapter: cadence and JIT


def test_cadence_is_read_from_the_listing():
    stamps = [ISSUED - 300 * i for i in range(6)]
    assert bom_radar.cadence(stamps) == 300
    assert bom_radar.cadence([ISSUED]) == bom_radar.DEFAULT_CADENCE_S
    assert bom_radar.cadence([ISSUED, ISSUED + 5]) == bom_radar.CADENCE_RANGE_S[0]  # clamped


def test_stamps_are_utc_and_other_products_ignored():
    assert bom_radar.parse_stamp("IDR714.T.202611160114.png", "IDR714") == ISSUED
    assert bom_radar.parse_stamp("IDR713.T.202611160114.png", "IDR714") is None
    assert bom_radar.parse_stamp("IDR714.T.202110092349.png.tmp", "IDR714") is None
    assert bom_radar.parse_stamp("IDR714.gif", "IDR714") is None


def test_fetches_no_faster_than_bom_issues_frames(tmp_path):
    ftp = FakeFtp(frames=["IDR714.T.202611160109.png", FRAME])
    b = bom_radar.BomRadar(cfg(tmp_path), transport=ftp)
    t = ISSUED + 90
    b.refresh(t)
    assert len(ftp.lists()) == 1 and len(ftp.gets()) == 1
    assert ftp.lists()[0][2] == "IDR714.T.*.png"  # only this product's frames, not the 1 MB listing
    for dt in (30, 120, 200):  # the next frame is due at 01:19 + a minute to publish
        b.refresh(t + dt)
    assert len(ftp.lists()) == 1
    b.refresh(ISSUED + 300 + bom_radar.PUBLISH_LAG_S)
    assert len(ftp.lists()) == 2
    assert len(ftp.gets()) == 1  # the listing had nothing newer: no second download
    b.refresh(ISSUED + 300 + bom_radar.PUBLISH_LAG_S + 10)
    assert len(ftp.lists()) == 2  # a late frame is looked for at most once a minute
    b.refresh(ISSUED + 300 + bom_radar.PUBLISH_LAG_S + bom_radar.LIST_GAP_S)
    assert len(ftp.lists()) == 3


def test_layers_are_fetched_once_and_rechecked_weekly(tmp_path):
    ftp = FakeFtp()
    b = bom_radar.BomRadar(cfg(tmp_path), transport=ftp)
    b.refresh(ISSUED + 90)
    assert sorted(c[2] for c in ftp.gets(".png") if ".T." not in c[2]) == sorted(
        f"IDR714.{n}.png" for n in bom_radar.LAYERS)
    n = len(ftp.calls)
    b._next_try = 0
    b.refresh(ISSUED + 3600)
    assert not [c for c in ftp.calls[n:] if c[0] == "get" and ".T." not in c[2]]
    b._next_try = 0
    b.refresh(ISSUED + bom_radar.LAYERS_TTL_S + 100)
    assert len([c for c in ftp.calls[n:] if c[0] == "get" and ".T." not in c[2]]) == len(bom_radar.LAYERS)


def test_failures_back_off_and_keep_the_last_frame(tmp_path):
    ftp = FakeFtp()
    b = bom_radar.BomRadar(cfg(tmp_path), transport=ftp)
    b.refresh(ISSUED + 90)
    ftp.fail = OSError("421 too many users")
    t = ISSUED + 400
    b.refresh(t)
    n = len(ftp.calls)
    b.refresh(t + bom_radar.RETRY_S - 1)
    assert len(ftp.calls) == n  # backing off
    b.refresh(t + bom_radar.RETRY_S)
    b.refresh(t + bom_radar.RETRY_S + 2 * bom_radar.RETRY_S - 1)
    assert len(ftp.calls) == n + 1  # doubled
    assert b.latest().issued_at == ISSUED


def test_the_request_path_never_touches_the_network(tmp_path):
    ftp = FakeFtp()
    live = bom_radar.BomRadar(cfg(tmp_path), transport=ftp)
    live.refresh(ISSUED + 90)
    calls = len(ftp.calls)
    offline = bom_radar.BomRadar(cfg(tmp_path), get_text=cache.offline)
    offline.refresh(ISSUED + 9999)  # a no-op: no transport at all
    assert offline.latest().issued_at == ISSUED and len(ftp.calls) == calls


def test_unsupported_product_is_refused(tmp_path):
    with pytest.raises(ValueError):
        bom_radar.BomRadar(cfg(tmp_path, radar_product="IDR999"), transport=FakeFtp())


# ------------------------------------------------------------------ the app

class FakeRadar:
    def __init__(self):
        self.refreshes = 0

    def refresh(self, now=None):
        self.refreshes += 1


def radar_app(tmp_path, radar=None, cond=None):
    from almanac.providers import commons as photos

    state = {"radar": radar, "cond": cond}
    app = Almanac(cfg(tmp_path), weather_loader=lambda: None,
                  photo_loader=lambda d, s: photos.Photo("1", "P", "c", __import__("PIL.Image").Image.new("RGB", (900, 600), (60, 120, 220))),
                  conditions_loader=lambda d, t, h: state["cond"], bom_loader=lambda: {},
                  radar_loader=lambda now: state["radar"])
    app._live = lambda now: True
    app.state = state
    return app


def at_issue(minutes=2):
    return datetime.fromtimestamp(ISSUED, SYD) + timedelta(minutes=minutes)


def test_normal_mode_answers_as_before(tmp_path):
    app = radar_app(tmp_path, radar=recorded(tmp_path / "r"))
    now = at_issue()
    assert app.alert_info(now) == {"id": ""}
    assert app.frame_info(now)["mode"] == "normal"


def test_radar_mode_poster_and_cadence(tmp_path):
    app = radar_app(tmp_path, radar=recorded(tmp_path / "r"))
    now = at_issue()
    app.set_mode("radar", now)
    info = app.alert_info(now)
    assert info == {"id": "radar:%d" % ISSUED, "mode": "radar", "min_gap_s": 240}  # rain within 64 km
    fi = app.frame_info(now)
    assert fi["mode"] == "radar" and fi["alert"] == info["id"] and fi["photo"] is None
    dry = synthetic([(30, 30, 6)], issued=ISSUED + 300)  # rain far from home only
    app.state["radar"] = dry
    later = app.alert_info(now + timedelta(minutes=5))
    assert later["min_gap_s"] == 600 and later["id"] == "radar:%d" % (ISSUED + 300)
    assert app.frame_info(now + timedelta(minutes=5))["hash"] != fi["hash"]  # a new frame is a new poster
    app.set_mode("normal", now + timedelta(minutes=6))
    assert app.alert_info(now + timedelta(minutes=6)) == {"id": ""}


def test_a_new_warning_flips_back_to_normal(tmp_path):
    app = radar_app(tmp_path, radar=recorded(tmp_path / "r"), cond=Conditions(hazards=[STORM], alert="a1"))
    now = at_issue()
    app.set_mode("radar", now)
    assert app.alert_info(now)["mode"] == "radar"
    reissued = replace(STORM, ident="BOM:reissue")
    app.state["cond"] = Conditions(hazards=[reissued], alert="a2")
    assert app.alert_info(now + timedelta(minutes=3))["mode"] == "radar"
    app.state["cond"] = Conditions(hazards=[reissued, FLOOD], alert="a3")
    assert app.alert_info(now + timedelta(minutes=6)) == {"id": "a3"}
    assert app.radar.state()["why"] == "new warning"
    assert app.frame_info(now + timedelta(minutes=6))["mode"] == "normal"


def test_a_render_for_another_moment_never_ends_the_real_mode(tmp_path):
    app = radar_app(tmp_path, radar=recorded(tmp_path / "r"))
    del app._live  # the real check: far from the clock means a ?at= render
    now = datetime.now(SYD)
    app.set_mode("radar", now)
    tomorrow = now + timedelta(days=1)
    assert app.alert_info(tomorrow) == {"id": ""}  # past midnight there, so normal...
    assert app.radar.state()["mode"] == "radar"  # ...but nothing was saved


def test_mode_survives_a_restart(tmp_path):
    app = radar_app(tmp_path, radar=recorded(tmp_path / "r"))
    app.set_mode("radar", at_issue())
    again = radar_app(tmp_path, radar=app.state["radar"])
    assert again.alert_info(at_issue(3))["mode"] == "radar"


def test_radar_is_fetched_only_in_radar_mode(tmp_path):
    app = radar_app(tmp_path)
    live = FakeRadar()
    now = at_issue()
    for _ in range(3):
        assert app.radar_tick(live, now) is False
    app.frame_info(now)  # requests in normal mode fetch nothing either
    assert live.refreshes == 0
    app.set_mode("radar", now)
    assert app.radar_tick(live, now) is True and live.refreshes == 1
    app.set_mode("normal", now)
    assert app.radar_tick(live, now) is False and live.refreshes == 1


def test_entering_radar_mode_wakes_the_fetcher(tmp_path):
    app = radar_app(tmp_path)
    app.radar_wanted.clear()
    app.set_mode("radar", at_issue())
    assert app.radar_wanted.is_set()


def test_radar_mode_without_a_frame_yet_still_renders(tmp_path):
    app = radar_app(tmp_path, radar=None)
    app.set_mode("radar", at_issue())
    assert app.alert_info(at_issue())["id"] == "radar:none"
    canvas = app.poster(at_issue())["canvas"]
    assert set(canvas.getdata()) <= set(range(7))


def _serve(app):
    import threading
    from http.server import ThreadingHTTPServer

    from almanac.app import make_handler

    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def test_mode_endpoint(tmp_path, monkeypatch):
    import almanac.app

    monkeypatch.setattr(almanac.app, "RADAR_FIRST_FRAME_WAIT_S", 0.05)  # no radar thread here to wait for
    app = radar_app(tmp_path, radar=recorded(tmp_path / "r"))
    srv, base = _serve(app)
    try:
        def post(q, headers=None):
            return urllib.request.urlopen(urllib.request.Request(base + "/mode" + q, method="POST", data=b"",
                                                                 headers=headers or {}))

        for bad in ("", "?set=party", "?set="):
            with pytest.raises(urllib.error.HTTPError) as e:
                post(bad)
            assert e.value.code == 400
        with pytest.raises(urllib.error.HTTPError) as e:
            post("?set=radar", {"Origin": "http://evil.example"})
        assert e.value.code == 403
        assert app.radar.state() == {}  # neither changed the mode
        assert json.load(post("?set=radar")) == {"mode": "radar"}
        assert app.radar.state()["mode"] == "radar"
        info = json.load(urllib.request.urlopen(base + "/alert.json"))
        assert info["mode"] == "radar" and info["min_gap_s"] in (240, 600)
        assert json.load(post("?set=normal")) == {"mode": "normal"}
        assert json.load(urllib.request.urlopen(base + "/alert.json")) == {"id": ""}
    finally:
        srv.shutdown()


# ------------------------------------------------------------------ the poster


def test_radar_poster_is_inks_only_with_badges_over_the_radar(tmp_path):
    radar = recorded(tmp_path / "r")
    now = at_issue()
    c = cfg(tmp_path)
    cond = Conditions(hazards=[STORM], alert="a1", fire_ban=True)
    canvas = render(now=now, poster_day=now.date(), is_tomorrow=False, forecast=None, photo=None, cfg=c, cond=cond,
                    radar=radar)
    assert set(canvas.getdata()) <= set(range(7))
    picture = canvas.crop((0, 0, 500, H))
    inks = Counter(picture.getdata())
    assert inks[YELLOW] and inks[ORANGE] and inks[RED] and inks[BLUE]
    # Badges top left: the storm badge is white with red text, at y 12-40.
    corner = Counter(canvas.crop((12, 12, 200, 40)).getdata())
    assert corner[RED] > 50
    plain = render(now=now, poster_day=now.date(), is_tomorrow=False, forecast=None, photo=None, cfg=c, cond=None,
                   radar=radar)
    assert Counter(plain.crop((12, 12, 200, 40)).getdata())[RED] < corner[RED]


def test_radar_poster_keeps_a_warning_map_off_and_its_badge_on(tmp_path):
    from almanac.domain import MapImage

    radar = recorded(tmp_path / "r")
    now = at_issue()
    with_map = Conditions(hazards=[STORM], alert="a1", warning=MapImage(png=b"not drawn", title=STORM.where))
    canvas = render(now=now, poster_day=now.date(), is_tomorrow=False, forecast=None, photo=None, cfg=cfg(tmp_path),
                    cond=with_map, radar=radar)
    assert Counter(canvas.crop((12, 12, 200, 40)).getdata())[RED] > 50  # the badge the map would have replaced


def test_no_radar_configured_refuses_radar_mode(tmp_path):
    app = Almanac(cfg(tmp_path, radar_provider="none"), weather_loader=lambda: None,
                  photo_loader=lambda d, s: None, conditions_loader=lambda d, t, h: None, bom_loader=lambda: {})
    assert not app.radar_available
    srv, base = _serve(app)
    try:
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(urllib.request.Request(base + "/mode?set=radar", method="POST", data=b""))
        assert e.value.code == 409 and app.radar.state() == {}
    finally:
        srv.shutdown()
