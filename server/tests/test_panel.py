"""The A2 side panel: worst cases never overflow, the header's lead rule, the
chart's details, and urgent facts reaching the frame through /alert.json."""

import json
import urllib.request
from datetime import date, datetime, timedelta

import pytest

import almanac.render as R
from almanac import conditions, hazards as hz, photos, schedule, weather
from almanac.app import Almanac
from almanac.config import Config
from almanac.palette import BLACK, BLUE, RED, WHITE
from panel_fixtures import CASES, SYD, WORST, conds, forecast, now_of, payload, preset, render_preset
from test_almanac import _serve, vivid


@pytest.fixture
def cfg(tmp_path):
    return Config(cache_dir=tmp_path)


def overlap(a, b):
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def texts(layout, section):
    return [t for s, t, _ in layout["texts"] if s == section]


# ------------------------------------------------------------------ overflow


@pytest.mark.parametrize("name, map_mode, still", CASES, ids=[c[2] for c in CASES])
def test_nothing_overflows(cfg, name, map_mode, still):
    img, layout = render_preset(name, cfg, map_mode)
    side_x, side_w = layout["side"]
    assert side_w == (322 if map_mode else 270)
    assert {i for _, i in img.getcolors()} <= set(range(7))
    boxes = layout["boxes"]
    assert {"header.day", "header.pair", "forecast", "hero", "hero.fact", "chart", "caption"} <= set(boxes)
    for section, (x0, y0, x1, y1) in boxes.items():
        assert x0 >= layout["photo_w"] + 4, f"{section} reaches into the picture"
        # The panel's rules run from side_x to side_x + side_w inclusive;
        # nothing may go past them (boxes are exclusive, hence the + 1).
        assert x1 <= side_x + side_w + 1, f"{section} is {x1 - side_x - side_w - 1:.0f} px too wide"
        assert y0 >= 0 and y1 <= R.H, f"{section} is off the poster"
        if section != "caption":
            assert y1 <= layout["caption_top"], f"{section} runs {y1 - layout['caption_top']:.0f} px into the caption"
    assert not overlap(boxes["header.day"], boxes["header.pair"]), "day name runs into the icon/temperatures"
    # Top to bottom, each band ends before the next begins.
    bands = [("header.day", "header.pair"), ("forecast",), ("hero", "hero.fact"), ("facts",), ("chart",), ("sun",), ("caption",)]
    present = [[boxes[k] for k in band if k in boxes] for band in bands]
    present = [b for b in present if b]
    for upper, lower in zip(present, present[1:]):
        assert max(b[3] for b in upper) <= min(b[1] for b in lower), "two bands of the panel overlap"
    assert not overlap(boxes["hero"], boxes["hero.fact"]), "top fact runs into the coming hour"
    assert R.CHART_MIN <= layout["chart_h"] <= R.CHART_MAX


@pytest.mark.parametrize("name", WORST)
@pytest.mark.parametrize("map_mode", [False, True])
def test_worst_cases_put_the_hazard_first(cfg, name, map_mode):
    _, layout = render_preset(name, cfg, map_mode)
    f = layout["facts"]
    hazards = [x for x in f if x.hazard]
    if hazards:
        assert f[0].hazard
        assert texts(layout, "hero.fact")[0] == f[0].value  # and it is the one shown large
    assert len([t for t in texts(layout, "facts") if t.strip()]) >= 2 * min(4, len(f) - 1)


def test_hail_leads_the_worst_photo(cfg):
    _, layout = render_preset("worstPhoto", cfg)
    assert [x.key for x in layout["facts"]][:3] == ["hail", "wind-change", "rain"]


def test_map_mode_gives_up_the_sun_row_before_any_warning(cfg):
    _, layout = render_preset("worstMap", cfg)
    assert not layout["sun_row"] and "sun" not in layout["boxes"]
    assert layout["caption_lines"] == layout["caption_wanted"]
    _, photo_layout = render_preset("worstPhoto", cfg)
    assert photo_layout["sun_row"] and "sun" in photo_layout["boxes"]


