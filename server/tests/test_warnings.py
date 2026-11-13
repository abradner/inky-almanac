"""Specific vs broad hazards, the alert id, BOM warning maps, and the poster
reacting to a warning mid-hour."""

import base64
import io
import json
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from PIL import Image

from almanac import conditions, hazards as hz, weather, photos
from almanac.app import Almanac
from almanac.config import Config
from almanac.palette import BLUE, RED, WHITE
from almanac.render import badges_for, draw_warning_map, render
from test_almanac import _serve, payload, vivid

SYD = ZoneInfo("Australia/Sydney")

RSS = """<?xml version="1.0"?><rss version="2.0"><channel><title>NSW</title>
<item><title>13/17:13 EDT Detailed Severe Thunderstorm Warning for Blue Mountains/Hawkesbury and Sydney</title>
<link>http://reg.bom.gov.au/products/IDN21035.shtml</link></item>
<item><title>13/17:10 EDT Severe Thunderstorm Warning for Sydney Metropolitan</title>
<link>http://reg.bom.gov.au/products/IDN21033.shtml</link></item>
<item><title>13/08:00 EDT Flood Watch for the Hawkesbury-Nepean</title>
<link>http://reg.bom.gov.au/products/IDN36700.shtml</link></item>
</channel></rss>"""


def map_png(colour=(255, 220, 0)):
    img = Image.new("RGB", (70, 75), (240, 235, 220))
    img.paste(Image.new("RGB", (30, 75), (205, 230, 250)), (40, 0))  # "sea"
    img.paste(Image.new("RGB", (10, 10), colour), (10, 10))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def page(png, issued="5:13 pm Friday, 13 November 2026"):
    b64 = base64.b64encode(png).decode()
    return f'<p>Issued at {issued}.<br>\n</p>\n<img alt="" src="data:image/png;base64,{b64}">'


def cfg(tmp_path):
    return Config(cache_dir=tmp_path)


def test_bom_warnings_are_classified_specific_or_broad():
    c = Config()
    got = {h.where: h for h in hz.bom_hazards(RSS, c.bom_warning_areas, c.bom_warning_ignore)}
    assert got["Detailed Severe Thunderstorm Warning"].specific
    assert got["Severe Thunderstorm Warning"].specific
    assert not got["Flood Watch"].specific
    assert got["Detailed Severe Thunderstorm Warning"].url.endswith("IDN21035.shtml")


def test_alert_id_tracks_specific_hazards_only():
    c = Config()
    hs = hz.bom_hazards(RSS, c.bom_warning_areas, c.bom_warning_ignore)
    a = hz.alert_id(hs)
    assert a and a == hz.alert_id(list(reversed(hs)))  # order doesn't matter
    reissued = RSS.replace("13/17:13 EDT Detailed", "13/17:43 EDT Detailed")
    assert hz.alert_id(hz.bom_hazards(reissued, c.bom_warning_areas, c.bom_warning_ignore)) != a
    broad_only = [h for h in hs if not h.specific]
    assert hz.alert_id(broad_only) == ""


def test_page_map_extracts_the_embedded_png_and_issue_time():
    png = map_png()
    data, issued = hz.page_map(page(png))
    assert data == png and issued == "5:13 pm"
    assert hz.page_map("<p>Issued at 5:13 pm Friday.</p>")[0] is None
    bogus = '<img src="data:image/png;base64,' + base64.b64encode(b"not a png").decode() + '">'
    assert hz.page_map(bogus)[0] is None


def test_detailed_storm_warning_map_is_preferred_and_cached(tmp_path):
    c = cfg(tmp_path)
    fetched = []

    def get_text(url):
        fetched.append(url)
        return page(map_png((255, 0, 0)) if "IDN21035" in url else map_png())

    hs = list(reversed(hz.bom_hazards(RSS, c.bom_warning_areas, c.bom_warning_ignore)))  # detailed one last
    w = conditions.load_warning_map(c, hs, 1000, get_text)
    assert w["title"] == "Detailed Severe Thunderstorm Warning" and w["issued"] == "5:13 pm"
    assert fetched == ["http://reg.bom.gov.au/products/IDN21035.shtml"]

    def offline(url):
        raise OSError("cache only")

    again = conditions.load_warning_map(c, hs, 1050, offline)
    assert again["png"] == w["png"]


