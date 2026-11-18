"""Render the side-panel fixtures as stills in estimated ink colours.

    docker run --rm -v "$PWD/server:/app" -v "$PWD/docs/img:/out" inky-almanac-test \
        python tests/render_stills.py /out

Writes, all at central Sydney:

- panel-<case>.png for every case in panel_fixtures.CASES (synthetic photo);
- the README posters, with a real Commons photo (panel_fixtures.commons_photo):
  poster-day.png, poster-evening.png, poster-hazards.png, and
  poster-warning.png (map mode, with an unlabelled synthetic warning map),
  poster-stale.png (a feed outage, for the stale-data crop), and the
  extremes poster-extreme-hot.png, poster-extreme-hot-map.png and
  poster-extreme-cold.png;
- poster-radar*.png: radar mode drawn from the recorded BOM radar frame in
  tests/fixtures/radar (heavy rain over Sydney, 16 Nov 2026 12:14) beside the
  stormy fixture side panel, with the frame's time moved to the panel's moment;
- behaviour-*.png: crops of those posters for docs/behaviours.md.

The warning, hazard and radar posters are staged: illustrative, not a real
event. The stills in docs/img come from this script; regenerate them rather
than edit them. Pillow writes them without metadata.
"""

import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parent)]  # the fixtures, and the almanac package

from almanac.config import Config  # noqa: E402
from almanac.palette import to_preview  # noqa: E402
from panel_fixtures import CASES, render_preset  # noqa: E402


def main(out):
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    cfg = Config(cache_dir=Path(tempfile.mkdtemp()))
    for name, map_mode, still in CASES:
        img, layout = render_preset(name, cfg, map_mode)
        to_preview(img).save(out / f"panel-{still}.png", optimize=True)
        print(still, "chart", layout["chart_h"], "caption", layout["caption_lines"], "/", layout["caption_wanted"],
              "facts", [(f.key, f.score) for f in layout["facts"]])
    readme_posters(out, cfg)
    radar_stills(out, cfg)
    crops(out)


def _render(name, cfg, photo, cond, map_mode=False, stale_h=0):
    from almanac import schedule
    from almanac.render import render
    from panel_fixtures import forecast, now_of, observations, preset

    s = preset(name, map_mode)
    now = now_of(s)
    day, is_tomorrow = schedule.poster_day(now, cfg.tomorrow_from_hour)
    fc = forecast(s)
    fc.fetched_at -= stale_h * 3600  # an unreachable forecast feed
    return render(now=now, poster_day=day, is_tomorrow=is_tomorrow, forecast=fc, photo=photo, cfg=cfg,
                  cond=cond, observed=observations(s))


def readme_posters(out, cfg):
    """Full posters for the README: a real photo and the current side panel."""
    from almanac.conditions import Conditions
    from almanac.domain import MapImage
    from almanac.hazards import Hazard, alert_id
    from panel_fixtures import commons_photo, conds, map_png, preset

    photo = commons_photo()
    storm = Hazard("storm", "watch", "Severe Thunderstorm Warning", None, "BOM", ident="BOM:storm",
                   detail="Sydney Metropolitan", titled=True)
    flood = Hazard("flood", "watch", "Cooks River at Canterbury", 0, "SES", ident="SES:1")
    watch = Hazard("flood", "advice", "Flood Watch", None, "BOM", specific=False, ident="BOM:fw",
                   detail="Sydney Metropolitan", titled=True)
    hazards = [storm, flood, watch]
    warning = MapImage(png=map_png(label=False), title=storm.where, area=storm.detail, issued="12:40 pm",
                       credit="Illustrative map")
    posters = {
        "day": _render("afternoon", cfg, photo, conds(preset("afternoon"))),
        "evening": _render("night", cfg, photo, conds(preset("night"))),
        "hazards": _render("storm", cfg, photo, Conditions(hazards=[storm, flood], alert=alert_id([storm, flood]))),
        "warning": _render("storm", cfg, None, Conditions(hazards=hazards, alert=alert_id(hazards), warning=warning)),
        # An outage: the forecast is 4 h old and one hazard feed has nothing.
        "stale": _render("afternoon", cfg, photo, Conditions(fire_ban=True, unavailable=["SES"]), stale_h=4),
    }
    for name, img in posters.items():
        to_preview(img).save(out / f"poster-{name}.png", optimize=True)
        print(f"poster-{name}")
    extremes(out, cfg, photo, map_png)


