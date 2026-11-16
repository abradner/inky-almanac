"""BOM station observations (providers/bom_obs.py) and what is made of them
(observed.py), and their precedence over modelled figures in facts.py.

The fixture is a live IDN60920 fetch (2026-11-16 11:00 AEDT), trimmed to the
Sydney stations the defaults name. No network here.
"""

import random
import re
import xml.etree.ElementTree as ET
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from almanac import facts, observed
from almanac.config import Config
from almanac.domain import Reading, StationLogs
from almanac.facts import Inputs, score
from almanac.palette import ORANGE
from almanac.providers import bom_obs
from almanac.providers.cache import offline
from panel_fixtures import SYD, forecast, preset

FIXTURE = (Path(__file__).parent / "fixtures" / "bom-idn60920-sydney.xml").read_text()
AT = datetime(2026, 11, 16, 11, 0, tzinfo=SYD)  # the fixture's observation time
NINE = datetime(2026, 11, 16, 9, 0, tzinfo=SYD)


def cfg(tmp_path, **kw):
    return Config(cache_dir=tmp_path, **kw)


# ------------------------------------------------------------------ parsing


def test_parses_horsley_park_from_the_live_fixture():
    r = bom_obs.parse(FIXTURE)["94760"]
    assert (r.name, r.credit, r.at) == ("Horsley Park", "Horsley Park AWS", AT)
    assert (r.temp, r.apparent, r.dew_point, r.humidity) == (17.9, 16.5, 13.4, 75)
    assert (r.wind_dir, r.wind, r.gust, r.max_gust) == (196, 13, 24, 28)
    assert (r.rain, r.rain_start) == (0.0, NINE)
    assert (r.rain_prev_period, r.rain_prev_end) == (0.2, NINE)


def test_only_the_configured_stations_and_a_wind_only_station_has_no_temperature():
    got = bom_obs.parse(FIXTURE, {"94760", "94769"})
    assert set(got) == {"94760", "94769"}
    fd = got["94769"]
    assert fd.credit == "Fort Denison AWS" and fd.temp is None and fd.rain is None
    assert fd.wind_dir is not None and fd.gust is not None


def _shuffle_elements(xml_text, seed):
    root = ET.fromstring(xml_text)
    rnd = random.Random(seed)
    for level in root.iter("level"):
        kids = list(level)
        rnd.shuffle(kids)
        for k in list(level):
            level.remove(k)
        level.extend(kids)
    return ET.tostring(root, encoding="unicode")


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_elements_are_read_by_type_not_position(seed):
    shuffled = _shuffle_elements(FIXTURE, seed)
    assert shuffled != FIXTURE
    assert bom_obs.parse(shuffled) == bom_obs.parse(FIXTURE)


def _station(xml_text, wmo):
    return re.search(rf'<station wmo-id="{wmo}".*?</station>', xml_text, re.S).group(0)


def test_missing_and_malformed_elements_read_as_none():
    block = _station(FIXTURE, "94760")
    trimmed = re.sub(r'\s*<element[^>]*type="(gust_kmh|rainfall|rel-humidity)"[^>]*>[^<]*</element>', "", block)
    trimmed = re.sub(r'(type="air_temperature">)[^<]*', r"\1-", trimmed)
    r = bom_obs.parse(FIXTURE.replace(block, trimmed))["94760"]
    assert (r.gust, r.rain, r.rain_start, r.humidity, r.temp) == (None, None, None, None, None)
    assert r.wind == 13 and r.rain_prev_period == 0.2  # the rest still parses


def test_a_compass_direction_stands_in_for_missing_degrees():
    block = _station(FIXTURE, "94760")
    r = bom_obs.parse(FIXTURE.replace(block, re.sub(r'\s*<element[^>]*type="wind_dir_deg"[^>]*>[^<]*</element>', "", block)))
    assert r["94760"].wind_dir == 202.5  # SSW


def test_a_station_without_an_observation_time_is_skipped():
    block = _station(FIXTURE, "94760")
    assert "94760" not in bom_obs.parse(FIXTURE.replace(block, block.replace('time-local="', 'time-lcl="')))