def test_the_chart_takes_the_height_that_is_left(cfg):
    _, quiet = render_preset("morning", cfg)
    _, busy = render_preset("worstPhoto", cfg)
    assert quiet["chart_h"] == R.CHART_MAX
    assert R.CHART_MIN < busy["chart_h"] < R.CHART_MAX


# ------------------------------------------------------------------ header


def test_header_icon_steps_down_with_the_day_name():
    assert R.header_icon_size("Monday") == 56
    assert R.header_icon_size("Tuesday") == 56
    assert R.header_icon_size("Thursday") == 48
    assert R.header_icon_size("Wednesday") == 40


def day(t_max, t_min):
    return weather.Day(date(2026, 11, 14), 1, t_max, t_min, 0, 0, 0, None, None, [])


def test_lead_rule_overnight_low_before_the_high_leads():
    fc = weather.Forecast({date(2026, 11, 15): day(31, 19)}, None, 0)
    lead, other = R.lead_pair(datetime(2026, 11, 14, 8, 59), fc, day(30, 16), False, 9)
    assert lead == (BLUE, 16, "OVERNIGHT LOW") and other == (RED, 30, "high today")


def test_lead_rule_high_today_until_the_evening():
    fc = weather.Forecast({date(2026, 11, 15): day(31, 19)}, None, 0)
    for hour in (9, 17):
        lead, other = R.lead_pair(datetime(2026, 11, 14, hour, 0), fc, day(30, 16), False, 9)
        assert lead == (RED, 30, "HIGH TODAY") and other == (BLUE, 19, "low tonight")
    lead, other = R.lead_pair(datetime(2026, 11, 14, 9, 0), weather.Forecast({}, None, 0), day(30, 16), False, 9)
    assert lead[2] == "HIGH TODAY" and other is None  # no tomorrow in the forecast yet


def test_lead_rule_low_tonight_from_the_switch():
    lead, other = R.lead_pair(datetime(2026, 11, 14, 18, 0), None, day(31, 19), True, 9)
    assert lead == (BLUE, 19, "LOW TONIGHT") and other == (RED, 31, "high tomorrow")


def test_the_lead_hour_is_configurable(cfg, monkeypatch):
    monkeypatch.setenv("ALMANAC_HIGH_LEADS_FROM", "7")
    assert Config().high_leads_from == 7
    _, layout = render_preset("morning", Config(cache_dir=cfg.cache_dir))  # 7:01
    assert "HIGH TODAY" in texts(layout, "header.pair")
    _, layout = render_preset("morning", cfg)
    assert "OVERNIGHT LOW" in texts(layout, "header.pair")


def test_the_evening_keeps_todays_date_and_labels_the_forecast(cfg):
    _, layout = render_preset("night", cfg)  # 9 pm Saturday 14 November
    assert texts(layout, "header.day") == ["Saturday", "14 November"]
    assert texts(layout, "forecast")[0] == "Tomorrow:"
    assert "LOW TONIGHT" in texts(layout, "header.pair")
    _, day_layout = render_preset("afternoon", cfg)
    assert "Tomorrow:" not in texts(day_layout, "forecast")


def test_a_long_day_name_shrinks_rather_than_collide(cfg):
    _, layout = render_preset("worstPhoto", cfg)  # Wednesday, in the 270 px panel
    assert texts(layout, "header.day")[0] == "Wednesday"  # whole, not cut


# ------------------------------------------------------------------ hero and facts


def test_hero_shows_the_coming_hour(cfg):
    _, layout = render_preset("worstPhoto", cfg)  # 5:01 pm
    assert texts(layout, "hero")[:2] == ["5–6PM", "44°"]
    assert texts(layout, "hero.fact") == ["Hail", "next hour"]


def test_the_hour_icon_shows_hail():
    assert R.hour_icon(96) == R.hour_icon(99) == "hail"
    assert R.hour_icon(95) == "storm" and R.hour_icon(61) == "rain"


