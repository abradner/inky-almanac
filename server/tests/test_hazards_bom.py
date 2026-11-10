import json
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from almanac import bom, conditions, hazards as hz, weather
from almanac.config import Config
from almanac.palette import BLACK, ORANGE, RED, WHITE, YELLOW
from almanac.render import MAX_BADGES, badges_for

SYD = ZoneInfo("Australia/Sydney")
HOME = (-33.87, 151.21)  # central Sydney


def cfg(tmp_path, **kw):
    return Config(cache_dir=tmp_path, **kw)


def square(lon, lat, d=0.02):
    return {"type": "Polygon", "coordinates": [[[lon - d, lat - d], [lon + d, lat - d], [lon + d, lat + d], [lon - d, lat + d], [lon - d, lat - d]]]}


# Shaped like rfs.nsw.gov.au/feeds/majorIncidents.json
RFS = {
    "type": "FeatureCollection",
    "features": [
        {"geometry": {"type": "GeometryCollection", "geometries": [{"type": "Point", "coordinates": [151.19, -33.82]}]},
         "properties": {"title": "RIVER RD, LANE COVE", "category": "Watch and Act"}},
        {"geometry": {"type": "Point", "coordinates": [147.47, -32.87]},
         "properties": {"title": "FIFIELD RD, FIFIELD", "category": "Advice"}},  # 450 km away
        {"geometry": {"type": "Point", "coordinates": [151.22, -33.88]},
         "properties": {"title": "MACQUARIE ST, SYDNEY", "category": "Not Applicable"}},
        {"geometry": {"type": "Point", "coordinates": [151.23, -33.86]},
         "properties": {"title": "Burn, Centennial Park", "category": "Planned Burn"}},
    ],
}


def hazardwatch_html(alerts, features):
    data = {"props": {"pageProps": {"alertData": {"alerts": alerts, "geojson": {"type": "FeatureCollection", "features": features}}}}}
    return f'<html><script id="__NEXT_DATA__" type="application/json">{json.dumps(data)}</script></html>'


def ses_alert(ident, level, place, event="Riverine Flood", agency="NSWSES"):
    return {
        "identifier": ident, "status": "Actual", "agencyCode": agency,
        "info": {"event": event, "headline": f"{place} flooding", "parameter": {
            "AffectedLocation": place, "AustralianWarningSystem:WarningLevel": level}},
    }


BOM_RSS = """<?xml version="1.0" encoding="utf-8" ?><rss version="2.0"><channel><title>NSW warnings</title>
<item><title>10/04:40 EDT Hazardous Surf Warning for New South Wales</title></item>
<item><title>10/05:00 EDT Hazardous Surf Warning for the Sydney Metropolitan coast</title></item>
<item><title>10/09:10 EDT Severe Thunderstorm Warning for people in Sydney Metropolitan and Hunter</title></item>
<item><title>09/12:15 EDT Minor Flood Warning for the Warrego River (NSW)</title></item>
<item><title>10/08:00 EDT Flood Watch for the Hawkesbury-Nepean</title></item>
<item><title>10/07:00 EDT Final Flood Warning for the Hawkesbury River</title></item>
</channel></rss>"""


PRECIS = """<?xml version="1.0"?><product><forecast>
<area aac="NSW_PT131" description="Sydney" type="location">
 <forecast-period index="0" start-time-local="2026-11-10T10:00:00+11:00">
  <element type="forecast_icon_code">1</element>
  <element type="air_temperature_maximum" units="Celsius">27</element>
  <text type="precis">Sunny.</text><text type="probability_of_precipitation">5%</text>
 </forecast-period>
 <forecast-period index="1" start-time-local="2026-11-11T00:00:00+11:00">
  <element type="forecast_icon_code">16</element>
  <element type="precipitation_range">0 to 2 mm</element>
  <element type="air_temperature_minimum" units="Celsius">14</element>
  <element type="air_temperature_maximum" units="Celsius">29</element>
  <text type="precis">Shower or two. Possible storm.</text><text type="probability_of_precipitation">50%</text>
 </forecast-period>
</area></forecast></product>"""


# ------------------------------------------------------------------ geometry


def test_inside_a_polygon_is_zero_km_and_outside_is_measured():
    assert hz.distance_km(square(151.21, -33.87), *HOME) == 0.0
    far = hz.distance_km({"type": "Point", "coordinates": [151.21, -33.97]}, *HOME)
    assert 10.5 < far < 11.6  # 0.1 deg of latitude
    assert hz.distance_km(None, *HOME) is None


# ------------------------------------------------------------------ RFS


def test_rfs_keeps_nearby_warnings_and_drops_the_rest():
    got = hz.rfs_hazards(RFS, *HOME, radius_km=30)
    assert [(h.kind, h.level, h.where) for h in got] == [("fire", "watch", "Lane Cove")]
    assert 4 <= got[0].km <= 7


# ------------------------------------------------------------------ SES