def extremes(out, cfg, photo, map_png):
    """The README's extremes: the worst hot afternoon in photo and map mode,
    and a bitter winter morning. The map is the unlabelled synthetic one."""
    from dataclasses import replace

    from almanac.conditions import Conditions
    from panel_fixtures import conds, preset

    hot_map = conds(preset("worstPhoto", True))
    hot_map.warning = replace(hot_map.warning, png=map_png(label=False), credit="Illustrative map")
    posters = {
        "extreme-hot": _render("worstPhoto", cfg, photo, conds(preset("worstPhoto"))),
        "extreme-hot-map": _render("worstPhoto", cfg, None, hot_map, map_mode=True),
        "extreme-cold": _render("bitter", cfg, photo, Conditions()),
    }
    for name, img in posters.items():
        to_preview(img).save(out / f"poster-{name}.png", optimize=True)
        print(f"poster-{name}")


# (crop name, source still, box) for docs/behaviours.md. Photo-mode panels
# start at x=516, map-mode panels at x=464.
CROPS = [
    ("header", "poster-day.png", (510, 0, 800, 96)),
    ("tomorrow", "poster-evening.png", (510, 0, 800, 112)),
    ("hero", "poster-day.png", (510, 100, 800, 192)),
    ("facts", "panel-worstPhoto-photo.png", (510, 118, 800, 268)),
    ("southerly", "panel-southerly-photo.png", (510, 112, 800, 186)),
    ("chart", "poster-day.png", (510, 192, 800, 334)),
    ("sun-moon", "poster-day.png", (510, 384, 800, 422)),
    ("badges", "poster-hazards.png", (0, 0, 480, 88)),
    ("map", "poster-warning.png", (0, 0, 464, 480)),
    ("warning-list", "poster-warning.png", (458, 340, 800, 480)),
    ("radar", "poster-radar.png", (0, 0, 500, 480)),
    ("stale-badge", "poster-stale.png", (0, 0, 240, 64)),
    ("stale-footer", "poster-stale.png", (510, 424, 800, 480)),
]


def crops(out):
    from PIL import Image

    for name, src, box in CROPS:
        Image.open(out / src).crop(box).save(out / f"behaviour-{name}.png", optimize=True)
        print(f"behaviour-{name}")


def radar_stills(out, cfg):
    from dataclasses import replace

    from almanac import schedule
    from almanac.conditions import Conditions
    from almanac.hazards import Hazard
    from almanac.providers import bom_radar
    from almanac.render import render
    from panel_fixtures import conds, forecast, now_of, observations, preset

    fixtures = HERE / "fixtures" / "radar"
    RAIN_FRAME = "IDR714.T.202611160114.png"  # 16 Nov 2026 12:14 AEDT, heavy rain over Sydney

    class Recorded:
        def list(self, directory, pattern):
            return [RAIN_FRAME]  # the heavy-rain frame, not the clear-night ones

        def get(self, directory, name, limit=None):
            return (fixtures / name).read_bytes()

    src = bom_radar.BomRadar(cfg, transport=Recorded())
    src.refresh(now=4e9)
    s = preset("storm")
    now = now_of(s)
    radar = replace(src.latest(), issued_at=now.timestamp() - 180)
    day, is_tomorrow = schedule.poster_day(now, cfg.tomorrow_from_hour)
    storm = Hazard("storm", "watch", "Severe Thunderstorm Warning", source="BOM", ident="x", titled=True)
    cases = {"radar": conds(s), "radar-hazards": Conditions(hazards=[storm], alert="x")}
    for name, cond in cases.items():
        img = render(now=now, poster_day=day, is_tomorrow=is_tomorrow, forecast=forecast(s), photo=None, cfg=cfg,
                     cond=cond, observed=observations(s), radar=radar)
        to_preview(img).save(out / f"poster-{name}.png", optimize=True)
        print(f"poster-{name}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "stills")