# ------------------------------------------------------------------ polling and the log


class Feed:
    def __init__(self, *texts):
        self.texts = list(texts)
        self.calls = []

    def __call__(self, url):
        self.calls.append(url)
        t = self.texts.pop(0) if len(self.texts) > 1 else self.texts[0]
        if isinstance(t, Exception):
            raise t
        return t


def _at(xml_text, when):
    """The fixture re-stamped to another observation time."""
    return xml_text.replace("2026-11-16T11:00:00+11:00", when.isoformat())


def test_polls_at_most_every_ten_minutes_and_logs_new_readings(tmp_path):
    t0 = AT.timestamp()
    feed = Feed(FIXTURE, _at(FIXTURE, AT + timedelta(minutes=10)))
    p = bom_obs.BomObservations(cfg(tmp_path), get_text=feed)
    assert p.logs() is None
    assert p.refresh(t0) is True
    assert feed.calls == ["ftp://ftp.bom.gov.au/anon/gen/fwo/IDN60920.xml"]
    assert p.refresh(t0 + 9 * 60) is False and len(feed.calls) == 1  # too soon
    assert p.refresh(t0 + 10 * 60) is True and len(feed.calls) == 2
    logs = p.logs()
    assert isinstance(logs, StationLogs) and logs.home == "94760" and logs.source == "BOM"
    assert [r.at for r in logs.log("94760")] == [AT, AT + timedelta(minutes=10)]
    # A fresh instance (a restart) reads the log, and still waits its turn.
    again = bom_obs.BomObservations(cfg(tmp_path), get_text=feed)
    assert again.refresh(t0 + 15 * 60) is False and len(feed.calls) == 2
    assert again.logs() == logs


def test_the_same_issue_twice_is_logged_once(tmp_path):
    feed = Feed(FIXTURE)
    p = bom_obs.BomObservations(cfg(tmp_path), get_text=feed)
    p.refresh(AT.timestamp())
    p.refresh(AT.timestamp() + 600)
    assert len(p.logs().log("94760")) == 1 and len(feed.calls) == 2


def test_backs_off_on_failure_and_recovers(tmp_path):
    t0 = AT.timestamp()
    feed = Feed(OSError("down"), OSError("down"), FIXTURE)
    p = bom_obs.BomObservations(cfg(tmp_path), get_text=feed)
    assert p.refresh(t0) is False  # fails: next try in 20 min
    assert p.refresh(t0 + 19 * 60) is False and len(feed.calls) == 1
    assert p.refresh(t0 + 20 * 60) is False and len(feed.calls) == 2  # fails again: 40 min
    assert p.refresh(t0 + 59 * 60) is False and len(feed.calls) == 2
    assert p.refresh(t0 + 60 * 60) is True and len(feed.calls) == 3
    assert p._failures == 0


def test_backoff_is_capped_at_an_hour(tmp_path):
    p = bom_obs.BomObservations(cfg(tmp_path), get_text=Feed(OSError("down")))
    t, waits = AT.timestamp(), []
    for _ in range(6):
        p.refresh(t)
        waits.append(p._next_try - t)
        t = p._next_try
    assert waits == [1200, 2400, 3600, 3600, 3600, 3600]


def test_a_product_without_our_stations_is_a_failure_not_an_empty_log(tmp_path):
    p = bom_obs.BomObservations(cfg(tmp_path, obs_station="1", obs_crosscheck=(), obs_leading=()),
                                get_text=Feed(FIXTURE))
    assert p.refresh(AT.timestamp()) is False and p.logs() is None and p._failures == 1


def test_the_request_path_transport_never_fetches(tmp_path):
    p = bom_obs.BomObservations(cfg(tmp_path), get_text=offline)
    assert p.refresh(AT.timestamp()) is False and p.logs() is None