def test_warning_map_replaces_the_photo_and_the_caption(tmp_path):
    w = {"png": map_png(), "title": "Detailed Severe Thunderstorm Warning", "issued": "5:13 pm"}
    hs = [hz.Hazard("storm", "watch", w["title"], None, "BOM", ident="a", url="x")]
    cond = conditions.Conditions(hazards=hs, alert="a", warning=w)
    now = datetime(2026, 11, 13, 17, 20, tzinfo=SYD)
    img = render(now=now, poster_day=now.date(), is_tomorrow=False, forecast=None,
                 photo=photos.Photo("1", "P", "c", Image.new("RGB", (900, 600), (40, 130, 50))), cfg=cfg(tmp_path), cond=cond)
    left = {i for _, i in img.crop((0, 0, 500, 480)).getcolors()}
    assert BLUE in left  # the map's sea; the (all-green) photo has no blue
    # its own BOM badge isn't repeated over the map (the caption says it)
    assert all(b[0] != "DETAILED SEVERE THUNDERSTORM WARNING" for b in badges_for(cond))


def test_broad_notices_get_small_badges_after_specific_ones():
    hs = [hz.Hazard("flood", "advice", "Flood Watch", None, "BOM", specific=False, ident="f"),
          hz.Hazard("storm", "watch", "Severe Thunderstorm Warning", None, "BOM", ident="s")]
    b = badges_for(conditions.Conditions(hazards=hs, fire_ban=True))
    texts = [x[0] for x in b]
    assert texts.index("SEVERE THUNDERSTORM WARNING") < texts.index("FLOOD WATCH")
    assert not b[texts.index("SEVERE THUNDERSTORM WARNING")].small
    assert b[texts.index("FLOOD WATCH")].small and b[texts.index("TOTAL FIRE BAN")].small


def _app(tmp_path, state):
    return Almanac(
        cfg(tmp_path),
        weather_loader=lambda: weather.parse(payload(), datetime(2026, 11, 13, 17, tzinfo=SYD).timestamp()),
        photo_loader=lambda d, s: photos.Photo("1", "P", "c", vivid()),
        conditions_loader=lambda d, t, h: state["cond"],
        bom_loader=lambda: {},
    )


def test_a_warning_mid_hour_makes_a_new_poster_and_alert(tmp_path):
    state = {"cond": conditions.Conditions()}
    app = _app(tmp_path, state)
    t = datetime(2026, 11, 13, 17, 5, tzinfo=SYD)
    quiet = app.frame_info(t)
    assert quiet["alert"] == "" and quiet["alert_poll_minutes"] >= 1
    hs = [hz.Hazard("storm", "watch", "Severe Thunderstorm Warning", None, "BOM", ident="BOM:x")]
    state["cond"] = conditions.Conditions(hazards=hs, alert=hz.alert_id(hs))
    stormy = app.frame_info(t.replace(minute=20))  # same hour slot
    assert stormy["alert"] == hz.alert_id(hs) and stormy["hash"] != quiet["hash"]


def test_alert_endpoint_is_cheap_and_matches(tmp_path):
    hs = [hz.Hazard("storm", "watch", "Severe Thunderstorm Warning", None, "BOM", ident="BOM:x")]
    state = {"cond": conditions.Conditions(hazards=hs, alert=hz.alert_id(hs))}
    srv, base = _serve(_app(tmp_path, state))
    try:
        assert json.load(urllib.request.urlopen(base + "/alert.json")) == {"id": hz.alert_id(hs)}
    finally:
        srv.shutdown()


