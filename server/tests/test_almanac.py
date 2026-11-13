import io
import json
import math
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from PIL import Image, ImageDraw

from almanac import astro, conditions, photos, schedule, weather
from almanac.app import Almanac
from almanac.config import Config
from almanac.palette import INK_RGB, to_preview
from almanac.render import H, W, dither_photo, render

SYD = ZoneInfo("Australia/Sydney")


def cfg(tmp_path, **kw):
    c = Config(cache_dir=tmp_path, **kw)
    return c


def payload(start=date(2026, 11, 10), days=3, code=61):
    """A synthetic Open-Meteo response."""
    hourly_t, temps, probs, codes = [], [], [], []
    for d in range(days):
        for h in range(24):
            ts = datetime.combine(start + timedelta(days=d), datetime.min.time()) + timedelta(hours=h)
            hourly_t.append(ts.strftime("%Y-%m-%dT%H:%M"))
            temps.append(14 + 8 * math.sin((h - 9) / 24 * 2 * math.pi))
            probs.append(min(100, max(0, (h - 12) * 12)))
            codes.append(code)
    daily_days = [(start + timedelta(days=d)).isoformat() for d in range(days)]
    return {
        "current": {"temperature_2m": 17.2, "weather_code": code},
        "hourly": {"time": hourly_t, "temperature_2m": temps, "precipitation_probability": probs, "weather_code": codes},
        "daily": {
            "time": daily_days,
            "weather_code": [code] * days,
            "temperature_2m_max": [22.4] * days,
            "temperature_2m_min": [9.6] * days,
            "sunrise": [f"{d}T05:48" for d in daily_days],
            "sunset": [f"{d}T19:31" for d in daily_days],
            "uv_index_max": [8.7] * days,
            "precipitation_probability_max": [80] * days,
            "precipitation_sum": [6.3] * days,
        },
    }


def painting(w=843, h=650):
    img = Image.new("RGB", (w, h))
    px = img.load()
    for y in range(h):
        for x in range(w):
            px[x, y] = (int(255 * x / w), int(255 * y / h), 160)
    return img


# ------------------------------------------------------------------ schedule


@pytest.mark.parametrize(
    "now, expect",
    [
        ("2026-11-10T05:30", "2026-11-10T06:01"),
        ("2026-11-10T06:00", "2026-11-10T06:01"),
        ("2026-11-10T06:01", "2026-11-10T07:01"),
        ("2026-11-10T14:20", "2026-11-10T15:01"),
        ("2026-11-10T22:01", "2026-11-11T06:01"),  # last refresh of the night done
        ("2026-11-10T21:59", "2026-11-10T22:01"),
        ("2026-11-10T23:45", "2026-11-11T06:01"),
    ],
)
def test_next_wake(now, expect):
    got = schedule.next_wake(datetime.fromisoformat(now), 6, 22)
    assert got == datetime.fromisoformat(expect)


def test_sleep_minutes_is_clamped():
    assert schedule.sleep_minutes(datetime(2026, 11, 10, 6, 0, 50), 6, 22) == schedule.MIN_SLEEP
    overnight = schedule.sleep_minutes(datetime(2026, 11, 10, 22, 1, 30), 6, 22)
    assert 470 <= overnight <= 481


def test_poster_day_switches_to_tomorrow_in_the_evening():
    assert schedule.poster_day(datetime(2026, 11, 10, 17, 59), 18) == (date(2026, 11, 10), False)
    assert schedule.poster_day(datetime(2026, 11, 10, 18, 0), 18) == (date(2026, 11, 11), True)
    assert schedule.poster_day(datetime(2026, 11, 10, 22, 1), 18) == (date(2026, 11, 11), True)


# ------------------------------------------------------------------ moon


def test_moon_phase_at_known_moments():
    # Full moon 2024-01-25 17:54 UTC, new moon 2024-02-09 22:59 UTC. A mean
    # synodic month drifts up to ~14 h from the true phase; 0.025 is ~18 h.
    full = astro.moon_phase(datetime(2024, 1, 25, 17, 54, tzinfo=timezone.utc))
    new = astro.moon_phase(datetime(2024, 2, 9, 22, 59, tzinfo=timezone.utc))
    assert abs(full - 0.5) < 0.025
    assert min(new, 1 - new) < 0.025
    assert astro.phase_name(full) == "Full moon"
    assert astro.illumination(full) > 0.99