def test_merge_keeps_only_newer_readings_and_prunes_old_ones():
    r = bom_obs.parse(FIXTURE)["94760"]
    old = replace(r, at=AT - timedelta(hours=4))
    logs = bom_obs.merge({"94760": [old]}, {"94760": r}, AT.timestamp())
    assert logs["94760"] == [r]  # the 4-hour-old reading is pruned
    assert bom_obs.merge(logs, {"94760": replace(r, at=AT - timedelta(minutes=10))}, AT.timestamp()) == logs


def test_an_unreadable_log_starts_afresh(tmp_path):
    (tmp_path / bom_obs.LOG_NAME).write_text("{not json")
    p = bom_obs.BomObservations(cfg(tmp_path), get_text=Feed(FIXTURE))
    assert p.logs() is None
    assert p.refresh(AT.timestamp()) is True and p.logs() is not None


# ------------------------------------------------------------------ observed.py


def R(minutes, station="94760", **kw):
    """A reading `minutes` after 11:00 (negative: before)."""
    base = dict(station=station, name=station, credit=f"{station} AWS", at=AT + timedelta(minutes=minutes),
                rain_start=NINE)
    base.update(kw)
    return Reading(**base)


def logs(home=(), crosscheck=None, leading=None):
    out = {"94760": list(home)}
    for group in (crosscheck or {}, leading or {}):
        out.update(group)
    return StationLogs(logs=out, home="94760", crosscheck=tuple(crosscheck or ()), leading=tuple(leading or ()),
                       source="BOM")


def test_a_reading_older_than_thirty_minutes_is_missing():
    log = [R(0, rain=1.0)]
    assert observed.latest(log, AT + timedelta(minutes=30)) is not None
    assert observed.latest(log, AT + timedelta(minutes=31)) is None
    assert observed.summarise(logs(log), AT + timedelta(minutes=31)) is None


def test_a_crosscheck_station_stands_in_when_home_is_silent():
    stale_home = [R(-60, rain=9.0)]
    olympic = [R(0, "95765", rain=2.0)]
    o = observed.summarise(logs(stale_home, crosscheck={"95765": olympic}), AT + timedelta(minutes=5))
    assert (o.station, o.since9_mm, o.credit, o.source) == ("95765", 2.0, "95765 AWS", "BOM")


def test_since_9am_is_the_gauge_and_before_9am_is_not_today():
    o = observed.summarise(logs([R(0, rain=4.2)]), AT)
    assert o.since9_mm == 4.2
    early = R(-240, rain=12.0, rain_start=NINE - timedelta(days=1))  # 7am: since 9am yesterday
    assert observed.summarise(logs([early]), AT - timedelta(hours=4)).since9_mm is None


def test_last_hour_differences_readings_an_hour_apart():
    log = [R(-60, rain=1.0), R(-50, rain=1.5), R(-10, rain=3.0), R(0, rain=3.4)]
    assert observed.rain_last_hour(log, AT) == 2.4


def test_last_hour_picks_the_reading_nearest_an_hour_back_within_tolerance():
    assert observed.rain_last_hour([R(-68, rain=0.5), R(0, rain=3.0)], AT) == 2.5
    assert observed.rain_last_hour([R(-75, rain=0.5), R(0, rain=3.0)], AT) is None  # a gap
    assert observed.rain_last_hour([R(-50, rain=0.5), R(0, rain=3.0)], AT) == 2.5
    assert observed.rain_last_hour([R(-49, rain=0.5), R(0, rain=3.0)], AT) is None
    # two candidates: the one nearer an hour back wins, whatever the order
    assert observed.rain_last_hour([R(-70, rain=0.1), R(-60, rain=0.5), R(0, rain=3.0)], AT) == 2.5


def test_last_hour_is_unknown_across_a_gap():
    assert observed.rain_last_hour([R(-120, rain=0.0), R(0, rain=3.0)], AT) is None
    assert observed.rain_last_hour([R(0, rain=3.0)], AT) is None


