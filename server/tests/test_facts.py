"""Fact scoring: one test per rule, each threshold checked on both sides."""

import random
from datetime import date, datetime

import pytest

from almanac import climate, facts, normals, weather
from almanac.facts import Inputs, score
from almanac.palette import BLACK, BLUE, ORANGE, RED
from panel_fixtures import SYD, forecast, preset


class Fixed:
    """Normals pinned to round numbers, so these tests don't move when the
    placeholder table is replaced."""

    def normal_max(self, month, hour=None):
        return 26

    def normal_uv(self, month, hour=None):
        return 11


N = Fixed()


def I(**kw):
    base = dict(hour=14, month=11, code=1)
    base.update(kw)
    return Inputs(**base)


def by_key(inputs):
    return {f.key: f for f in score(inputs, N)}


def only(inputs, key):
    return by_key(inputs).get(key)


# ------------------------------------------------------------------ helpers


@pytest.mark.parametrize("x, want", [(2.5, 3), (2.49, 2), (0, 0), (-4.5, -5), (-4.4, -4)])
def test_half_up_rounds_half_away_from_zero(x, want):
    assert facts.half_up(x) == want


@pytest.mark.parametrize("mm, text", [(0.0, "0.0"), (0.6, "0.6"), (0.99, "1.0"), (1.0, "1"), (12.4, "12"), (88, "88")])
def test_mm_format(mm, text):
    assert facts.fmt_mm(mm) == text


@pytest.mark.parametrize("deg, point", [(0, "N"), (22.4, "N"), (22.5, "NE"), (180, "S"), (202.4, "S"), (202.5, "SW"),
                                        (315, "NW"), (350, "N"), (-90, "W")])
def test_compass_points(deg, point):
    assert facts.compass(deg) == point


@pytest.mark.parametrize("a, b, want", [(0, 90, 90), (350, 80, 90), (315, 180, 135), (10, 350, 20), (0, 180, 180)])
def test_turn_is_the_smaller_angle(a, b, want):
    assert facts.turn(a, b) == want


# ------------------------------------------------------------------ hazards


@pytest.mark.parametrize("code", [96, 99])
def test_hail_scores_100(code):
    f = only(I(code=code), "hail")
    assert (f.score, f.value, f.label, f.ink, f.hazard) == (100, "Hail", "next hour", RED, True)


def test_storms_score_90():
    f = only(I(code=95), "storms")
    assert (f.score, f.value, f.label, f.ink, f.hazard) == (90, "Storms", "next hour", RED, True)


def test_storms_later_score_60_unless_storms_are_already_here():
    f = only(I(storms_later=True), "storms-later")
    assert (f.score, f.value, f.label, f.hazard) == (60, "Storms later", "today", True)
    assert "storms-later" not in by_key(I(code=95, storms_later=True))
    assert "storms-later" not in by_key(I(code=96, storms_later=True))


@pytest.mark.parametrize("code", [45, 48])
def test_fog_scores_50(code):
    f = only(I(code=code), "fog")
    assert (f.score, f.value, f.label, f.ink, f.hazard) == (50, "Fog", "low visibility", BLACK, True)


def test_fog_and_storms_later_can_both_stand():
    assert {"fog", "storms-later"} <= set(by_key(I(code=45, storms_later=True)))


def test_an_ordinary_sky_is_no_hazard():
    assert not [f for f in score(I(code=3), N) if f.hazard]


# ------------------------------------------------------------------ rain


def test_rain_depth_from_0_2_mm():
    assert only(I(rain_mm=0.2, rain_prob=10), "rain").score == 42  # 40 + 0.2 * 8
    assert only(I(rain_mm=0.19, rain_prob=10), "rain") is None


def test_rain_depth_bonus_tops_out_at_40():
    assert only(I(rain_mm=5, rain_prob=90), "rain").score == 80
    assert only(I(rain_mm=88, rain_prob=100), "rain").score == 80
    assert only(I(rain_mm=4.9, rain_prob=90), "rain").score == 79


def test_rain_chance_from_25_percent_without_depth():
    assert only(I(rain_mm=0.1, rain_prob=25), "rain").score == 15  # 25 * 0.6
    assert only(I(rain_mm=0.1, rain_prob=24), "rain") is None
    assert only(I(rain_prob=90), "rain").score == 54