def test_the_top_fact_keeps_clear_of_the_hours_temperature():
    from almanac.facts import Coming, Fact

    R.set_layout(500)
    hour = weather.Hour(hour=15, temp=-14, rain_prob=0, code=3)
    words = "NW→S gust 148 km/h and then some more words".split()
    for n in range(1, len(words) + 1):
        canvas = R.new_canvas((800, 200), WHITE)
        rec = R.Recorder(canvas)
        value = " ".join(words[:n])
        R._hero(rec, Coming(14, hour, [Fact("x", 60, "wind change", value, RED)]), R.SIDE_X, 0)
        col_right = R.SIDE_X + R.HERO_ICON + 4 + max(rec.textlength("2–3PM", font=R.CB(13)), rec.textlength("-14°", font=R.BOLD(50)))
        assert rec.boxes["hero.fact"][0] >= col_right + 8, f"{value!r} crowds the temperature"


def test_a_quiet_hour_says_nothing_unusual(cfg):
    _, layout = render_preset("morning", cfg)
    assert layout["facts"] == []
    assert texts(layout, "hero.fact") == ["Partly cloudy", "nothing unusual"]
    assert "facts" not in layout["boxes"]


def test_hazard_rows_read_name_then_when(cfg):
    s = preset("morning")
    fc = forecast(s)
    for h in fc.day(date(2026, 11, 14)).hours:
        if h.hour == 8:
            h.code, h.rain_mm, h.rain_prob = 45, 3, 90  # fog with heavy rain: rain leads, fog drops to the table
    now = now_of(s)
    layout = {}
    R.render(now=now, poster_day=now.date(), is_tomorrow=False, forecast=fc, photo=None, cfg=cfg, layout=layout)
    assert layout["facts"][0].key == "rain"
    assert texts(layout, "facts")[:2] == ["Fog", "low visibility"]


def facts_rows(rows, side_w=270):
    """Draw just the facts table; returns (texts, height used)."""
    R.set_layout(800 - side_w - 30)
    canvas = R.new_canvas((800, 120), WHITE)
    rec = R.Recorder(canvas)
    end = R._facts_table(rec, rows, R.SIDE_X, 0)
    return [(t, ink) for _, t, ink in rec.texts], end, rec.boxes["facts"]


def test_the_sub_text_shares_the_row_when_it_fits():
    from almanac.facts import Fact

    rows, end, _ = facts_rows([Fact("since9", 50, "Since 9am", "248 mm", BLUE, sub="96 mm last hr")])
    assert [t for t, _ in rows] == ["Since 9am", "248 mm", " · 96 mm last hr"]
    assert end == 18  # one row


def test_a_long_sub_text_drops_to_its_own_line():
    from almanac.facts import Fact

    long = Fact("since9", 50, "Rain since nine this morning", "248 mm", BLUE, sub="96 mm in the last hour")
    rows, end, box = facts_rows([long])
    assert [t for t, _ in rows] == ["Rain since nine this morning", "248 mm", "96 mm in the last hour"]
    assert end == 18 + 14  # the row, then a line for the sub-text
    assert box[2] <= R.SIDE_X + R.SIDE_W + 1


# ------------------------------------------------------------------ chart


def chart_canvas(fc, d, now, w=270, h=110):
    """Just the chart, drawn alone on a blank canvas."""
    canvas = R.new_canvas((w + 20, h + 20), WHITE)
    rec = R.Recorder(canvas)
    R._hourly_chart(rec, fc.day(d), 10, 10, w, h, now)
    return canvas, rec