def test_last_hour_across_the_9am_reset_uses_the_closing_total():
    yday = NINE - timedelta(days=1)
    t = NINE + timedelta(minutes=30)
    before = R(-150, rain=10.0, rain_start=yday)  # 8:30
    after = R(-90, rain=0.6, rain_start=NINE, rain_prev_period=11.2, rain_prev_end=NINE)  # 9:30
    assert observed.rain_last_hour([before, after], t) == pytest.approx(1.8)  # 1.2 before 9 + 0.6 after


def test_last_hour_across_the_reset_without_a_closing_total_is_unknown():
    yday = NINE - timedelta(days=1)
    t = NINE + timedelta(minutes=30)
    before = R(-150, rain=10.0, rain_start=yday)
    assert observed.rain_last_hour([before, R(-90, rain=0.6, rain_start=NINE)], t) is None
    # ... or a closing total for some other period
    wrong = R(-90, rain=0.6, rain_start=NINE, rain_prev_period=11.2, rain_prev_end=NINE - timedelta(hours=1))
    assert observed.rain_last_hour([before, wrong], t) is None
    # ... or one smaller than what had already fallen (a corrected gauge)
    low = R(-90, rain=2.0, rain_start=NINE, rain_prev_period=9.0, rain_prev_end=NINE)
    assert observed.rain_last_hour([before, low], t) is None  # not 2.0 - 1.0


def test_a_falling_total_is_not_negative_rain():
    assert observed.rain_last_hour([R(-60, rain=3.0), R(0, rain=2.0)], AT) is None


def test_last_hour_ignores_readings_from_the_future():
    log = [R(-60, rain=1.0), R(0, rain=2.0), R(30, rain=9.0)]
    assert observed.rain_last_hour(log, AT + timedelta(minutes=1)) == 1.0


# -- the southerly detector


def nw(minutes, station="94760", **kw):
    return R(minutes, station, **dict(dict(wind_dir=315.0, wind=20.0, gust=30.0, temp=31.0), **kw))


def s_(minutes, station="94760", **kw):
    return R(minutes, station, **dict(dict(wind_dir=190.0, wind=35.0, gust=60.0, temp=24.0), **kw))


def test_southerly_with_a_temperature_drop_and_gust_rise():
    got = observed.southerly([nw(-30), nw(-20), s_(-10), s_(0)], AT)
    assert got is not None and got.at == AT - timedelta(minutes=10)
    assert (got.from_dir, got.to_dir, got.temp_drop, got.gust) == (315, 190, 7, 60)


def test_southerly_on_a_temperature_drop_alone():
    assert observed.southerly([nw(-20), s_(-10, gust=32.0)], AT) is not None


def test_southerly_at_a_wind_only_station_on_the_gust_alone():
    log = [nw(-20, temp=None), s_(-10, temp=None, gust=50.0)]
    assert observed.southerly(log, AT).temp_drop is None


def test_a_wind_swing_alone_is_not_a_southerly():
    # into the sector, but the same air: no temperature drop, no gust rise
    assert observed.southerly([nw(-20), s_(-10, temp=30.0, gust=35.0)], AT) is None
    assert observed.southerly([nw(-20, temp=None), s_(-10, temp=None, gust=35.0)], AT) is None


def test_thresholds_are_inclusive_and_just_under_fails():
    assert observed.southerly([nw(-20), s_(-10, temp=29.0, gust=30.0)], AT) is not None  # drop 2.0
    assert observed.southerly([nw(-20), s_(-10, temp=29.1, gust=44.9)], AT) is None
    assert observed.southerly([nw(-20), s_(-10, temp=31.0, gust=45.0)], AT) is not None  # rise 15


def test_no_southerly_without_a_swing():
    assert observed.southerly([s_(-20), s_(-10), s_(0)], AT) is None  # southerly all along
    assert observed.southerly([R(-20, wind_dir=150.0, wind=20.0, gust=30.0, temp=31.0), s_(-10)], AT) is None  # SSE to S
    assert observed.southerly([nw(-10), nw(0)], AT) is None


