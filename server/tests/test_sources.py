"""Which source wins: the model's hourly temperatures anchored to the official
outlook (anchor.py), end to end through the app's composition; a fresh
station temperature as the coming hour's (facts.Coming.temp); and the day's
rain on the forecast line from the outlook only.

Central Sydney fixtures only (panel_fixtures)."""

from datetime import date, datetime, timedelta

import pytest

import almanac.render as R
from almanac import anchor, facts, observed
from almanac.app import Almanac
from almanac.config import Config
from almanac.domain import Day, DayOutlook, Forecast, Hour, Reading, StationLogs
from almanac.palette import BLACK, BLUE, RED, WHITE
from almanac.providers import commons as photos, openmeteo as weather
from panel_fixtures import CASES, SYD, forecast, now_of, payload, preset, render_preset
from test_almanac import vivid

D = date(2026, 11, 14)


def day_of(temps, d=D):
    """A Day whose hour h has temps[h] (24 values from midnight)."""
    hours = [Hour(hour=h, temp=t, rain_prob=0, code=1) for h, t in enumerate(temps)]
    return Day(d, 1, max(temps), min(temps), 0, 0.0, 0.0, datetime(2026, 11, 14, 5, 45),
               datetime(2026, 11, 14, 19, 35), hours)


def anchored(temps, **outlook):
    fc = Forecast(days={D: day_of(temps)}, current_temp=None, fetched_at=0)
    anchor.anchor_temps(fc, {D: DayOutlook(**outlook)})
    return [h.temp for h in fc.day(D).hours]


# A model day: 14.2 at 5 am, 18.4 at 3 pm, and a late evening (23:00, 13.0)
# cooler than the dawn.
MODEL = [15.0, 14.8, 14.6, 14.5, 14.3, 14.2, 14.4, 15.1, 16.0, 16.8, 17.4, 17.8, 18.1, 18.3, 18.4, 18.4,
         18.2, 17.6, 16.9, 16.0, 15.2, 14.6, 13.8, 13.0]
PEAK_AT, TROUGH_AT = 14, 5  # the first of the two 18.4s


def order(ts):
    return sorted(range(len(ts)), key=lambda i: (ts[i], i))


def test_both_ends_map_the_peak_and_trough_exactly_and_keep_the_shape():
    out = anchored(MODEL, t_max=21, t_min=12)
    assert max(out[6:22]) == 21 and out[PEAK_AT] == 21 and out[15] == 21  # exactly, both peak hours
    assert min(out[0:10]) == 12 and out[TROUGH_AT] == 12
    assert order(out) == order(MODEL)  # every hour keeps its rank: same timing
    assert all(12 < t < 21 for i, t in enumerate(out) if MODEL[i] not in (14.2, 18.4) and 14.2 < MODEL[i] < 18.4)


def test_between_the_anchors_the_map_is_linear():
    out = anchored(MODEL, t_max=21, t_min=12)
    k = (21 - 12) / (18.4 - 14.2)
    assert out[9] == pytest.approx(12 + (16.8 - 14.2) * k)


def test_hours_beyond_the_anchors_move_with_slope_one():
    # 23:00 is 1.2 below the dawn trough in the model; stretched 2.1x it would
    # land 2.6 below BOM's min, so it continues at slope 1 instead.
    out = anchored(MODEL, t_max=21, t_min=12)
    assert out[23] == pytest.approx(12 - 1.2)


def test_the_peak_is_the_daytime_peak_not_a_hot_night():
    # A northerly night warmer than the day (23:00 at 19.0, above the 18.4
    # afternoon): the chart's hours set the peak, and the late hour rides
    # above BOM's max at slope 1.
    hot_night = MODEL[:23] + [19.0]
    out = anchored(hot_night, t_max=21, t_min=12)
    assert out[PEAK_AT] == 21 and max(out[6:22]) == 21
    assert out[23] == pytest.approx(21 + 0.6)


def test_a_missing_min_shifts_the_day_to_the_max():
    out = anchored(MODEL, t_max=21)
    assert out[PEAK_AT] == 21
    assert [b - a for a, b in zip(out, out[1:])] == pytest.approx([b - a for a, b in zip(MODEL, MODEL[1:])])
    assert out[TROUGH_AT] == pytest.approx(14.2 + 21 - 18.4)


def test_a_missing_max_shifts_the_day_to_the_min():
    out = anchored(MODEL, t_min=12)
    assert out[TROUGH_AT] == 12
    assert out[PEAK_AT] == pytest.approx(18.4 + 12 - 14.2)
    assert order(out) == order(MODEL)