def test_rain_value_always_shows_chance_and_depth():
    assert only(I(rain_mm=0.6, rain_prob=60), "rain").value == "60% · 0.6 mm"
    assert only(I(rain_mm=0, rain_prob=60), "rain").value == "60% · 0.0 mm"
    f = only(I(rain_mm=12, rain_prob=100), "rain")
    assert (f.value, f.label, f.ink) == ("100% · 12 mm", "Rain next hr", BLUE)


@pytest.mark.parametrize("code", sorted(facts.SNOW))
def test_snow_says_snow(code):
    assert only(I(code=code, rain_mm=2, rain_prob=90), "rain").label == "Snow next hr"


# ------------------------------------------------------------------ since 9am


def test_since_9am_from_1_mm():
    f = only(I(since9_mm=1, last_hr_mm=0.4), "since9")
    assert (f.score, f.value, f.sub, f.label, f.ink) == (22, "1 mm", "0.4 mm last hr", "Since 9am", BLUE)
    assert only(I(since9_mm=0.99), "since9") is None


def test_since_9am_bonus_tops_out_at_30():
    assert only(I(since9_mm=15), "since9").score == 50
    assert only(I(since9_mm=248), "since9").score == 50
    assert only(I(since9_mm=14), "since9").score == 48


def test_since_9am_value_and_last_hour():
    f = only(I(since9_mm=18, last_hr_mm=7), "since9")
    assert (f.value, f.sub) == ("18 mm", "7 mm last hr")


# ------------------------------------------------------------------ wind


def test_southerly_change():
    f = only(I(dir_now=315, dir_next=180, wind=38, gust=85), "wind-change")
    assert f.score == 88  # 55 + 10 (to S) + (85 - 40) * 0.5 = 87.5
    assert (f.label, f.value, f.ink) == ("Southerly change", "NW→S gust 85 km/h", ORANGE)


def test_a_change_to_anywhere_but_south_is_a_wind_change():
    f = only(I(dir_now=0, dir_next=90, wind=20, gust=30), "wind-change")
    assert (f.score, f.label, f.value) == (55, "Wind change", "N→E gust 30 km/h")


def test_change_needs_90_degrees():
    assert only(I(dir_now=0, dir_next=90, wind=25), "wind-change") is not None
    assert only(I(dir_now=0, dir_next=89, wind=25), "wind-change") is None
    assert only(I(dir_now=350, dir_next=80, wind=25), "wind-change") is not None  # across north


def test_change_needs_wind_or_gusts_of_20():
    assert only(I(dir_now=0, dir_next=180, wind=20, gust=0), "wind-change") is not None
    assert only(I(dir_now=0, dir_next=180, wind=0, gust=20), "wind-change") is not None
    assert only(I(dir_now=0, dir_next=180, wind=19.9, gust=19.9), "wind-change") is None


def test_change_gust_bonus_only_above_40():
    assert only(I(dir_now=0, dir_next=90, wind=20, gust=40), "wind-change").score == 55
    assert only(I(dir_now=0, dir_next=90, wind=20, gust=50), "wind-change").score == 60


def test_no_direction_means_no_change():
    assert only(I(dir_now=None, dir_next=180, wind=50, gust=60), "wind-change") is None
    assert only(I(dir_now=0, dir_next=None, wind=50, gust=60), "wind-change") is None


def test_a_change_replaces_the_plain_wind_fact():
    keys = by_key(I(dir_now=0, dir_next=180, wind=50, gust=60))
    assert "wind-change" in keys and "wind" not in keys


def test_plain_wind_from_gust_40_or_wind_30():
    assert only(I(gust=40, dir_next=45), "wind").score == 30
    assert only(I(wind=30, gust=30), "wind").score == 30
    assert only(I(gust=60, wind=20), "wind").score == 46  # 30 + 20 * 0.8


def test_breezy_from_gust_25():
    assert only(I(gust=39.9, wind=29.9), "wind").score == 10
    assert only(I(gust=25), "wind").score == 10
    assert only(I(gust=24.9, wind=20), "wind") is None


def test_plain_wind_label_and_value():
    f = only(I(gust=44, wind=21, dir_next=45), "wind")
    assert (f.label, f.value, f.ink) == ("Wind NE", "21 gust 44 km/h", ORANGE)
    assert only(I(gust=44, wind=21), "wind").label == "Wind"