@pytest.mark.parametrize("rgb, ink", [
    ((255, 242, 211), WHITE),  # land
    ((255, 255, 255), WHITE),
    ((219, 237, 255), BLUE),   # sea
    ((255, 220, 0), 5),        # warning area -> yellow
    ((250, 170, 30), 6),       # immediate threat -> orange
    ((230, 50, 50), RED), ((210, 10, 10), RED), ((180, 0, 0), RED),
    ((0, 0, 255), 0), ((7, 7, 254), 0),  # town labels -> black
    ((51, 51, 51), 0), ((0, 0, 0), 0),   # region names, text
    ((128, 128, 128), 2), ((127, 121, 105), 2), ((110, 119, 128), 2),  # boundaries -> green
])
def test_bom_map_colours_get_deliberate_inks(rgb, ink):
    from almanac.dither import map_ink

    assert map_ink(*rgb) == ink


# ---- review fixes -------------------------------------------------------------


def test_a_corrupt_map_is_never_cached_and_never_breaks_the_poster(tmp_path):
    truncated = map_png()[:60]
    assert hz.page_map(page(truncated))[0] is None
    huge = Image.new("RGB", (3000, 2000), (255, 255, 255))
    buf = io.BytesIO(); huge.save(buf, "PNG")
    assert hz.page_map(page(buf.getvalue()))[0] is None  # over the pixel cap
    # and if a bad map reaches render anyway, the photo is shown instead
    w = {"png": b"\x89PNG not really", "title": "Severe Thunderstorm Warning", "issued": ""}
    cond = conditions.Conditions(hazards=[], warning=w)
    now = datetime(2026, 11, 13, 17, 20, tzinfo=SYD)
    img = render(now=now, poster_day=now.date(), is_tomorrow=False, forecast=None,
                 photo=photos.Photo("1", "P", "c", vivid()), cfg=cfg(tmp_path), cond=cond)
    assert img.size == (800, 480) and cond.warning is None


def test_the_largest_embedded_image_is_the_map():
    tiny = Image.new("RGB", (4, 4), (0, 0, 255)); b = io.BytesIO(); tiny.save(b, "PNG")
    html = (f"<img src='data:image/png;base64,{base64.b64encode(b.getvalue()).decode()}'>"
            + page(map_png()))
    assert hz.page_map(html)[0] == map_png()


def test_map_cache_is_per_issue_so_an_old_track_never_shows(tmp_path):
    c = cfg(tmp_path)
    first = [hz.Hazard("storm", "watch", "Detailed Severe Thunderstorm Warning", None, "BOM", ident="BOM:17:13", url="http://x/IDN21035.shtml")]
    conditions.load_warning_map(c, first, 1000, lambda u: page(map_png((255, 0, 0))))
    reissue = [hz.Hazard("storm", "watch", "Detailed Severe Thunderstorm Warning", None, "BOM", ident="BOM:17:43", url="http://x/IDN21035.shtml")]

    def down(url):
        raise OSError("page fetch failed")

    assert conditions.load_warning_map(c, reissue, 1100, down) is None  # not the 17:13 map


def test_specific_hazards_beat_broad_badges_over_a_map():
    hs = [hz.Hazard("fire", "watch", "Lane Cove", 6, "RFS", ident="f"),
          hz.Hazard("storm", "watch", "Detailed Severe Thunderstorm Warning", None, "BOM", ident="s", url="u")]
    cond = conditions.Conditions(hazards=hs, fire_ban=True,
                                 warning={"png": b"", "title": "Detailed Severe Thunderstorm Warning", "issued": ""})
    texts = [b[0] for b in badges_for(cond)]
    assert texts[0] == "FIRE · WATCH AND ACT · Lane Cove 6 km"


def test_two_flood_warnings_for_different_rivers_both_survive():
    a = hz.Hazard("flood", "watch", "Flood Warning", None, "BOM", ident="BOM:13/10:00 Flood Warning for the Hawkesbury River")
    b = hz.Hazard("flood", "watch", "Flood Warning", None, "BOM", ident="BOM:13/10:05 Flood Warning for the Nepean River")
    assert len(hz.sort_and_dedupe([a, b])) == 2