def test_no_figures_change_nothing():
    assert anchored(MODEL) == MODEL
    fc = Forecast(days={D: day_of(MODEL)}, current_temp=None, fetched_at=0)
    assert anchor.anchor_temps(fc, {}) is fc and [h.temp for h in fc.day(D).hours] == MODEL
    assert anchor.anchor_temps(None, {D: DayOutlook(t_max=20)}) is None


def test_a_flat_model_day_shifts_to_the_max():
    out = anchored([17.0] * 24, t_max=21, t_min=12)
    assert out == [21] * 24


def test_a_flat_model_day_without_a_max_shifts_to_the_min():
    assert anchored([17.0] * 24, t_min=12) == [12] * 24


def test_a_max_below_the_model_min_still_hits_the_max():
    # BOM's max (12) is under every model hour (14.2 and up): a pure shift.
    out = anchored(MODEL, t_max=12)
    assert max(out[6:22]) == 12 and out[PEAK_AT] == 12
    assert order(out) == order(MODEL)


def test_a_max_at_or_below_the_min_lets_the_max_win():
    # An order-preserving map can't put the peak below the trough, so the
    # figure the header prints (the max) wins and the shape is kept.
    for t_min in (16, 18):
        out = anchored(MODEL, t_max=16, t_min=t_min)
        assert out[PEAK_AT] == 16
        assert order(out) == order(MODEL)
        assert [b - a for a, b in zip(out, out[1:])] == pytest.approx([b - a for a, b in zip(MODEL, MODEL[1:])])


@pytest.mark.parametrize("outlook", [dict(t_max=21, t_min=12), dict(t_max=21), dict(t_min=12), dict(t_max=16, t_min=18)])
def test_anchoring_twice_changes_nothing(outlook):
    once = anchored(MODEL, **outlook)
    assert anchored(once, **outlook) == pytest.approx(once)


def test_only_outlook_days_are_anchored_and_other_fields_survive():
    other = date(2026, 11, 15)
    fc = Forecast(days={D: day_of(MODEL), other: day_of(MODEL, other)}, current_temp=None, fetched_at=0)
    fc.day(D).hours[9].wind = 33.0
    anchor.anchor_temps(fc, {D: DayOutlook(t_max=21, t_min=12)})
    assert [h.temp for h in fc.day(other).hours] == MODEL
    assert fc.day(D).hours[9].wind == 33.0 and fc.day(D).hours[9].hour == 9


# ------------------------------------------------------------------ end to end


def app_with(tmp_path, s, outlook):
    return Almanac(
        Config(cache_dir=tmp_path),
        weather_loader=lambda: weather.parse(payload(s), now_of(s).timestamp() - 600),
        photo_loader=lambda d, n: photos.Photo("1", "P", "c", vivid()),
        conditions_loader=lambda d, t, h: None,
        bom_loader=lambda: outlook,
    )


def panel(app, now, cfg):
    from almanac import schedule

    day, is_tomorrow = schedule.poster_day(now, cfg.tomorrow_from_hour)
    layout = {}
    R.render(now=now, poster_day=day, is_tomorrow=is_tomorrow, forecast=app.forecast(), photo=None, cfg=cfg,
             layout=layout)
    return layout


def chart_max_label(layout):
    return [t for s, t, ink in layout["texts"] if s == "chart" and ink == RED][-1]


def texts(layout, section):
    return [t for s, t, _ in layout["texts"] if s == section]


def test_the_header_max_equals_the_chart_max_label(tmp_path):
    s = preset("afternoon")  # 2:01 pm; the model's day peaks at 34° at 3 pm
    now = now_of(s)
    app = app_with(tmp_path, s, {now.date(): DayOutlook(t_max=38, t_min=16, source="BOM")})
    layout = panel(app, now, app.cfg)
    assert texts(layout, "header.pair")[:2] == ["HIGH TODAY", "38°"]
    assert chart_max_label(layout) == "38°"
    assert texts(layout, "hero")[:2] == ["2–3PM", "38°"]  # the coming hour is the peak hour
    high = next(f for f in layout["facts"] if f.key == "high")
    assert high.value.endswith("(38°)")


def test_the_header_max_equals_the_chart_max_label_with_todays_min_dropped(tmp_path):
    s = preset("afternoon")
    now = now_of(s)
    app = app_with(tmp_path, s, {now.date(): DayOutlook(t_max=21, source="BOM")})
    layout = panel(app, now, app.cfg)
    assert texts(layout, "header.pair")[1] == "21°"
    assert chart_max_label(layout) == "21°"