# ------------------------------------------------------------------ UV


def test_uv_two_above_normal():
    f = only(I(uv=13), "uv")
    assert (f.score, f.label, f.value, f.ink) == (40, "UV, above normal", "13 extreme", ORANGE)


def test_uv_eight_or_more():
    assert only(I(uv=12), "uv").score == 12  # one above normal: not "above normal"
    assert only(I(uv=8), "uv").label == "UV"
    assert only(I(uv=7), "uv") is None
    assert only(I(uv=7.5), "uv").value == "8 very high"  # rounds before comparing


def test_uv_only_before_6pm():
    assert only(I(uv=13, hour=17), "uv") is not None
    assert only(I(uv=13, hour=18), "uv") is None


# ------------------------------------------------------------------ the high


def test_high_of_38_scores_75():
    f = only(I(high=38, high_month=11), "high")
    assert (f.score, f.value, f.label, f.ink) == (75, "+12° (38°)", "Today vs normal", RED)
    assert only(I(high=37.4, high_month=11), "high").score == 55


def test_high_of_35_scores_55():
    assert only(I(high=35, high_month=11), "high").score == 55
    assert only(I(high=34, high_month=11), "high").score == 30  # still 8 above normal


def test_six_from_normal_scores_30():
    assert only(I(high=32, high_month=11), "high").score == 30
    assert only(I(high=31, high_month=11), "high") is None
    cold = only(I(high=20, high_month=11), "high")
    assert (cold.score, cold.value, cold.ink) == (30, "-6° (20°)", BLUE)
    assert only(I(high=21, high_month=11), "high") is None


def test_high_label_follows_the_forecast_day():
    assert only(I(high=36, high_month=11, evening=True), "high").label == "Tomorrow vs normal"


def test_no_high_no_fact():
    assert only(I(high=None, high_month=11), "high") is None
    assert only(I(high=40, high_month=None), "high") is None


# ------------------------------------------------------------------ ranking


def test_non_hazard_scores_are_capped_at_89():
    assert only(I(dir_now=315, dir_next=180, gust=148, wind=98), "wind-change").score == 89
    assert only(I(gust=160, wind=100), "wind").score == 89


def test_hail_and_storms_always_lead():
    rnd = random.Random(1)
    for _ in range(500):
        i = I(code=rnd.choice([95, 96, 99]), rain_mm=rnd.uniform(0, 100), rain_prob=rnd.randint(0, 100),
              wind=rnd.uniform(0, 120), gust=rnd.uniform(0, 160), dir_now=rnd.uniform(0, 360),
              dir_next=rnd.uniform(0, 360), uv=rnd.uniform(0, 14), since9_mm=rnd.uniform(0, 300),
              high=rnd.uniform(-5, 48), high_month=rnd.randint(1, 12), hour=rnd.randint(6, 22))
        assert score(i, N)[0].hazard


def test_ranked_highest_first_and_ties_keep_their_order():
    got = score(I(code=45, rain_mm=0.2, rain_prob=50, gust=25, uv=8, high=32, high_month=11), N)
    assert [f.key for f in got] == ["fog", "rain", "high", "uv", "wind"]  # 50, 42, 30, 12, 10


def test_equal_scores_keep_the_listed_order():
    got = score(I(uv=8, gust=30), N)  # UV 12, wind 10
    assert [f.key for f in got] == ["uv", "wind"]
    tie = score(I(rain_prob=50, since9_mm=5), N)  # rain 30, since 9am 30
    assert [f.key for f in tie] == ["rain", "since9"]


def test_a_quiet_hour_has_no_facts():
    assert score(I(), N) == []


def test_default_normals_are_the_climate_normals():
    assert normals.DEFAULT is climate.NORMALS
    assert score(I(high=32, high_month=11)) == score(I(high=32, high_month=11), climate.NORMALS)
    with pytest.raises(ValueError):
        normals.MonthlyNormals(max_temp=(1,) * 11, uv=(1,) * 12)


# ------------------------------------------------------------------ fast refresh


def coming(*fs):
    return facts.Coming(14, None, list(fs))


