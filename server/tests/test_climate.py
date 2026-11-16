"""Climate normals: statistics on synthetic series with known answers, time
zone handling, the placeholder fallback and the cache lifecycle. No network:
recorded fixtures are from Sydney Observatory Hill (Open-Meteo, NASA POWER)
and ARPANSA's public Sydney UV site."""

import json
import os
import time
import urllib.error
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from almanac import climate
from almanac.climate import Obs
from almanac.config import Config

SYD = ZoneInfo("Australia/Sydney")
FIX = Path(__file__).parent / "fixtures"
OBS_HILL = dict(latitude=-33.86, longitude=151.21, timezone="Australia/Sydney")


def cfg(tmp_path, **kw):
    return Config(cache_dir=tmp_path, **{**OBS_HILL, **kw})


def local(y, mo, d, h, fold=0):
    """An aware UTC instant for a Sydney clock time."""
    return datetime(y, mo, d, h, tzinfo=SYD, fold=fold).astimezone(timezone.utc)


def series(start_utc, hours, **fields):
    """Hourly Obs from start_utc; each field is a value or a function of the
    hour index."""
    out = []
    for i in range(hours):
        kw = {k: (v(i) if callable(v) else v) for k, v in fields.items()}
        out.append(Obs(utc=start_utc + timedelta(hours=i), **kw))
    return out


# ------------------------------------------------------------------ statistics


def test_percentile_interpolates_linearly():
    xs = list(range(1, 11))  # 1..10
    assert climate.percentile(xs, 50) == 5.5
    assert climate.percentile(xs, 90) == pytest.approx(9.1)
    assert climate.percentile(xs, 10) == pytest.approx(1.9)
    assert climate.percentile([7.0], 90) == 7.0
    assert climate.percentile([], 50) is None


def test_temperature_cell_statistics():
    # 2021-01-10..19, every day 15:00 local reads 20, 21, ... 29.
    obs = []
    for d in range(10):
        obs.append(Obs(utc=local(2021, 1, 10 + d, 15), temp=20.0 + d))
    t = climate.compute(obs, SYD)["hourly"][0][15]["temp"]
    assert t["mean"] == 24.5
    assert t["p50"] == 24.5
    assert t["p10"] == pytest.approx(20.9)
    assert t["p90"] == pytest.approx(28.1)
    assert t["std"] == pytest.approx(2.87, abs=0.01)  # population std of 0..9


def test_wet_hour_threshold_is_point_one_mm():
    # Jan 3 pm on ten days: 0.0 x4, 0.09 x2 (ERA5 drizzle: dry), 0.1, 0.5, 1.0, 3.0
    amounts = [0.0, 0.0, 0.0, 0.0, 0.09, 0.09, 0.1, 0.5, 1.0, 3.0]
    obs = [Obs(utc=local(2021, 1, 1 + d, 15), rain=a) for d, a in enumerate(amounts)]
    r = climate.compute(obs, SYD)["hourly"][0][15]["rain"]
    assert r["p_wet"] == 0.4
    assert r["wet_mean"] == pytest.approx((0.1 + 0.5 + 1.0 + 3.0) / 4, abs=0.005)
    assert r["wet_p90"] == pytest.approx(2.4)  # p90 of [0.1, 0.5, 1.0, 3.0]


def test_wind_percentiles_and_sectors():
    dirs = [0, 10, 350, 45, 90, 180, 200, 225, 270, 315]  # N N N NE E S S SW W NW
    obs = [Obs(utc=local(2021, 2, 1 + d, 9), wind=float(5 * (d + 1)), gust=float(10 * (d + 1)), wind_dir=w) for d, w in enumerate(dirs)]
    w = climate.compute(obs, SYD)["hourly"][1][9]["wind"]
    assert w["p50"] == 27.5 and w["p90"] == pytest.approx(45.5)
    assert w["gust_p50"] == 55.0 and w["gust_p90"] == pytest.approx(91.0)
    assert w["sectors"]["N"] == 0.3
    assert w["sectors"]["S"] == 0.2
    assert sum(w["sectors"].values()) == pytest.approx(1.0)