def test_the_evening_chart_is_anchored_to_tomorrows_outlook(tmp_path):
    s = preset("night")  # 9:01 pm: tomorrow's poster; the model says 26°
    now = now_of(s)
    tomorrow = now.date() + timedelta(days=1)
    app = app_with(tmp_path, s, {now.date(): DayOutlook(t_max=33, t_min=15, source="BOM"),  # today's, listed first
                                 tomorrow: DayOutlook(t_max=29, t_min=17, source="BOM")})
    layout = panel(app, now, app.cfg)
    assert texts(layout, "header.pair")[:2] == ["LOW TONIGHT", "17°"]
    assert "29°" in texts(layout, "header.pair")
    assert chart_max_label(layout) == "29°"


def test_without_an_outlook_the_model_curve_is_drawn_as_is(tmp_path):
    s = preset("afternoon")
    app = app_with(tmp_path, s, {})
    assert chart_max_label(panel(app, now_of(s), app.cfg)) == "34°"


# ------------------------------------------------------------------ observed now


NOW = datetime(2026, 11, 14, 14, 1, tzinfo=SYD)


def obs(age_minutes, temp=23.6):
    return observed.Observed("Horsley Park", "Horsley Park AWS", NOW - timedelta(minutes=age_minutes), temp=temp)


@pytest.mark.parametrize("age, used", [(0, True), (29, True), (30, True), (31, False), (90, False)])
def test_the_observed_temperature_is_used_only_while_fresh(age, used):
    c = facts.coming_hour(forecast(preset("afternoon")), NOW, NOW.date(), False, observed=obs(age))
    assert c.temp == (23.6 if used else None)


def test_no_observed_temperature_leaves_the_forecast():
    fc = forecast(preset("afternoon"))
    assert facts.coming_hour(fc, NOW, NOW.date(), False, observed=obs(1, temp=None)).temp is None
    assert facts.coming_hour(fc, NOW, NOW.date(), False).temp is None


def test_the_hero_draws_the_observed_temperature():
    R.set_layout(500)
    hour = Hour(hour=15, temp=31.0, rain_prob=0, code=1)
    for temp, want in ((23.6, "24°"), (None, "31°")):
        rec = R.Recorder(R.new_canvas((800, 200), WHITE))
        R._hero(rec, facts.Coming(14, hour, [], temp=temp), R.SIDE_X, 0)
        assert [t for s, t, _ in rec.texts if s == "hero"][1] == want


def test_the_panel_shows_a_fresh_station_temperature(tmp_path):
    s = preset("afternoon")
    now = now_of(s)
    layout = {}
    R.render(now=now, poster_day=now.date(), is_tomorrow=False, forecast=forecast(s), photo=None,
             cfg=Config(cache_dir=tmp_path), layout=layout, observed=obs(5, temp=27.2))
    assert texts(layout, "hero")[1] == "27°"


def test_summarise_carries_the_here_stations_temperature():
    at = datetime(2026, 11, 16, 11, 0, tzinfo=SYD)
    r = Reading(station="94760", name="Horsley Park", credit="Horsley Park AWS", at=at, temp=24.1)
    logs = StationLogs(logs={"94760": [r]}, home="94760", source="BOM")
    assert observed.summarise(logs, at + timedelta(minutes=10)).temp == 24.1
    assert observed.summarise(logs, at + timedelta(minutes=31)) is None  # stale: no Observed at all


# ------------------------------------------------------------------ the day's rain


def forecast_texts(layout):
    return [(t, ink) for s, t, ink in layout["texts"] if s == "forecast"]


def rain_panel(tmp_path, s, outlook, now=None):
    app = app_with(tmp_path, s, outlook)
    return panel(app, now or now_of(s), app.cfg)


def test_the_days_rain_comes_from_the_outlook_with_its_range(tmp_path):
    s = preset("afternoon")
    d = now_of(s).date()
    layout = rain_panel(tmp_path, s, {d: DayOutlook(precis="Showers. Possible storm", rain_prob=80,
                                                    rain_range="2 to 8 mm", source="BOM")})
    assert ("80% · 2–8 mm", BLUE) in forecast_texts(layout)
    assert texts(layout, "forecast")[0] == "Showers. Possible storm"  # appended to the précis line


def test_without_a_range_only_the_chance_shows(tmp_path):
    s = preset("afternoon")
    layout = rain_panel(tmp_path, s, {now_of(s).date(): DayOutlook(precis="Shower or two", rain_prob=60, source="BOM")})
    assert ("60%", BLUE) in forecast_texts(layout)
    assert not any("mm" in t for t, _ in forecast_texts(layout))