def test_now_marker_is_white_over_a_bar_and_black_elsewhere():
    s = preset("worstPhoto")  # rain bars at every hour
    fc, now = forecast(s), now_of(s)
    under, _ = chart_canvas(fc, now.date(), None)  # what the marker is drawn over
    img, rec = chart_canvas(fc, now.date(), now)
    nx, top, base = rec.marks["now"]
    white = black = 0
    for yy in range(top + 4, base):  # below the arrowhead
        for xx in (nx - 1, nx):
            if (yy - top) % 6 >= 3:
                assert img.getpixel((xx, yy)) == under.getpixel((xx, yy)), "the dashes have gaps"
            elif under.getpixel((xx, yy)) == BLUE:
                assert img.getpixel((xx, yy)) == WHITE, f"marker not white over the bar at y={yy}"
                white += 1
            else:
                assert img.getpixel((xx, yy)) == BLACK, f"marker not black off the bar at y={yy}"
                black += 1
    assert white and black  # both cases actually exercised


def chart_with(probs=None, temps=None, now_hour=None, w=270, h=110):
    """The night preset's (tomorrow's) chart with chosen rain chances and
    temperatures by hour; returns (canvas, recorder)."""
    s = preset("night")
    fc = forecast(s)
    d = date(2026, 11, 15)
    for hr in fc.day(d).hours:
        if probs is not None:
            hr.rain_prob = probs.get(hr.hour, probs.get("rest", hr.rain_prob))
        if temps is not None:
            hr.temp = temps.get(hr.hour, temps.get("rest", hr.temp))
    now = None if now_hour is None else datetime(2026, 11, 15, now_hour, 1, tzinfo=SYD)
    return chart_canvas(fc, d, now, w, h)


def rain_labels(rec):
    return [(t, ink) for _, t, ink in rec.texts if t.endswith("%")]


def test_rain_labels_mark_the_wettest_and_driest_hours():
    _, rec = chart_with({12: 6, 18: 90, "rest": 50}, {9: 25, 16: 15, "rest": 20})
    assert rain_labels(rec) == [("90%", WHITE), ("6%", BLUE)]  # white inside a tall bar; blue above a short one


def test_no_label_on_a_bar_that_is_not_drawn():
    _, rec = chart_with({12: 4, 18: 90, "rest": 50}, {9: 25, 16: 15, "rest": 20})
    assert rain_labels(rec) == [("90%", WHITE)]  # 4% draws no bar, so it gets no label
    _, rec = chart_with({12: 3, "rest": 2}, {9: 25, 16: 15, "rest": 20})
    assert rain_labels(rec) == []  # nor does a wettest hour of 3%


def test_a_rain_label_is_nudged_off_an_extreme_rather_than_dropped():
    # The wettest bar (1 pm, 30%: label inside its top) half-covers the
    # low's label at noon.
    probs, temps = {13: 30, "rest": 10}, {12: 15, 17: 25, "rest": 20}
    _, rec = chart_with(probs, temps)
    labels = dict(rec.marks["labels"])
    assert "30%" in labels and "15°" in labels
    assert not overlap(labels["30%"], labels["15°"])
    home = 10 + (13 - 6 + 0.5) * (270 / 16)  # chart_canvas puts the chart at x = 10
    centre = (labels["30%"][0] + labels["30%"][2]) / 2
    assert centre - home >= 270 / 16 / 2 - 1  # moved right, away from the low, by at least half a bar
    assert dict(rain_labels(rec))["30%"] == BLUE  # off its bar: blue, outlined


def test_a_rain_label_on_an_extremes_hour_has_too_far_to_go():
    # Same hour as the low: half a bar or a whole bar can't clear a label
    # as wide as both, so it is left out (and the low is drawn).
    _, rec = chart_with({12: 30, "rest": 10}, {12: 15, 17: 25, "rest": 20})
    assert "30%" not in dict(rec.marks["labels"]) and "15°" in dict(rec.marks["labels"])


def test_a_rain_label_is_dropped_only_when_boxed_in():
    # A narrow chart, the wettest bar under the high's label, with the low's
    # label and the now-marker filling the room either side.
    _, rec = chart_with({7: 100, "rest": 50}, {7: 25, 6: 15, "rest": 20}, now_hour=8, w=120)
    assert "100%" not in dict(rec.marks["labels"])
    _, roomy = chart_with({7: 100, "rest": 50}, {7: 25, 6: 15, "rest": 20}, now_hour=8, w=270)
    assert "100%" in dict(roomy.marks["labels"])  # the same chart with room keeps it