def test_ses_alert_covering_home_counts_and_far_ones_do_not():
    html = hazardwatch_html(
        [ses_alert("a1", "Watch and Act", "Cooks River at Tempe"), ses_alert("a2", "Advice", "Willara Crossing"),
         ses_alert("a3", "Emergency Warning", "RFS dupe", agency="NSWRFS")],
        [{"geometry": square(151.21, -33.87, 0.2), "properties": {"alertIdentifier": "a1"}},
         {"geometry": square(144.35, -29.03), "properties": {"alertIdentifier": "a2"}},
         {"geometry": square(151.21, -33.87), "properties": {"alertIdentifier": "a3"}}],
    )
    got = hz.ses_hazards(hz.hazardwatch_data(html), *HOME, radius_km=30)
    assert [(h.kind, h.level, h.where, h.km) for h in got] == [("flood", "watch", "Cooks River at Tempe", 0)]


def test_hazardwatch_page_without_data_raises():
    with pytest.raises(ValueError):
        hz.hazardwatch_data("<html>maintenance</html>")


# ------------------------------------------------------------------ BOM


def test_bom_warnings_filtered_to_home_areas():
    c = Config()
    got = hz.bom_hazards(BOM_RSS, c.bom_warning_areas, c.bom_warning_ignore)
    assert [(h.where, h.level, h.kind) for h in got] == [
        ("Severe Thunderstorm Warning", "watch", "storm"),
        ("Flood Watch", "advice", "flood"),
    ]


def test_bom_precis_parses_and_overlays_field_by_field():
    days = bom.parse(PRECIS, "Sydney")
    today, tomorrow = days[date(2026, 11, 10)], days[date(2026, 11, 11)]
    assert (today.t_max, today.t_min, today.precis, today.rain_prob, today.icon) == (27, None, "Sunny", 5, "clear")
    assert (tomorrow.t_min, tomorrow.rain_range, tomorrow.icon) == (14, "0 to 2 mm", "storm")
    with pytest.raises(ValueError):
        bom.parse(PRECIS, "Atlantis")

    om = weather.Day(date(2026, 11, 10), 3, 25.0, 11.0, 20, 0.0, 8.0, datetime(2026, 11, 10, 5, 48), datetime(2026, 11, 10, 19, 31), [])
    bom.overlay(om, today)
    assert (om.t_max, om.t_min, om.label, om.icon, om.rain_prob, om.source) == (27, 11.0, "Sunny", "clear", 5, "BOM")


# ------------------------------------------------------------------ loading


def test_one_broken_source_does_not_hide_the_others(tmp_path):
    c = cfg(tmp_path)

    def get_text(url):
        if "majorIncidents" in url:
            return json.dumps(RFS)
        if "hazardwatch" in url:
            return "<html>changed shape</html>"
        if "IDZ00054" in url:
            return BOM_RSS
        raise OSError("not needed here")

    got, missing = conditions.load_hazards(c, datetime(2026, 11, 10, 9, tzinfo=SYD).timestamp(), get_text)
    assert {h.source for h in got} == {"RFS", "BOM"} and missing == ["SES"]
    assert got[0].level == "watch"  # sorted most urgent first


def test_bom_days_fall_back_to_nothing_when_unreachable(tmp_path):
    def down(url):
        raise OSError("ftp down")

    assert conditions.load_bom_days(cfg(tmp_path), now=0, get_text=down) == {}
    got = conditions.load_bom_days(cfg(tmp_path), now=0, get_text=lambda u: PRECIS)
    assert got[date(2026, 11, 11)].t_max == 29


# ------------------------------------------------------------------ badges


def test_badge_order_colours_and_cap():
    hs = [
        hz.Hazard("fire", "advice", "Ku-Ring-Gai", 6, "RFS"),
        hz.Hazard("flood", "emergency", "Tempe", 0, "NSWSES"),
        hz.Hazard("storm", "watch", "Severe Thunderstorm Warning", None, "BOM"),
    ]
    b = badges_for(conditions.Conditions(fire_ban=True, hazards=hz.sort_and_dedupe(hs)))
    assert [x[0] for x in b] == [
        "FLOOD · EMERGENCY · Tempe",
        "TOTAL FIRE BAN",
        "SEVERE THUNDERSTORM WARNING",
        "FIRE · ADVICE · Ku-Ring-Gai 6 km",
    ]
    assert b[0][1:] == (RED, WHITE) and b[2][1:] == (WHITE, RED) and b[3][1:] == (YELLOW, BLACK)

    many = [hz.Hazard("fire", "advice", f"Place{i}", i, "RFS") for i in range(6)]
    capped = badges_for(conditions.Conditions(hazards=many, aqi=160))
    assert len(capped) == MAX_BADGES and capped[-1][0].startswith("+4 MORE")


def test_moderate_fire_danger_no_longer_gets_a_badge():
    assert badges_for(conditions.Conditions(fire_rating="MODERATE")) == []
    assert badges_for(conditions.Conditions(fire_rating="HIGH"))[0][0] == "FIRE DANGER  HIGH"


# ------------------------------------------------------------------ review fixes