def test_lit_span_mirrors_south_of_the_equator():
    # First quarter: right half lit in the north, left half in the south.
    assert astro.lit_span(0.25, 10, southern=False) == pytest.approx((0, 10), abs=1e-9)
    assert astro.lit_span(0.25, 10, southern=True) == pytest.approx((-10, 0), abs=1e-9)
    assert astro.lit_span(0.0, 10, southern=True) is None
    assert astro.lit_span(0.5, 10, southern=False) == pytest.approx((-10, 10))


# ------------------------------------------------------------------ weather


def test_parse_groups_hours_by_day():
    fc = weather.parse(payload(), 0)
    day = fc.day(date(2026, 11, 10))
    assert len(day.hours) == 24 and day.hours[0].hour == 0
    assert day.label == "Light rain" and day.icon == "rain"
    assert day.sunrise == datetime(2026, 11, 10, 5, 48)


def test_parse_tolerates_nulls():
    p = payload()
    p["hourly"]["precipitation_probability"][3] = None
    p["daily"]["uv_index_max"][0] = None
    fc = weather.parse(p, 0)
    assert fc.day(date(2026, 11, 10)).uv == 0.0


def test_weather_cache_survives_a_network_failure(tmp_path):
    c = cfg(tmp_path)
    first = weather.load(c, now=1000, fetch=lambda url: payload())
    assert first.fetched_at == 1000

    def boom(url):
        raise OSError("network down")

    stale = weather.load(c, now=1000 + weather.TTL_SECONDS + 1, fetch=boom)
    assert stale is not None and stale.fetched_at == 1000


def test_weather_without_any_cache_returns_none(tmp_path):
    def boom(url):
        raise OSError("network down")

    assert weather.load(cfg(tmp_path), now=0, fetch=boom) is None


# ------------------------------------------------------------------ photos


def entry(i):
    return {"id": str(i), "title": f"Peak {i}", "credit": "Photo: A · CC BY-SA 4.0", "thumb": f"https://x/{i}.jpg"}


def jpeg(img):
    buf = io.BytesIO()
    img.save(buf, "JPEG")
    return buf.getvalue()


def vivid(w=960, h=640):
    """Blue sky, white snow line, green forest: what the screen should accept."""
    img = Image.new("RGB", (w, h), (60, 120, 220))
    d = ImageDraw.Draw(img)
    d.rectangle([0, h * 0.45, w, h * 0.55], fill=(245, 245, 250))
    d.rectangle([0, h * 0.55, w, h], fill=(30, 120, 50))
    return img


def grey(w=960, h=640):
    return Image.new("RGB", (w, h), (128, 128, 128))


def test_order_is_stable_per_day_and_shuffles():
    pool = [entry(i) for i in range(50)]
    first = photos.order(pool, date(2026, 11, 10))[0]
    assert first == photos.order(pool, date(2026, 11, 10))[0]
    others = {photos.order(pool, date(2026, 11, 10), s)[0]["id"] for s in range(1, 8)}
    assert others - {first["id"]}, "shuffling never changes the photo"
    assert photos.order([], date(2026, 11, 10)) == []


def test_usable_rejects_panoramas_small_and_non_jpeg():
    ok = {"mime": "image/jpeg", "width": 6000, "height": 4000}
    assert photos.usable(ok)
    assert not photos.usable(dict(ok, width=12000))  # 3:1 panorama
    assert not photos.usable(dict(ok, mime="image/png"))
    assert not photos.usable(dict(ok, width=1000, height=800))


def test_nice_title():
    assert photos.nice_title("File:Moraine_Lake_17092005.jpg") == "Moraine Lake"  # catalogue number dropped
    assert photos.nice_title("File:x.jpg", "<i>Matterhorn</i> at dawn") == "Matterhorn at dawn"
    assert photos.nice_title("File:IMG_1234.JPG") == "Untitled"


def test_colour_screen():
    assert photos.colourful_enough(vivid())
    assert not photos.colourful_enough(grey())
    assert not photos.colourful_enough(Image.new("RGB", (100, 100), (5, 10, 40)))  # night