@pytest.mark.parametrize("now_hour", range(6, 22))
def test_no_label_overlaps_the_now_marker(now_hour):
    _, rec = chart_with({"rest": 50, 6: 10, 21: 95}, None, now_hour=now_hour)
    nx, top, base = rec.marks["now"]
    marker = [(nx - 1, top, nx + 1, base), (nx - 5, top - 4, nx + 6, top + 4)]
    for text, box in rec.marks["labels"]:
        if text.endswith("%"):
            assert not any(overlap(box, m) for m in marker), f"{text} sits on the now-marker at {now_hour}h"


def test_a_nudge_never_leaves_the_chart():
    # The wettest bar is the first one, half under the low's label at 7 am:
    # the only free side is off the chart, so the label goes, not the edge.
    _, rec = chart_with({6: 30, "rest": 10}, {7: 15, 17: 25, "rest": 20})
    for text, box in rec.marks["labels"]:
        assert box[0] >= 10 and box[2] <= 10 + 270 + 1, f"{text} leaves the chart"
    assert "15°" in dict(rec.marks["labels"])


def test_extremes_step_aside_from_the_now_marker():
    _, rec = chart_with({"rest": 50}, {14: 30, 9: 25, "rest": 20}, now_hour=14)  # the high is at the now hour
    nx, top, base = rec.marks["now"]
    labels = dict(rec.marks["labels"])
    assert not overlap(labels["30°"], (nx - 1, top, nx + 1, base))
    _, rec = chart_with({"rest": 50}, {9: 30, 14: 25, "rest": 20}, now_hour=14)  # an ordinary hour
    assert not overlap(dict(rec.marks["labels"])["30°"], (nx - 1, top, nx + 1, base))


def test_no_rain_labels_on_a_dry_day_even_with_room_for_them():
    s = preset("morning")
    fc = forecast(s)
    d = date(2026, 11, 14)
    for h in fc.day(d).hours:  # dry, with both extremes well away from the 6 am bar
        h.rain_prob = 0
        h.temp = {12: 25, 15: 15}.get(h.hour, 20)
    _, rec = chart_canvas(fc, d, None)
    chart = [t for _, t, _ in rec.texts]
    assert not [t for t in chart if t.endswith("%")]
    assert "25°" in chart and "15°" in chart


def test_centred_labels_are_nudged_inside_the_chart():
    from PIL import Image, ImageDraw

    d = ImageDraw.Draw(Image.new("P", (10, 10)))
    half = d.textlength("100%", font=R.CB(10)) / 2 + 2
    assert R._clamp_x(d, 0, "100%", R.CB(10), 10, 200) == 10 + half
    assert R._clamp_x(d, 500, "100%", R.CB(10), 10, 200) == 210 - half
    assert R._clamp_x(d, 100, "100%", R.CB(10), 10, 200) == 100


def test_recorder_boxes_cover_what_was_drawn():
    rec = R.Recorder(R.new_canvas((50, 50), WHITE))
    rec.section = "s"
    rec.line([(10, 5), (30, 5)], fill=BLACK, width=2)  # level: taller, not longer
    assert rec.boxes["s"] == (10, 4, 31, 7)
    rec.boxes.clear()
    rec.line([(5, 10), (5, 30)], fill=BLACK, width=2)  # upright: wider, not taller
    assert rec.boxes["s"] == (4, 10, 7, 31)
    rec.boxes.clear()
    rec.line([(5, 5), (20, 20)], fill=BLACK, width=4)  # slanted: grows both ways
    assert rec.boxes["s"] == (3, 3, 23, 23)


def test_extremes_are_labelled_last_in_their_inks(cfg):
    _, layout = render_preset("afternoon", cfg)
    chart = [(t, ink) for s, t, ink in layout["texts"] if s == "chart"]
    assert chart[-2][1] == BLUE and chart[-1][1] == RED
    assert chart[-1][0] == "34°"