def test_a_long_edge_passing_close_counts_even_if_its_corners_are_far():
    # A thin strip 5 km south of home, corners ~45 km away east and west.
    strip = {"type": "Polygon", "coordinates": [[[150.71, -33.915], [151.71, -33.915], [151.71, -33.94], [150.71, -33.94], [150.71, -33.915]]]}
    km = hz.distance_km(strip, *HOME)
    assert 4.5 < km < 5.5


def test_malformed_features_are_skipped_not_fatal():
    bad = {"features": [
        {"properties": None, "geometry": {"type": "Point", "coordinates": [151.21, -33.84]}},
        {"properties": {"title": None, "category": "Advice"}, "geometry": {"type": "Point", "coordinates": [151.21, -33.84]}},
        {"properties": {"title": "X, Y", "category": "Advice"}, "geometry": {"type": "Point", "coordinates": None}},
        {"properties": {"title": "Z", "category": "Advice"}, "geometry": {"type": "Point", "coordinates": ["junk", "data"]}},
        RFS["features"][0],
    ]}
    got = hz.rfs_hazards(bad, *HOME, radius_km=30)
    assert ("Lane Cove", "watch") in {(h.where, h.level) for h in got}
    assert ("Unnamed", "advice") in {(h.where, h.level) for h in got}


def test_expired_ses_alerts_are_dropped():
    alert = ses_alert("e1", "Watch and Act", "Tempe")
    alert["info"]["expires"] = "2026-11-10T00:00:00Z"
    data = hz.hazardwatch_data(hazardwatch_html([alert], [{"geometry": square(151.21, -33.87), "properties": {"alertIdentifier": "e1"}}]))
    before = datetime(2026, 11, 9, 23, 0, tzinfo=ZoneInfo("UTC"))
    after = datetime(2026, 11, 10, 1, 0, tzinfo=ZoneInfo("UTC"))
    assert len(hz.ses_hazards(data, *HOME, 30, now_utc=before)) == 1
    assert hz.ses_hazards(data, *HOME, 30, now_utc=after) == []


def test_hazard_data_older_than_90_min_counts_as_unavailable(tmp_path):
    c = cfg(tmp_path)
    t0 = datetime(2026, 11, 10, 9, tzinfo=SYD).timestamp()
    ok = lambda u: json.dumps(RFS) if "majorIncidents" in u else BOM_RSS if "IDZ00054" in u else hazardwatch_html([], [])

    def down(url):
        raise OSError("down")

    hs, missing = conditions.load_hazards(c, t0, ok)
    assert missing == [] and hs
    hs, missing = conditions.load_hazards(c, t0 + 60 * 60, down)  # cached, 1 h old: still used
    assert missing == [] and hs
    hs, missing = conditions.load_hazards(c, t0 + 2 * 60 * 60, down)  # 2 h old: not shown as current
    assert hs == [] and missing == ["RFS", "SES", "BOM"]


def test_unavailable_sources_get_a_neutral_badge_and_bom_its_own_style():
    b = badges_for(conditions.Conditions(unavailable=["SES"], hazards=[hz.Hazard("storm", "watch", "Severe Thunderstorm Warning", None, "BOM")]))
    assert b[0] == ("SEVERE THUNDERSTORM WARNING", WHITE, RED)
    assert b[1] == ("NO LIVE HAZARD DATA: SES", WHITE, BLACK)


def test_bom_afternoon_issue_keeps_todays_first_seen_max_and_precis(tmp_path):
    c = cfg(tmp_path)
    morning = conditions.load_bom_days(c, now=0, get_text=lambda u: PRECIS)
    assert morning[date(2026, 11, 10)].t_max == 27
    afternoon = PRECIS.replace('<element type="air_temperature_maximum" units="Celsius">27</element>', "").replace("Sunny.", "Clear.")
    later = conditions.load_bom_days(c, now=10**6, get_text=lambda u: afternoon)
    assert later[date(2026, 11, 10)].t_max == 27 and later[date(2026, 11, 10)].precis == "Sunny"


def test_request_path_never_touches_the_network(tmp_path, monkeypatch):
    import urllib.request

    from almanac.app import Almanac
    from almanac import photos

    calls = []

    def no_network(*a, **k):  # loaders swallow errors, so record, don't raise
        calls.append(a[0].full_url if a and hasattr(a[0], "full_url") else a)
        raise OSError("no network in tests")

    monkeypatch.setattr(urllib.request, "urlopen", no_network)
    # A cached forecast, so the BOM overlay path runs too.
    from test_almanac import payload

    t = datetime(2026, 11, 10, 9, 1, tzinfo=SYD).timestamp()
    (tmp_path / "weather.json").write_text(json.dumps({"fetched_at": t, "payload": payload()}))
    app = Almanac(cfg(tmp_path), photo_loader=lambda d, s: None)
    info = app.frame_info(datetime(2026, 11, 10, 9, 1, tzinfo=SYD))
    assert info["size"] > 0  # renders from (empty) caches...
    assert calls == [], f"...without fetching: {calls}"