def test_a_low_chance_is_black(tmp_path):
    s = preset("afternoon")
    layout = rain_panel(tmp_path, s, {now_of(s).date(): DayOutlook(precis="Sunny", rain_prob=10,
                                                                   rain_range="0 to 1 mm", source="BOM")})
    assert ("10% · 0–1 mm", BLACK) in forecast_texts(layout)


def test_no_outlook_no_daily_rain(tmp_path):
    # The model has a daily chance (its precipitation_probability_max) and a
    # daily sum; neither may stand in for BOM's.
    s = preset("afternoon")
    layout = rain_panel(tmp_path, s, {})
    assert texts(layout, "forecast") and not any("%" in t or "mm" in t for t in texts(layout, "forecast"))


def test_the_evening_shows_tomorrows_rain(tmp_path):
    s = preset("night")  # 9:01 pm: the poster describes tomorrow
    today = now_of(s).date()
    layout = rain_panel(tmp_path, s, {
        today: DayOutlook(precis="Cloudy", rain_prob=10, rain_range="0 to 1 mm", source="BOM"),
        today + timedelta(days=1): DayOutlook(precis="Rain", rain_prob=90, rain_range="10 to 20 mm", source="BOM"),
    })
    shown = forecast_texts(layout)
    assert ("90% · 10–20 mm", BLUE) in shown
    assert not any(t.startswith("10%") for t, _ in shown)


@pytest.mark.parametrize("name, map_mode, still", CASES, ids=[c[2] for c in CASES])
def test_the_days_rain_fits_every_fixture_uncut(tmp_path, name, map_mode, still):
    s = preset(name, map_mode)
    _, layout = render_preset(name, Config(cache_dir=tmp_path), map_mode)
    want = R.rain_note(forecast(s).day(now_of(s).date()))[0]
    shown = [t for t, _ in forecast_texts(layout)]
    assert want in shown, shown  # whole, never cut with an ellipsis
    x0, _, x1, _ = layout["boxes"]["forecast"]
    side_x, side_w = layout["side"]
    assert side_x <= x0 and x1 <= side_x + side_w + 1
    assert layout["boxes"]["forecast"][3] < layout["boxes"]["hero"][1]


def forecast_line(label, chance=80, rng="2 to 8 mm", tomorrow=False):
    R.set_layout(500)  # the 270 px photo panel
    fc = day_of(MODEL)
    fc.label_override, fc.rain_chance, fc.rain_text = label, chance, rng
    rec = R.Recorder(R.new_canvas((800, 200), WHITE))
    rec.section = "forecast"
    bottom = R._forecast_line(rec, fc, R.SIDE_X, 0, tomorrow)
    return [t for _, t, _ in rec.texts], bottom


def test_a_precis_cut_short_is_cut_shorter_to_keep_the_rain_inline():
    long = "Very hot. Severe thunderstorms with giant hail, damaging winds and heavy rain likely in the afternoon and evening"
    shown, bottom = forecast_line(long)
    assert len(shown) == 4 and shown[1].endswith("…") and shown[2:] == [" · ", "80% · 2–8 mm"]
    assert bottom == 2 * 17  # no extra line


def test_a_whole_two_line_precis_keeps_its_words_and_the_rain_gets_its_own_line():
    R.set_layout(500)
    rec = R.Recorder(R.new_canvas((800, 200), WHITE))
    tail = rec.textlength(" · 80% · 2–8 mm", font=R.C(14))
    words = []
    for w in "Cloudy with showers developing over the ranges and spreading to the coast later today then".split():
        words.append(w)
        lines = R.wrap(rec, " ".join(words), R.C(14), R.SIDE_W, 2)
        if len(lines) == 2 and rec.textlength(lines[1], font=R.C(14)) + tail > R.SIDE_W:
            break
    else:
        pytest.fail("the probe never filled two lines")
    label = " ".join(words)
    assert len(R.wrap(rec, label, R.C(14), R.SIDE_W, 3)) == 2  # the précis fits two lines whole
    shown, bottom = forecast_line(label)
    assert not any(t.endswith("…") for t in shown)
    assert shown[-1] == "80% · 2–8 mm" and " · " not in shown
    assert bottom == 3 * 17


def test_tomorrows_prefix_leaves_room_for_the_rain():
    shown, bottom = forecast_line("Rain", tomorrow=True)
    assert shown == ["Tomorrow:", "Rain", " · ", "80% · 2–8 mm"] and bottom == 17