@pytest.mark.parametrize("top, minutes", [(49, None), (50, 30), (74, 30), (75, 15), (89, 15), (90, 10), (100, 10)])
def test_refresh_tiers_follow_the_top_score(top, minutes):
    assert facts.refresh_minutes(coming(facts.Fact("x", top, "", "", BLUE), facts.Fact("y", 10, "", "", BLUE))) == minutes


def test_no_facts_no_tier():
    assert facts.refresh_minutes(coming()) is None
    assert facts.refresh_minutes(None) is None


def shown(*values):
    return coming(*[facts.Fact(f"k{i}", 60 - i, f"label {i}", v, BLUE, sub="s") for i, v in enumerate(values)])


def test_digest_tracks_what_the_panel_shows():
    a = shown("1", "2", "3", "4", "5")
    assert facts.digest(a) == facts.digest(shown("1", "2", "3", "4", "5"))
    assert len(facts.digest(a)) == 8
    assert facts.digest(a) != facts.digest(shown("1", "2", "3", "4", "6"))  # a table row's value
    assert facts.digest(a) != facts.digest(shown("9", "2", "3", "4", "5"))  # the hero fact
    relabelled = coming(*[facts.Fact(f.key, f.score, "other", f.value, f.ink, f.sub) for f in a.facts])
    assert facts.digest(a) != facts.digest(relabelled)
    resubbed = coming(*[facts.Fact(f.key, f.score, f.label, f.value, f.ink, "t") for f in a.facts])
    assert facts.digest(a) != facts.digest(resubbed)


def test_digest_ignores_what_the_panel_does_not_show():
    a = shown("1", "2", "3", "4", "5")
    assert facts.digest(a) == facts.digest(shown("1", "2", "3", "4", "5", "a sixth, never drawn"))


def S(*pairs):
    return list(pairs)


DRAWN = S(("hail", 100), ("rain", 54), ("wind", 30))


def test_the_same_facts_are_not_worth_a_redraw():
    assert not facts.worth_redrawing(DRAWN, list(DRAWN))
    assert not facts.worth_redrawing([], [])


@pytest.mark.parametrize("rain, redraw", [(59, False), (60, True), (44, False), (43, True)])
def test_score_moves_beyond_the_thresholds(rain, redraw):
    # Worse news: more than WORSE_BY (+6 redraws, +5 waits). Better news:
    # more than BETTER_BY (-11 redraws, -10 waits).
    assert facts.WORSE_BY == 5 and facts.BETTER_BY == 10
    assert facts.worth_redrawing(DRAWN, S(("hail", 100), ("rain", rain), ("wind", 30))) is redraw


def test_the_hero_fact_has_the_same_thresholds():
    drawn = S(("rain", 80), ("wind", 30))
    assert facts.worth_redrawing(drawn, S(("rain", 86), ("wind", 30)))
    assert not facts.worth_redrawing(drawn, S(("rain", 85), ("wind", 30)))
    assert facts.worth_redrawing(drawn, S(("rain", 69), ("wind", 30)))
    assert not facts.worth_redrawing(drawn, S(("rain", 70), ("wind", 30)))


def test_a_fact_appearing_or_going_is_worth_a_redraw():
    assert facts.worth_redrawing(DRAWN, DRAWN + [("uv", 12)])
    assert facts.worth_redrawing(DRAWN, DRAWN[:2])
    assert facts.worth_redrawing(DRAWN, S(("hail", 100), ("rain", 54), ("uv", 30)))  # one in, one out
    assert facts.worth_redrawing([], S(("rain", 54)))


def test_a_new_hero_kind_is_worth_a_redraw():
    assert facts.worth_redrawing(S(("rain", 80), ("wind", 79)), S(("wind", 81), ("rain", 80)))


def test_the_table_reordering_alone_is_not():
    assert not facts.worth_redrawing(S(("hail", 100), ("rain", 54), ("wind", 50)), S(("hail", 100), ("wind", 52), ("rain", 51)))


def test_shown_state_is_the_displayed_kinds_and_scores_in_order():
    c = shown("1", "2", "3", "4", "5", "6")
    assert facts.shown_state(c) == [(f"k{i}", 60 - i) for i in range(5)]
    assert facts.shown_state(None) == []