def test_the_change_is_measured_against_the_extremes_after_the_swing():
    # the temperature dips and recovers, the gust peaks and eases: still a change
    assert observed.southerly([nw(-30), s_(-20, temp=28.0, gust=30.0), s_(-10, temp=30.5, gust=30.0)], AT) is not None
    got = observed.southerly([nw(-30), s_(-20, temp=31.0, gust=50.0), s_(-10, temp=31.0, gust=30.0)], AT)
    assert got is not None and got.gust == 50


def test_the_southerly_sector_runs_from_se_to_sw_inclusive():
    assert observed.southerly([nw(-20), s_(-10, wind_dir=135.0)], AT) is not None
    assert observed.southerly([nw(-20), s_(-10, wind_dir=225.0)], AT) is not None
    assert observed.southerly([nw(-20), s_(-10, wind_dir=134.0)], AT) is None
    assert observed.southerly([nw(-20), s_(-10, wind_dir=230.0)], AT) is None


def test_a_small_turn_into_the_sector_is_not_a_change():
    # ESE (outside the sector) to SSE: into it, but by only 30 degrees
    assert observed.southerly([R(-20, wind_dir=120.0, wind=20.0, gust=30.0, temp=31.0), s_(-10, wind_dir=150.0)], AT) is None
    assert observed.southerly([R(-20, wind_dir=90.0, wind=20.0, gust=30.0, temp=31.0), s_(-10, wind_dir=150.0)], AT) is not None


def test_a_swing_within_the_sector_is_not_a_change():
    # SE to SW is 90 degrees, but the wind was southerly all along
    log = [R(-20, wind_dir=135.0, wind=20.0, gust=30.0, temp=31.0), s_(-10, wind_dir=225.0)]
    assert observed.southerly(log, AT) is None


def test_no_southerly_from_a_calm_or_an_old_change():
    assert observed.southerly([nw(-20), s_(-10, wind=5.0)], AT) is None  # too light to count
    assert observed.southerly([nw(-80), s_(-70), s_(0)], AT) is None  # swung over an hour ago
    assert observed.southerly([nw(-60), s_(-20)], AT) is None  # a 40-minute gap: no baseline
    assert observed.southerly([nw(-20), s_(-10)], AT + timedelta(minutes=40)) is None  # stale
    assert observed.southerly([nw(-20), s_(-10), nw(0)], AT) is None  # it swung back


def test_a_southerly_at_home_is_here():
    o = observed.summarise(logs([nw(-20, rain=0.0), s_(-10, rain=0.0)]), AT)
    assert o.southerly_here is not None and o.southerly_coast is None


def test_a_southerly_at_the_coast_but_not_home_is_arriving():
    coast = {"94767": [nw(-20, "94767"), s_(-10, "94767")], "94768": [nw(-30, "94768"), nw(-20, "94768")]}
    o = observed.summarise(logs([nw(-10, rain=0.0)], leading=coast), AT)
    assert o.southerly_here is None and o.southerly_coast.station == "94767"


def test_the_latest_coastal_change_is_the_one_reported():
    coast = {"95756": [nw(-40, "95756"), s_(-30, "95756")], "94767": [nw(-20, "94767"), s_(-10, "94767")]}
    o = observed.summarise(logs([nw(0)], leading=coast), AT)
    assert o.southerly_coast.station == "94767"


def test_once_it_is_home_the_coast_is_not_news():
    coast = {"94767": [nw(-30, "94767"), s_(-20, "94767")]}
    o = observed.summarise(logs([nw(-20), s_(-10)], leading=coast), AT)
    assert o.southerly_here is not None and o.southerly_coast is None


# ------------------------------------------------------------------ facts precedence


def I(**kw):
    base = dict(hour=14, month=11, code=1)
    base.update(kw)
    return Inputs(**base)


class Fixed:
    def normal_max(self, month, hour=None):
        return 26

    def normal_uv(self, month, hour=None):
        return 11


def keys(i):
    return {f.key: f for f in score(i, Fixed())}


FORECAST_SOUTHERLY = dict(dir_now=315, dir_next=180, wind=38, gust=85)


def test_a_forecast_southerly_keeps_its_score_without_observations():
    assert keys(I(**FORECAST_SOUTHERLY))["wind-change"].score == 88