def test_photo_for_skips_grey_candidates_and_remembers(tmp_path):
    c = cfg(tmp_path)
    pool = [entry(i) for i in range(6)]
    ordered = photos.order(pool, date(2026, 11, 10))
    dull = ordered[0]["id"]
    fetched = []

    def get(url, ua):
        fetched.append(url)
        return jpeg(grey() if url.endswith(f"/{dull}.jpg") else vivid())

    got = photos.photo_for(c, date(2026, 11, 10), fetcher=lambda: pool, get=get)
    assert got.id == ordered[1]["id"]
    assert dull in json.loads((tmp_path / "photo-rejects.json").read_text())
    fetched.clear()
    again = photos.photo_for(c, date(2026, 11, 10), fetcher=lambda: pool, get=get)
    assert again.id == got.id and fetched == [], "a cached, screened photo is not downloaded twice"


def test_photo_for_falls_back_to_last_cached_when_offline(tmp_path):
    c = cfg(tmp_path)
    pool = [entry(i) for i in range(6)]
    shown = photos.photo_for(c, date(2026, 11, 10), fetcher=lambda: pool, get=lambda u, ua: jpeg(vivid()))

    def offline(url, ua):
        raise OSError("down")

    nxt = photos.photo_for(c, date(2026, 11, 11), fetcher=lambda: pool, get=offline)
    assert nxt is not None and nxt.id == shown.id


def test_fetch_pool_parses_commons_responses():
    calls = []

    def api(params):
        calls.append(params)
        if params.get("list") == "categorymembers":
            return {"query": {"categorymembers": [{"title": "File:Lake_Louise.jpg"}, {"title": "File:Pano.jpg"}]}}
        return {
            "query": {
                "pages": [
                    {
                        "pageid": 11,
                        "title": "File:Lake_Louise.jpg",
                        "imageinfo": [
                            {
                                "mime": "image/jpeg",
                                "width": 6000,
                                "height": 4000,
                                "thumburl": "https://upload/960px-Lake_Louise.jpg",
                                "extmetadata": {
                                    "Artist": {"value": '<a href="//commons">Jane Doe</a>'},
                                    "LicenseShortName": {"value": "CC BY-SA 4.0"},
                                },
                            }
                        ],
                    },
                    {"pageid": 12, "title": "File:Pano.jpg", "imageinfo": [{"mime": "image/jpeg", "width": 9000, "height": 2000}]},
                ]
            }
        }

    pool = photos.fetch_pool("ua", api=api, pause=0)
    assert pool == [{"id": "11", "title": "Lake Louise", "credit": "Photo: Jane Doe · CC BY-SA 4.0", "thumb": "https://upload/960px-Lake_Louise.jpg"}]
    info_calls = [p for p in calls if p.get("prop") == "imageinfo"]
    assert len(info_calls) == 1 and info_calls[0]["titles"] == "File:Lake_Louise.jpg|File:Pano.jpg"


# ------------------------------------------------------------------ render


def test_dither_uses_only_panel_inks():
    out, box = dither_photo(painting(), (500, 480))
    assert out.mode == "P" and out.size == (box[2] - box[0], box[3] - box[1])
    assert max(i for _, i in out.getcolors()) <= 6


def test_dither_letterboxes_a_very_wide_painting():
    _, box = dither_photo(painting(1600, 600), (500, 480))
    assert box[0] > 0 and box[1] > 0


@pytest.mark.parametrize("hour, tomorrow", [(9, False), (19, True)])
def test_render_produces_ink_indices_only(tmp_path, hour, tomorrow):
    c = cfg(tmp_path)
    now = datetime(2026, 11, 10, hour, 1, tzinfo=SYD)
    day, is_t = schedule.poster_day(now, c.tomorrow_from_hour)
    assert is_t is tomorrow
    fc = weather.parse(payload(), now.timestamp() - 600)
    p = photos.Photo("1", "Moraine Lake", "Photo: Jane Doe · CC BY-SA 4.0", painting())
    img = render(now=now, poster_day=day, is_tomorrow=is_t, forecast=fc, photo=p, cfg=c)
    assert img.size == (W, H)
    colours = {i for _, i in img.getcolors()}
    assert colours <= set(range(7))
    assert len(colours) >= 5  # the poster actually uses colour
    assert to_preview(img).getpixel((W - 1, H - 1)) == INK_RGB[1]