def test_the_evening_chart_is_tomorrows_and_has_no_now_marker(cfg):
    _, layout = render_preset("night", cfg)
    assert "now" not in layout["marks"]
    assert ("26°", RED) in [(t, ink) for s, t, ink in layout["texts"] if s == "chart"]  # tomorrow's high


def test_sun_row_and_southern_moon(cfg):
    _, layout = render_preset("afternoon", cfg)
    sun = texts(layout, "sun")
    assert sun[0] == "↑5:45am ↓7:35pm" and sun[1] == "13h 50m daylight"


# ------------------------------------------------------------------ tiered refresh


def app_for(tmp_path, state):
    return Almanac(
        Config(cache_dir=tmp_path),
        weather_loader=lambda: weather.parse(payload(state["s"]), now_of(state["s"]).timestamp() - 600),
        photo_loader=lambda d, s: photos.Photo("1", "P", "c", vivid()),
        conditions_loader=lambda d, t, h: state.get("cond"),
        bom_loader=lambda: {},
    )


QUIET = preset("morning")  # 7:01, nothing scores
TIER30 = dict(QUIET, rainP=90)  # rain by chance: 54
TIER15 = dict(QUIET, rainP=90, rainMm=5)  # 80
TIER5 = dict(QUIET, cond="hail", rainP=90)  # hail: 100
T0 = now_of(QUIET)


def at(minutes):
    return T0 + timedelta(minutes=minutes)


def test_no_tier_no_fact_alert(tmp_path):
    state = {"s": preset("afternoon")}  # tops out at 30
    assert app_for(tmp_path, state).alert(now_of(state["s"])) == ""


def test_a_tier_raises_the_alert_at_once(tmp_path):
    state = {"s": QUIET}
    app = app_for(tmp_path, state)
    assert app.alert(at(0)) == ""
    state["s"] = TIER30
    raised = app.alert(at(2))
    assert raised.startswith("wx:") and len(raised) == 11
    assert app.frame_info(at(2))["alert"] == raised


@pytest.mark.parametrize("tier, changed, minutes", [
    (TIER5, dict(TIER5, rainP=95), 5),
    (TIER15, dict(TIER15, rainMm=6), 15),
    (TIER30, dict(TIER30, rainP=95), 30),
])
def test_a_changed_digest_waits_for_the_tiers_interval(tmp_path, tier, changed, minutes):
    state = {"s": tier}
    app = app_for(tmp_path, state)
    first = app.alert(at(0))
    state["s"] = changed
    assert app.alert(at(1)) == first
    assert app.alert(at(minutes - 1)) == first  # held
    later = app.alert(at(minutes))
    assert later.startswith("wx:") and later != first  # re-checked


def test_an_unchanged_digest_keeps_the_id(tmp_path):
    state = {"s": TIER30}
    app = app_for(tmp_path, state)
    first = app.alert(at(0))
    state["s"] = dict(TIER30, tMin=12, tMax=25)  # temperatures move; the facts shown don't
    assert app.alert(at(31)) == first  # re-checked after the interval: same digest, same id


def test_a_tier_that_drops_mid_interval_does_not_flicker(tmp_path):
    state = {"s": TIER30}
    app = app_for(tmp_path, state)
    first = app.alert(at(0))
    state["s"] = QUIET  # the fact vanishes
    seen = {app.alert(at(m)) for m in range(1, 30)}
    assert seen == {first}  # no clear (and so no clear-then-raise) inside the interval
    assert app.alert(at(30)) == ""  # one clear, at the interval's end
    state["s"] = TIER30
    assert app.alert(at(31)) == first  # and the same facts again raise the same id


def test_a_higher_tier_is_noticed_at_its_own_pace(tmp_path):
    state = {"s": TIER30}
    app = app_for(tmp_path, state)
    first = app.alert(at(0))
    state["s"] = TIER5
    assert app.alert(at(4)) == first
    assert app.alert(at(5)) != first  # five minutes, not thirty