def test_an_observed_southerly_at_the_coast_is_a_strong_arriving_fact():
    fs = score(I(southerly_seen="coast", southerly_station="Kurnell", southerly_gust=61.4), Fixed())
    assert fs[0].key == "southerly-arriving" and fs[0].score == 80
    assert (fs[0].label, fs[0].value, fs[0].sub, fs[0].ink) == (
        "Southerly change arriving", "at Kurnell", "", ORANGE)
    assert facts.refresh_minutes(facts.Coming(14, None, fs)) == 15


def test_an_observed_southerly_supersedes_the_forecast_one():
    k = keys(I(southerly_seen="coast", southerly_station="Kurnell", **FORECAST_SOUTHERLY))
    assert "southerly-arriving" in k and "wind-change" not in k
    assert k["wind"].label == "Wind S"  # the strong wind itself is still news
    k = keys(I(southerly_seen="here", southerly_station="Horsley Park", southerly_gust=58.6, **FORECAST_SOUTHERLY))
    assert (k["southerly-here"].score, k["southerly-here"].value, k["southerly-here"].sub) == (50, "arrived", "gust 59 km/h")
    assert "wind-change" not in k


def test_an_observed_southerly_leaves_other_wind_changes_alone():
    k = keys(I(southerly_seen="coast", southerly_station="Kurnell", dir_now=0, dir_next=90, wind=20, gust=30))
    assert k["wind-change"].label == "Wind change"


def test_observed_rain_replaces_the_modelled_figures():
    s = preset("worstPhoto")
    now = datetime(2026, 9, 30, 17, 1, tzinfo=SYD)
    fc = forecast(s)
    modelled = facts.inputs_for(fc, now, now.date(), False)
    assert modelled.since9_mm == pytest.approx(248, abs=0.1) and modelled.last_hr_mm == 96
    obs = observed.Observed("Horsley Park", "BOM Horsley Park AWS", now, since9_mm=12.4, last_hr_mm=3.2)
    i = facts.inputs_for(fc, now, now.date(), False, obs)
    assert (i.since9_mm, i.last_hr_mm) == (12.4, 3.2)
    assert i.rain_mm == modelled.rain_mm  # the coming hour is still the forecast's


def test_observed_since_9am_with_an_unknown_last_hour_drops_the_sub_text():
    obs = observed.Observed("Horsley Park", "BOM Horsley Park AWS", AT, since9_mm=12.4, last_hr_mm=None)
    s = preset("worstPhoto")
    now = datetime(2026, 9, 30, 17, 1, tzinfo=SYD)
    i = facts.inputs_for(forecast(s), now, now.date(), False, obs)
    f = keys(i)["since9"]
    assert (f.value, f.sub) == ("12 mm", "")


def test_without_an_observed_since_9am_the_model_stands():
    obs = observed.Observed("Horsley Park", "BOM Horsley Park AWS", AT)  # e.g. before 9am
    s = preset("worstPhoto")
    now = datetime(2026, 9, 30, 17, 1, tzinfo=SYD)
    i = facts.inputs_for(forecast(s), now, now.date(), False, obs)
    assert i.since9_mm == pytest.approx(248, abs=0.1) and i.last_hr_mm == 96


def test_inputs_carry_the_observed_southerly():
    sth = observed.Southerly("Kurnell", AT, 315, 190, 6.0, 62.0)
    obs = observed.Observed("Horsley Park", "BOM Horsley Park AWS", AT, southerly_coast=sth)
    s = preset("worstPhoto")
    now = datetime(2026, 9, 30, 17, 1, tzinfo=SYD)
    i = facts.inputs_for(forecast(s), now, now.date(), False, obs)
    assert (i.southerly_seen, i.southerly_station, i.southerly_gust) == ("coast", "Kurnell", 62.0)
    here = observed.Observed("Horsley Park", "x", AT, southerly_here=sth, southerly_coast=sth)
    assert facts.inputs_for(forecast(s), now, now.date(), False, here).southerly_seen == "here"