def test_render_without_data_still_draws(tmp_path):
    c = cfg(tmp_path)
    now = datetime(2026, 11, 10, 9, 1, tzinfo=SYD)
    img = render(now=now, poster_day=now.date(), is_tomorrow=False, forecast=None, photo=None, cfg=c)
    assert img.size == (W, H)


# ------------------------------------------------------------------ app


def test_app_png_is_4bit_indexed_and_hash_is_stable_within_the_hour(tmp_path):
    c = cfg(tmp_path)
    app = Almanac(
        c,
        weather_loader=lambda: weather.parse(payload(), datetime(2026, 11, 10, 9, tzinfo=SYD).timestamp()),
        photo_loader=lambda day, s: photos.Photo(str(s), f"P{s}", "credit", painting()),
        conditions_loader=lambda day, tomorrow, from_hour: None,
        bom_loader=lambda: {},
    )
    t1 = datetime(2026, 11, 10, 9, 1, tzinfo=SYD)
    info = app.frame_info(t1)
    png = app.poster(t1)["png"]
    img = Image.open(io.BytesIO(png))
    assert img.mode == "P" and img.size == (W, H)
    raw = png[: 64]
    assert raw[24] == 4, "PNG bit depth must be 4 for the frame's decoder budget"
    assert info["size"] == len(png)
    assert app.frame_info(t1.replace(minute=40))["hash"] == info["hash"]
    assert app.frame_info(t1.replace(hour=10))["hash"] != info["hash"]
    app.shuffle(t1)
    assert app.frame_info(t1)["hash"] != info["hash"]


# ------------------------------------------------------------------ usb mode


class FakeRun:
    def __init__(self, ok=True):
        self.calls, self.ok = [], ok

    def __call__(self, cmd, **kw):
        import subprocess

        self.calls.append(cmd)
        digest = cmd[-1].split("'")[3]
        out = f"shown {digest}\n" if self.ok else "Traceback: OSError\n"
        return subprocess.CompletedProcess(cmd, 0 if self.ok else 1, out, "")


def usb_app(tmp_path):
    return Almanac(
        cfg(tmp_path),
        weather_loader=lambda: weather.parse(payload(), datetime(2026, 11, 10, 9, tzinfo=SYD).timestamp()),
        photo_loader=lambda day, s: photos.Photo(str(s), f"P{s}", "credit", vivid()),
        conditions_loader=lambda day, tomorrow, from_hour: None,
        bom_loader=lambda: {},
    )


def test_usb_push_only_when_the_poster_changes(tmp_path):
    from almanac.usbpush import UsbPusher

    app = usb_app(tmp_path)
    run = FakeRun()
    pusher = UsbPusher(app, "/dev/ttyACM0", run=run)
    t = datetime(2026, 11, 10, 9, 1, tzinfo=SYD)
    assert pusher.push_once(t) == 61
    assert len(run.calls) == 1
    cmd = run.calls[0]
    assert cmd[:4] == ["mpremote", "connect", "/dev/ttyACM0", "cp"] and cmd[5:8] == [":frame.png", "+", "exec"]
    assert (tmp_path / "usb-frame.png").read_bytes() == app.poster(t)["png"]
    pusher.push_once(t.replace(minute=30))
    assert len(run.calls) == 1, "same hour, same poster: no redraw"
    # The last pushed hash survives a restart.
    UsbPusher(app, "/dev/ttyACM0", run=run).push_once(t.replace(minute=45))
    assert len(run.calls) == 1
    pusher.push_once(t.replace(hour=10))
    assert len(run.calls) == 2


def test_usb_push_failure_is_retried(tmp_path):
    from almanac.usbpush import UsbPusher

    app = usb_app(tmp_path)
    t = datetime(2026, 11, 10, 9, 1, tzinfo=SYD)
    with pytest.raises(RuntimeError):
        UsbPusher(app, "/dev/x", run=FakeRun(ok=False)).push_once(t)
    run = FakeRun()
    UsbPusher(app, "/dev/x", run=run).push_once(t)
    assert len(run.calls) == 1, "a failed push must not be recorded as shown"