def test_alert_joins_hazards_and_the_digest(tmp_path):
    s = preset("worstPhoto")
    state = {"s": s, "cond": conds(s)}
    app = app_for(tmp_path, state)
    now = now_of(s)
    hazard, wx = app.alert(now).split("+")
    assert hazard == state["cond"].alert and wx.startswith("wx:")
    srv, base = _serve(app)
    try:
        assert json.load(urllib.request.urlopen(base + "/alert.json?at=2026-09-30T17:02")) == {"id": app.alert(now)}
    finally:
        srv.shutdown()


def test_a_tier_mid_hour_makes_a_new_poster(tmp_path):
    state = {"s": QUIET}
    app = app_for(tmp_path, state)
    before = app.frame_info(at(0))
    state["s"] = TIER5
    after = app.frame_info(at(29))  # the same hour slot
    assert after["alert"].startswith("wx:") and after["hash"] != before["hash"]


def test_the_forecast_refreshes_as_often_as_the_tier(tmp_path):
    state = {"s": QUIET}
    app = app_for(tmp_path, state)
    assert app.weather_ttl() == weather.TTL_SECONDS
    for tier, ttl in ((TIER30, weather.TTL_SECONDS), (TIER15, 14 * 60), (TIER5, 4 * 60)):  # each a minute under its tier
        app._held_fact = None
        state["s"] = tier
        app.alert(at(0))
        assert app.weather_ttl() == ttl


def test_weather_load_honours_a_shorter_ttl(tmp_path):
    c = Config(cache_dir=tmp_path)
    weather.load(c, now=1000, fetch=lambda url: payload(QUIET))
    fresh = weather.load(c, now=1300, fetch=lambda url: payload(QUIET), ttl=240)
    assert fresh.fetched_at == 1300
    cached = weather.load(c, now=1500, fetch=lambda url: payload(QUIET))
    assert cached.fetched_at == 1300  # the default TTL still uses the cache


def test_broken_facts_never_break_the_alert(tmp_path, monkeypatch):
    import almanac.facts as F

    def boom(*a, **k):
        raise ValueError("bad data")

    monkeypatch.setattr(F, "coming_hour", boom)
    hs = [hz.Hazard("storm", "watch", "Severe Thunderstorm Warning", None, "BOM", ident="BOM:x")]
    state = {"s": preset("worstCold"), "cond": conditions.Conditions(hazards=hs, alert=hz.alert_id(hs))}
    assert app_for(tmp_path, state).alert(now_of(state["s"])) == hz.alert_id(hs)


# ------------------------------------------------------------------ text helpers


def test_wrapped_values_never_strand_a_unit():
    from PIL import Image, ImageDraw

    d = ImageDraw.Draw(Image.new("P", (10, 10)))
    f = R.CB(18)
    width = d.textlength("NW→S gust 148", font=f) + 4  # "km/h" can't join that line
    lines = R.wrap(d, R.glue_units("NW→S gust 148 km/h"), f, width, 3)
    assert lines == ["NW→S gust", "148 km/h"]
    assert R.glue_units("100% · 88 mm") == "100% · 88 mm"


def test_wrap_narrows_only_the_first_line_for_a_prefix():
    from PIL import Image, ImageDraw

    d = ImageDraw.Draw(Image.new("P", (10, 10)))
    f = R.C(14)
    text = "Cloudy. Shower or two, clearing later"
    width = d.textlength("Cloudy. Shower or two,", font=f) + 2
    assert R.wrap(d, text, f, width, 2) == ["Cloudy. Shower or two,", "clearing later"]
    indent = d.textlength("Tomorrow: ", font=f)
    indented = R.wrap(d, text, f, width, 3, first_indent=indent)
    assert " ".join(indented) == text  # re-broken, not cut short with an ellipsis
    assert d.textlength(indented[0], font=f) <= width - indent