def test_digest_includes_the_scores():
    a = shown("1", "2", "3", "4", "5")
    rescored = coming(*[facts.Fact(f.key, f.score - 1, f.label, f.value, f.ink, f.sub) for f in a.facts])
    assert facts.digest(a) != facts.digest(rescored)


# ------------------------------------------------------------------ from a forecast


def test_inputs_from_the_worst_photo_forecast():
    s = preset("worstPhoto")
    now = datetime(2026, 9, 30, 17, 1, tzinfo=SYD)
    i = facts.inputs_for(forecast(s), now, now.date(), False)
    assert (i.hour, i.month, i.code, i.rain_prob, i.rain_mm) == (17, 9, 96, 100, 88)
    assert (i.wind, i.gust, i.dir_now, i.dir_next, i.uv) == (98, 148, 315, 180, 13)
    assert i.since9_mm == pytest.approx(248, abs=0.1) and i.last_hr_mm == 96
    assert (i.high, i.high_month, i.evening) == (46, 9, False)


def test_since_9am_counts_only_completed_hours_after_nine():
    s = preset("worstPhoto")
    fc = forecast(s)
    d = date(2026, 9, 30)
    for h in fc.day(d).hours:
        h.rain_mm = 1.0  # one mm in every hour of the day
    at = lambda hour: facts.inputs_for(fc, datetime(2026, 9, 30, hour, 30, tzinfo=SYD), d, False)
    assert at(9).since9_mm == 0  # 9:30: no hour since 9 has ended
    assert at(10).since9_mm == 1  # 9-10
    assert at(17).since9_mm == 8  # 9-10 ... 4-5
    assert at(17).last_hr_mm == 1


def test_storms_later_reads_the_rest_of_the_day():
    s = preset("morning")
    fc = forecast(s)
    now = datetime(2026, 11, 14, 7, 1, tzinfo=SYD)
    assert not facts.inputs_for(fc, now, now.date(), False).storms_later
    fc.day(now.date()).hours[16].code = 95
    assert facts.inputs_for(fc, now, now.date(), False).storms_later
    coming = datetime(2026, 11, 14, 15, 1, tzinfo=SYD)  # the storm hour is the coming one, not "later"
    assert not facts.inputs_for(fc, coming, now.date(), False).storms_later
    assert facts.inputs_for(fc, coming.replace(hour=14), now.date(), False).storms_later  # an hour before


def test_the_evening_compares_tomorrows_high():
    s = preset("night")
    now = datetime(2026, 11, 14, 21, 1, tzinfo=SYD)
    i = facts.inputs_for(forecast(s), now, date(2026, 11, 15), True)
    assert (i.high, i.high_month, i.evening) == (26, 11, True)


def test_the_high_is_compared_with_the_forecast_days_month():
    s = preset("worstPhoto")  # 30 September; the evening forecast is 1 October's
    now = datetime(2026, 9, 30, 21, 1, tzinfo=SYD)
    i = facts.inputs_for(forecast(s), now, date(2026, 10, 1), True)
    assert (i.month, i.high_month, i.high) == (9, 10, 47)


def test_no_coming_hour_without_data():
    s = preset("morning")
    assert facts.inputs_for(None, datetime(2026, 11, 14, 7, 1, tzinfo=SYD), date(2026, 11, 14), False) is None
    beyond = datetime(2026, 11, 16, 23, 1, tzinfo=SYD)  # past the forecast's last hour
    assert facts.coming_hour(forecast(s), beyond, beyond.date(), False) is None


def test_coming_hour_ranks_the_forecast():
    s = preset("worstCold")
    now = datetime(2026, 6, 24, 7, 1, tzinfo=SYD)
    c = facts.coming_hour(forecast(s), now, now.date(), False)
    assert c.label_hour == 7 and c.hour.hour == 8
    assert [f.key for f in c.facts][:2] == ["wind-change", "rain"]
    assert c.facts[1].label == "Snow next hr"
    assert facts.refresh_minutes(c) == 15  # 81


def test_weather_parses_wind_direction_and_tolerates_its_absence():
    p = __import__("panel_fixtures").payload(preset("worstPhoto"))
    h = weather.parse(p, 0).day(date(2026, 9, 30)).hours[18]
    assert h.wind_dir == 180
    del p["hourly"]["wind_direction_10m"]
    assert weather.parse(p, 0).day(date(2026, 9, 30)).hours[18].wind_dir is None