@pytest.mark.parametrize(
    "deg,name",
    [(0, "N"), (22.4, "N"), (22.5, "NE"), (337.5, "N"), (337.4, "NW"), (180, "S"), (157.5, "S"), (157.4, "SE"), (270, "W"), (360, "N")],
)
def test_sector_edges(deg, name):
    assert climate.sector(deg) == name


def test_daily_max_and_min_by_local_date():
    # Three full local days in March: max 30/31/32, min 10/11/12.
    start = local(2021, 3, 10, 0)
    obs = series(start, 72, temp=lambda i: (10 + i // 24) + (20 if i % 24 == 14 else 0))
    d = climate.compute(obs, SYD)["daily"][2]
    assert d["days"] == 3
    assert d["max"]["mean"] == 31.0
    assert d["min"]["mean"] == 11.0


# ------------------------------------------------------------------ southerly changes


def wind_pair(d0, d1, speed1, gap_h=1, speed0=15.0):
    t = local(2021, 12, 5, 16)
    return [Obs(utc=t, wind=speed0, wind_dir=d0), Obs(utc=t + timedelta(hours=gap_h), wind=speed1, wind_dir=d1)]


def rate(obs):
    return climate.compute(obs, SYD)["hourly"][11][obs[-1].utc.astimezone(SYD).hour]["southerly"]


@pytest.mark.parametrize(
    "d0,d1,speed1,gap,expect",
    [
        (20, 190, 35, 1, True),  # N -> S at 35: the classic buster
        (45, 135, 20, 1, True),  # NE -> SE at exactly 20
        (315, 210, 25, 1, True),  # NW -> SW
        (270, 180, 25, 1, True),  # W -> S at 25
        (270, 180, 15, 1, False),  # W -> S at 15: too light to count
        (90, 180, 40, 1, False),  # E -> S: not from the north/west side
        (180, 190, 40, 1, False),  # already southerly
        (20, 270, 40, 1, False),  # N -> W: not to the south
        (20, 190, 35, 2, False),  # a gap: not within the hour
    ],
)
def test_southerly_change_detection(d0, d1, speed1, gap, expect):
    obs = wind_pair(d0, d1, speed1, gap)
    if gap == 1:  # is_southerly_change leaves the spacing to compute()
        assert climate.is_southerly_change(*obs) == expect
    assert (rate(obs) > 0) == expect


def test_southerly_rate_and_monthly_count():
    # December 2021: on 3 of the 31 days a change arrives at 17:00.
    obs = []
    for d in range(1, 32):
        t = local(2021, 12, d, 16)
        hit = d in (3, 10, 20)
        obs += [Obs(utc=t, wind=15.0, wind_dir=20), Obs(utc=t + timedelta(hours=1), wind=30.0, wind_dir=190 if hit else 30)]
    table = climate.compute(obs, SYD)
    assert table["hourly"][11][17]["southerly"] == pytest.approx(3 / 31, abs=1e-4)
    assert table["hourly"][11][16]["southerly"] == 0
    assert table["southerly_per_month"][11] == 3.0  # one December of data


# ------------------------------------------------------------------ time zones


def test_cells_group_by_clock_hour_across_daylight_saving():
    # Every UTC hour from 3 to 7 April 2020 (DST ends 5 April, 03:00 AEDT -> 02:00 AEST).
    start = local(2020, 4, 3, 0)
    obs = series(start, 24 * 4, temp=1.0)
    april = climate.compute(obs, SYD)["hourly"][3]
    assert april[2]["n"] == 5  # 02:00-02:59 happens twice on 5 April
    assert april[3]["n"] == 4
    # DST starts 4 October 2020: 02:00-02:59 never happens that day.
    start = local(2020, 10, 3, 0)
    october = climate.compute(series(start, 24 * 3, temp=1.0), SYD)["hourly"][9]
    assert october[2]["n"] == 2
    assert october[3]["n"] == 3


def test_open_meteo_labels_are_one_fixed_offset():
    """Open-Meteo labelled this July pull at +11 (the offset when it was
    recorded, in November): its 00:00 label is 23:00 AEST the day before."""
    payload = json.loads((FIX / "openmeteo-archive-obshill-jul.json").read_text())
    assert payload["utc_offset_seconds"] == 39600
    first = next(climate.observations(payload))
    assert first.utc.astimezone(SYD) == datetime(2020, 6, 30, 23, tzinfo=SYD)
    assert first.temp == payload["hourly"]["temperature_2m"][0]
    # The January pull's labels are already clock time.
    jan = next(climate.observations(json.loads((FIX / "openmeteo-archive-obshill-jan.json").read_text())))
    assert jan.utc.astimezone(SYD) == datetime(2020, 1, 1, 0, tzinfo=SYD)


def test_power_lst_converted_to_clock_time():
    """POWER's LST at 151.21 E is UTC+10 all year. In January (daylight
    saving) its 12:00 sample belongs to the 13:00 clock hour."""
    payload = json.loads((FIX / "power-uv-obshill-20200105.json").read_text())
    hours = climate.uv_hours(climate.parse_power(payload, 151.21), SYD, min_minutes=1)
    lst = payload["properties"]["parameter"]["ALLSKY_SFC_UV_INDEX"]
    assert hours[(date(2020, 1, 5), 13)] == lst["2020010512"]
    assert hours[(date(2020, 1, 5), 6)] == lst["2020010505"]
    # In July (standard time) LST 12:00 is clock 12:00.
    july = {"properties": {"parameter": {"ALLSKY_SFC_UV_INDEX": {"2020071512": 3.3, "2020071513": -999.0}}}, "header": {"fill_value": -999.0}}
    got = climate.uv_hours(climate.parse_power(july, 151.21), SYD, min_minutes=1)
    assert got == {(date(2020, 7, 15), 12): 3.3}  # the fill value is dropped


def test_arpansa_minutes_average_into_clock_hours():
    """The CSV is AEST all year: 11:00-13:59 AEST on 15 January is 12:00-14:59
    in Sydney clock time."""
    lines = (FIX / "arpansa-uv-sydney-20190115.csv").read_text(encoding="utf-8-sig").splitlines()
    rows = [l.split(",") for l in lines[1:]]
    noon_aest = [float(r[3]) for r in rows if r[0].startswith("2019-01-15 12:")]
    hours = climate.uv_hours(climate.parse_arpansa_csv(lines), SYD)
    assert sorted(h for _, h in hours) == [12, 13, 14]
    assert hours[(date(2019, 1, 15), 13)] == pytest.approx(sum(noon_aest) / 60)


def test_arpansa_short_hours_and_junk_are_dropped():
    lines = ["Date-Time,Lat,Lon,UV_Index"]
    lines += [f"2019-07-01 12:{m:02d}:00,-34.04,151.10,3.0" for m in range(44)]  # 44 minutes: too few
    lines += [f"2019-07-01 13:{m:02d}:00,-34.04,151.10,{'' if m == 0 else -0.01 if m == 1 else 2.0}" for m in range(60)]
    hours = climate.uv_hours(climate.parse_arpansa_csv(lines), SYD)
    assert list(hours) == [(date(2019, 7, 1), 13)]
    assert hours[(date(2019, 7, 1), 13)] == pytest.approx(2.0 * 58 / 59)  # blank skipped, -0.01 -> 0


def test_uv_table_mean_and_p90():
    means = {(date(2019, 1, d), 13): float(d) for d in range(1, 11)}
    t = climate.uv_table(means, {"source": "test"})
    assert t["hourly"][0][13] == {"n": 10, "mean": 5.5, "p90": 9.1}
    assert t["hourly"][0][12]["mean"] is None


# ------------------------------------------------------------------ placeholder and cache


def year_payload(year, offset_s=39600):
    """A synthetic full-year archive response labelled at a fixed offset."""
    start = datetime(year, 1, 1)
    n = (datetime(year + 1, 1, 1) - start).days * 24
    times = [(start + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M") for i in range(n)]
    return {
        "latitude": -33.9, "longitude": 151.2, "utc_offset_seconds": offset_s,
        "hourly": {
            "time": times,
            "temperature_2m": [20.0 + (i % 24) / 4 for i in range(n)],
            "wind_speed_10m": [12.0] * n,
            "wind_direction_10m": [45.0] * n,
            "wind_gusts_10m": [25.0] * n,
            "precipitation": [0.2 if i % 24 == 15 else 0.0 for i in range(n)],  # wet at 15:00 labels
        },
    }


class FakeArchive:
    def __init__(self, fail=()):
        self.urls = []
        self.fail = list(fail)  # exceptions to raise, in order, before succeeding

    def __call__(self, url):
        self.urls.append(url)
        if self.fail:
            raise self.fail.pop(0)
        year = int(url.split("start_date=")[1][:4])
        return year_payload(year)


def test_placeholder_when_cache_missing(tmp_path):
    c = cfg(tmp_path)
    n = climate.load(c)
    assert n.placeholder
    assert n.normal_max(1) == 26.0  # the placeholder's January
    assert n.daily_min(7) == 8.8
    assert 18 < n.normal_max(1, 15) <= 26.0
    assert n.uv_source.startswith("ARPANSA")  # UV is committed: never a placeholder


def test_placeholder_when_cache_is_for_another_place_or_junk(tmp_path):
    c = cfg(tmp_path)
    climate.build(c, fetch=FakeArchive(), sleep=lambda s: None, years=range(2019, 2020))
    assert not climate.load(c).placeholder
    assert climate.load(cfg(tmp_path, latitude=-35.28)).placeholder
    (tmp_path / climate.CACHE_NAME).write_text("{not json")
    assert climate.load(c).placeholder


def test_build_writes_cache_and_live_normals_follow_it(tmp_path):
    c = cfg(tmp_path)
    live = climate.LiveNormals(c)
    assert live.placeholder
    fake = FakeArchive()
    t = climate.build(c, fetch=fake, sleep=lambda s: None, years=range(2019, 2021))
    assert len(fake.urls) == 2
    q = fake.urls[0]
    for part in ("models=era5_seamless", "timezone=Australia%2FSydney", "wind_speed_unit=kmh", "start_date=2019-01-01", "end_date=2019-12-31"):
        assert part in q
    assert t["requests"] == 2 and t["grid"] == {"lat": -33.9, "lon": 151.2}
    assert not (tmp_path / climate.CHUNK_DIR).exists()  # chunks cleaned up
    assert not live.placeholder
    # Labels at +11 all year: in July, the 15:00 label is 14:00 AEST.
    assert live.temp(7, 14).mean == pytest.approx(20.0 + 15 / 4)
    assert live.rain(1, 15).p_wet == 1.0 and live.rain(1, 14).p_wet == 0
    assert live.rain(7, 14).p_wet == 1.0 and live.rain(7, 15).p_wet == 0
    assert live.wind(3, 12).sectors["NE"] == 1.0


def test_build_retries_and_resumes_from_saved_years(tmp_path):
    c = cfg(tmp_path)
    slept = []
    # Year 1 fails once (transient) then succeeds; year 2 fails for good.
    fake = FakeArchive(fail=[OSError("reset")])
    original = fake.__call__

    bad_requests = []

    def flaky(url):
        if "2020-01-01" in url:
            bad_requests.append(url)
            raise urllib.error.HTTPError(url, 400, "bad", {}, None)
        return original(url)

    with pytest.raises(urllib.error.HTTPError):
        climate.build(c, fetch=flaky, sleep=slept.append, years=range(2019, 2021))
    assert slept[0] == 10  # backed off once, then moved on
    assert len(bad_requests) == 1  # a 400 is our fault: not retried
    assert len([u for u in fake.urls if "2019" in u]) == 2
    assert climate.load(c).placeholder
    # The rerun reuses 2019 and fetches only 2020.
    again = FakeArchive()
    climate.build(c, fetch=again, sleep=lambda s: None, years=range(2019, 2021))
    assert [u.split("start_date=")[1][:4] for u in again.urls] == ["2020"]


def test_cache_refreshes_when_a_year_old(tmp_path):
    c = cfg(tmp_path)
    t0 = time.time()
    assert climate.build_if_needed(c, fetch=FakeArchive(), sleep=lambda s: None, years=range(2019, 2020), now=t0)
    fake = FakeArchive()
    kw = dict(fetch=fake, sleep=lambda s: None, years=range(2019, 2020))
    assert not climate.build_if_needed(c, now=t0 + 300 * 86400, **kw)
    assert fake.urls == []
    assert climate.build_if_needed(c, now=t0 + 366 * 86400, **kw)
    assert len(fake.urls) == 1
    assert json.loads((tmp_path / climate.CACHE_NAME).read_text())["built_at"] == t0 + 366 * 86400


def test_short_or_empty_response_is_rejected(tmp_path):
    p = year_payload(2019)
    p["hourly"]["precipitation"] = p["hourly"]["precipitation"][:-5]
    with pytest.raises(ValueError):
        climate.check_payload(p)
    p = year_payload(2019)
    p["hourly"]["temperature_2m"] = [None] * len(p["hourly"]["time"])
    with pytest.raises(ValueError):
        climate.check_payload(p)


# ------------------------------------------------------------------ the committed UV table and the interface


def test_committed_uv_table_is_sane():
    t = climate.load_uv_table()
    assert t is not None and t["credit"] == climate.UV_CREDIT
    n = climate.Normals(climate.PLACEHOLDER, t)
    # A typical day's peak: summer about 11-13, winter about 2-4.
    assert 11 <= n.normal_uv(1) <= 13 and 11 <= n.normal_uv(12) <= 13
    assert 2 <= n.normal_uv(6) <= 4 and 2 <= n.normal_uv(7) <= 4
    # Hourly means include cloudy days, so they sit lower; p90 near clear sky.
    assert 7.5 <= n.normal_uv(1, 13) <= 11 and 10.5 <= n.uv(1, 13).p90 <= 13
    assert 1.5 <= n.normal_uv(7, 12) <= 3.5
    assert n.normal_uv(1, 2) < 0.1  # night (the meter reads ~0.02 in the dark)
    # Summer peaks at 13:00 clock time (daylight saving), winter at 12:00.
    assert max(range(24), key=lambda h: n.uv(1, h).mean) == 13
    assert max(range(24), key=lambda h: n.uv(7, h).mean) == 12


def test_uv_daily_peaks():
    peaks = {}
    samples = [(local(2019, 1, 15, 12) + timedelta(minutes=m), float(m % 7)) for m in range(120)]
    climate.uv_hours(samples, SYD, peaks=peaks)
    assert peaks == {date(2019, 1, 15): [6.0, 120]}
    t = climate.uv_table({}, {}, {date(2019, 1, d): float(d) for d in range(1, 11)})
    assert t["daily_peak"][0] == {"days": 10, "p50": 5.5, "p90": 9.1}
    assert climate.Normals(climate.PLACEHOLDER, t).uv_peak(1) == 5.5


def test_interface_checks_ranges_and_helpers():
    n = climate.Normals(climate.PLACEHOLDER)
    with pytest.raises(ValueError):
        n.temp(13, 0)
    with pytest.raises(ValueError):
        n.uv(1, 24)
    t = n.temp(1, 15)
    assert n.temp_z(1, 15, t.mean + 2 * t.std) == pytest.approx(2.0)
    assert n.temp_percentile(1, 15, t.mean) == pytest.approx(50.0)
    assert n.temp_percentile(1, 15, t.mean + 10 * t.std) > 99.9
    assert n.normal_max(1) == n.daily_max(1)
    assert n.normal_max(1, 6) == n.temp(1, 6).mean != n.daily_max(1)
    assert n.normal_uv(1) == n.uv_peak(1)
    assert sum(n.southerly_rate(12, h) for h in range(24)) * 30 == pytest.approx(n.southerly_per_month(12), rel=0.01)