# ------------------------------------------------------------------ conditions

RFS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<FireDangerMap>
  <District><Name>Far North Coast</Name><DangerLevelToday>EXTREME</DangerLevelToday>
    <DangerLevelTomorrow>EXTREME</DangerLevelTomorrow><FireBanToday>Yes</FireBanToday><FireBanTomorrow>Yes</FireBanTomorrow></District>
  <District><Name>Greater Sydney Region</Name><Councils>The Hills; Blacktown</Councils>
    <DangerLevelToday>HIGH</DangerLevelToday><DangerLevelTomorrow>NO RATING</DangerLevelTomorrow>
    <FireBanToday>No</FireBanToday><FireBanTomorrow>Yes</FireBanTomorrow></District>
</FireDangerMap>"""


def aq_payload(day=date(2026, 11, 10), peak_hour=15, peak=160):
    times = [f"{day.isoformat()}T{h:02d}:00" for h in range(24)]
    vals = [40] * 24
    vals[peak_hour] = peak
    vals[2] = 400  # overnight spikes don't count
    return {"hourly": {"time": times, "us_aqi": vals}}


def test_parse_rfs_picks_the_district_and_day():
    assert conditions.parse_rfs(RFS_XML, "Greater Sydney Region", tomorrow=False) == ("HIGH", False)
    assert conditions.parse_rfs(RFS_XML, "greater sydney region ", tomorrow=True) == (None, True)
    assert conditions.parse_rfs(RFS_XML, "Nowhere", tomorrow=False) == (None, False)


def test_worst_daytime_aqi_ignores_night_and_other_days():
    assert conditions.worst_daytime_aqi(aq_payload(), date(2026, 11, 10)) == 160
    assert conditions.worst_daytime_aqi(aq_payload(), date(2026, 11, 11)) is None
    p = aq_payload(peak_hour=23, peak=300)
    assert conditions.worst_daytime_aqi(p, date(2026, 11, 10)) == 40


def test_load_conditions_and_quiet_failure(tmp_path):
    c = cfg(tmp_path)

    def get_text(url):
        return RFS_XML if "rfs" in url else json.dumps(aq_payload())

    t0 = datetime(2026, 11, 10, 7, 0, tzinfo=SYD).timestamp()
    got = conditions.load(c, date(2026, 11, 10), False, now=t0, get_text=get_text)
    assert (got.fire_rating, got.fire_ban, got.aqi) == ("HIGH", False, 160)

    def down(url):
        raise OSError("offline")

    # Within STALE_AFTER the cached data is used...
    later = conditions.load(c, date(2026, 11, 10), False, now=t0 + conditions.TTL * 3, get_text=down)
    assert later.fire_rating == "HIGH"
    # ...after that a badge is better missing than wrong.
    much_later = conditions.load(c, date(2026, 11, 10), False, now=t0 + conditions.STALE_AFTER + 1, get_text=down)
    assert much_later.fire_rating is None and much_later.aqi is None


def test_clean_air_shows_no_badge(tmp_path):
    c = cfg(tmp_path, fire_district="")

    def get_text(url):
        assert "fdrToban" not in url, "fire danger feed fetched although disabled"
        return json.dumps(aq_payload(peak=90))

    got = conditions.load(c, date(2026, 11, 10), False, now=0, get_text=get_text)
    assert got.aqi is None and got.fire_rating is None


def test_badges_use_afdrs_colours_and_put_bans_first():
    from almanac.palette import BLACK, GREEN, ORANGE, RED, WHITE, YELLOW
    from almanac.render import badges_for

    assert badges_for(None) == []
    b = badges_for(conditions.Conditions(fire_rating="HIGH", fire_ban=True, aqi=160))
    assert [x[0] for x in b] == ["TOTAL FIRE BAN", "FIRE DANGER  HIGH", "AIR QUALITY UNHEALTHY"]
    assert b[1][1:] == (YELLOW, BLACK) and b[2][1] == RED
    assert badges_for(conditions.Conditions(fire_rating="EXTREME"))[0][1:] == (ORANGE, BLACK)
    assert badges_for(conditions.Conditions(aqi=120))[0][1] == ORANGE


def test_render_draws_badges_over_the_photo(tmp_path):
    from almanac.palette import YELLOW

    c = cfg(tmp_path)
    now = datetime(2026, 11, 10, 9, 1, tzinfo=SYD)
    p = photos.Photo("1", "Lake", "credit", grey())  # a grey photo has no yellow of its own
    plain = render(now=now, poster_day=now.date(), is_tomorrow=False, forecast=None, photo=p, cfg=c)
    with_badge = render(now=now, poster_day=now.date(), is_tomorrow=False, forecast=None, photo=p, cfg=c,
                        cond=conditions.Conditions(fire_rating="HIGH"))
    corner = (12, 12, 200, 40)
    assert YELLOW not in {i for _, i in plain.crop(corner).getcolors()}
    assert YELLOW in {i for _, i in with_badge.crop(corner).getcolors()}


def test_frame_poll_is_remembered_across_restarts(tmp_path):
    app = usb_app(tmp_path)
    assert app.last_seen is None
    app.saw_frame("192.0.2.65")
    assert usb_app(tmp_path).last_seen[1] == "192.0.2.65"


# ------------------------------------------------------------------ review fixes


def test_junk_from_open_meteo_never_replaces_a_good_cache(tmp_path):
    c = cfg(tmp_path)
    weather.load(c, now=1000, fetch=lambda url: payload())
    junk = weather.load(c, now=1000 + weather.TTL_SECONDS + 1, fetch=lambda url: {"error": True})
    assert junk.fetched_at == 1000  # fell back to the good forecast...
    again = weather.load(c, now=1000 + weather.TTL_SECONDS + 2, fetch=lambda url: payload())
    assert again.fetched_at == 1000 + weather.TTL_SECONDS + 2  # ...and the cache still parses


def test_evening_posters_only_change_when_the_data_does(tmp_path, monkeypatch):
    # The next-hour line changes the poster every hour by design; everything
    # else must stay put, or the panel redraws for nothing.
    import almanac.render as R

    monkeypatch.setattr(R, "_next_hour", lambda d, forecast, x, y, now: y)
    fresh = datetime(2026, 11, 10, 18, 50, tzinfo=SYD).timestamp()  # stale stamps change hourly, rightly
    app = Almanac(
        cfg(tmp_path),
        weather_loader=lambda: weather.parse(payload(), fresh),
        photo_loader=lambda day, s: photos.Photo(str(s), f"P{s}", "credit", vivid()),
        conditions_loader=lambda day, tomorrow, from_hour: None,
        bom_loader=lambda: {},
    )
    a = app.frame_info(datetime(2026, 11, 10, 19, 1, tzinfo=SYD))["hash"]
    b = app.frame_info(datetime(2026, 11, 10, 20, 1, tzinfo=SYD))["hash"]
    assert a == b, "tomorrow view redraws hourly with identical data"


def test_png_by_hash_survives_the_hour_turning(tmp_path):
    app = usb_app(tmp_path)
    t = datetime(2026, 11, 10, 9, 59, 58, tzinfo=SYD)
    info = app.frame_info(t)
    app.poster(t.replace(hour=10, minute=0, second=1))  # the hour turns before the download
    entry = app.by_hash(info["hash"])
    assert entry is not None and len(entry["png"]) == info["size"]
    assert info["png"] == f"/frame.png?h={info['hash']}"


def test_sleep_minutes_are_real_minutes_across_dst():
    # DST starts in Sydney on 2026-10-04 at 02:00 -> 03:00.
    before = datetime(2026, 10, 3, 22, 1, 30, tzinfo=SYD)
    m = schedule.sleep_minutes(before, 6, 22)
    wake = (before.astimezone(timezone.utc) + timedelta(minutes=m)).astimezone(SYD)
    assert wake.hour == 6 and wake.minute in (1, 2), wake


def test_a_deleted_commons_photo_is_skipped_not_a_reason_to_give_up(tmp_path):
    import urllib.error

    c = cfg(tmp_path)
    pool = [entry(i) for i in range(6)]
    gone = photos.order(pool, date(2026, 11, 10))[0]["id"]

    def get(url, ua):
        if url.endswith(f"/{gone}.jpg"):
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        return jpeg(vivid())

    got = photos.photo_for(c, date(2026, 11, 10), fetcher=lambda: pool, get=get)
    assert got is not None and got.id != gone
    assert gone in json.loads((tmp_path / "photo-rejects.json").read_text())


def test_commons_api_errors_raise():
    import io as _io

    class Resp(_io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    opener = lambda req, timeout: Resp(b'{"error": {"code": "maxlag"}}')
    with pytest.raises(RuntimeError):
        photos._api({"action": "query"}, "ua", opener=opener)



# ------------------------------------------------------------------ second review


def test_cached_fire_feed_maps_by_date_after_an_offline_night(tmp_path):
    c = cfg(tmp_path)
    evening = datetime(2026, 11, 10, 21, 0, tzinfo=SYD).timestamp()
    conditions.load(c, date(2026, 11, 11), True, now=evening, get_text=lambda u: RFS_XML if "rfs" in u else json.dumps(aq_payload()))

    def down(url):
        raise OSError("offline")

    morning = datetime(2026, 11, 11, 7, 0, tzinfo=SYD).timestamp()  # 10 h later, offline
    got = conditions.load(c, date(2026, 11, 11), False, now=morning, get_text=down)
    # Yesterday's feed: its "tomorrow" (NO RATING, ban Yes) is today's; never its "today" (HIGH).
    assert (got.fire_rating, got.fire_ban) == (None, True)
    two_days = conditions.load(c, date(2026, 11, 12), True, now=morning, get_text=down)
    assert (two_days.fire_rating, two_days.fire_ban) == (None, False)


def test_junk_conditions_never_replace_a_good_cache(tmp_path):
    c = cfg(tmp_path)
    t0 = datetime(2026, 11, 10, 7, 0, tzinfo=SYD).timestamp()
    conditions.load(c, date(2026, 11, 10), False, now=t0, get_text=lambda u: RFS_XML if "rfs" in u else json.dumps(aq_payload()))
    junk = lambda u: "<html>maintenance</html>" if "rfs" in u else json.dumps({"hourly": "nope"})
    got = conditions.load(c, date(2026, 11, 10), False, now=t0 + 2 * 3600, get_text=junk)
    assert got.fire_rating == "HIGH" and got.aqi == 160


def test_a_conditions_crash_does_not_take_the_poster_down(tmp_path):
    def boom(day, tomorrow, from_hour):
        raise AttributeError("odd feed")

    app = Almanac(
        cfg(tmp_path),
        weather_loader=lambda: weather.parse(payload(), datetime(2026, 11, 10, 9, tzinfo=SYD).timestamp()),
        photo_loader=lambda day, s: photos.Photo(str(s), f"P{s}", "credit", vivid()),
        conditions_loader=boom,
        bom_loader=lambda: {},
    )
    assert app.frame_info(datetime(2026, 11, 10, 9, 1, tzinfo=SYD))["size"] > 0


def test_aqi_from_hour_ignores_smoke_that_has_cleared(tmp_path):
    p = aq_payload(peak_hour=8, peak=180)
    assert conditions.worst_daytime_aqi(p, date(2026, 11, 10), 6, 22) == 180
    c = cfg(tmp_path, fire_district="")
    t0 = datetime(2026, 11, 10, 14, 0, tzinfo=SYD).timestamp()
    get = lambda u: json.dumps(p)
    assert conditions.load(c, date(2026, 11, 10), False, now=t0, get_text=get).aqi == 180
    assert conditions.load(c, date(2026, 11, 10), False, now=t0, get_text=get, from_hour=14).aqi is None


def test_rate_limits_do_not_reject_photos(tmp_path):
    import urllib.error

    c = cfg(tmp_path)
    pool = [entry(i) for i in range(6)]

    def limited(url, ua):
        raise urllib.error.HTTPError(url, 429, "Too Many Requests", {}, None)

    photos.photo_for(c, date(2026, 11, 10), fetcher=lambda: pool, get=limited)
    assert not (tmp_path / "photo-rejects.json").exists()


def test_requests_never_crawl_when_a_stale_pool_exists(tmp_path):
    c = cfg(tmp_path)
    (tmp_path / "photo-pool.json").write_text(json.dumps({"fetched_at": 0, "pool": [entry(1)]}))

    crawls = []

    def crawl():  # load_pool swallows fetch errors, so record rather than raise
        crawls.append(1)
        return [entry(9)]

    assert photos.load_pool(c, now=10**10, fetcher=crawl)[0]["id"] == "1"
    assert crawls == [], "a request crawled Commons"
    fresh = photos.load_pool(c, now=10**10, fetcher=lambda: [entry(2)], refresh=True)
    assert fresh[0]["id"] == "2"
    assert json.loads((tmp_path / "photo-pool.json").read_text())["pool"][0]["id"] == "2"


def test_stale_stamp_does_not_change_hourly(tmp_path, monkeypatch):
    # The next-hour line changes the poster every hour by design; everything
    # else must stay put, or the panel redraws for nothing.
    import almanac.render as R

    monkeypatch.setattr(R, "_next_hour", lambda d, forecast, x, y, now: y)
    fetched = datetime(2026, 11, 10, 6, 0, tzinfo=SYD).timestamp()
    app = Almanac(
        cfg(tmp_path),
        weather_loader=lambda: weather.parse(payload(), fetched),
        photo_loader=lambda day, s: photos.Photo(str(s), f"P{s}", "credit", vivid()),
        conditions_loader=lambda day, tomorrow, from_hour: None,
        bom_loader=lambda: {},
    )
    a = app.frame_info(datetime(2026, 11, 10, 19, 1, tzinfo=SYD))["hash"]  # 13 h old
    b = app.frame_info(datetime(2026, 11, 10, 21, 1, tzinfo=SYD))["hash"]  # 15 h old
    assert a == b


def _serve(app):
    import threading
    from http.server import ThreadingHTTPServer

    from almanac.app import make_handler

    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def test_unknown_hash_is_404_and_cross_origin_shuffle_refused(tmp_path):
    import urllib.error
    import urllib.request

    app = usb_app(tmp_path)
    srv, base = _serve(app)
    try:
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(base + "/frame.png?h=deadbeefdeadbeef")
        assert e.value.code == 404
        req = urllib.request.Request(base + "/shuffle", method="POST", headers={"Origin": "http://evil.example"})
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req)
        assert e.value.code == 403
        ok = urllib.request.Request(base + "/shuffle", method="POST", data=b"")
        assert json.load(urllib.request.urlopen(ok))["shuffle"] == 1
    finally:
        srv.shutdown()



def test_first_boot_request_waits_for_the_running_crawl_instead_of_repeating_it(tmp_path):
    import threading

    c = cfg(tmp_path)
    crawls, started, release = [], threading.Event(), threading.Event()

    def slow_crawl():
        crawls.append(1)
        started.set()
        release.wait(5)
        return [entry(1)]

    t = threading.Thread(target=lambda: photos.load_pool(c, now=100, fetcher=slow_crawl, refresh=True))
    t.start()
    started.wait(5)
    result = []
    r = threading.Thread(target=lambda: result.append(photos.load_pool(c, now=100, fetcher=slow_crawl)))
    r.start()
    release.set()
    t.join(5)
    r.join(5)
    assert len(crawls) == 1 and result[0][0]["id"] == "1"


def test_fire_feed_fetched_before_six_counts_as_yesterdays(tmp_path):
    c = cfg(tmp_path)
    small_hours = datetime(2026, 11, 11, 0, 30, tzinfo=SYD).timestamp()
    got = conditions.load(c, date(2026, 11, 11), False, now=small_hours, get_text=lambda u: RFS_XML if "rfs" in u else json.dumps(aq_payload()))
    assert (got.fire_rating, got.fire_ban) == (None, True)  # the feed's Tomorrow fields
    breakfast = datetime(2026, 11, 11, 6, 30, tzinfo=SYD).timestamp()
    got = conditions.load(c, date(2026, 11, 11), False, now=breakfast, get_text=lambda u: RFS_XML if "rfs" in u else json.dumps(aq_payload()))
    assert (got.fire_rating, got.fire_ban) == ("HIGH", False)  # refetched after 06:00: Today fields
