"""Render the side-panel fixtures as stills in estimated ink colours.

    docker run --rm -v "$PWD/server:/app" -v "$PWD/docs/img:/out" inky-almanac-test \
        python tests/render_stills.py /out

Writes panel-<case>.png for every case in panel_fixtures.CASES, and
poster-radar*.png: radar mode drawn from the recorded BOM radar frame in
tests/fixtures/radar (heavy rain over Sydney, 16 Nov 2026 12:14) beside a
fixture side panel, with the frame's time moved to the panel's moment. The
stills in docs/img come from this script; regenerate them rather than edit
them.
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
    radar_stills(out, cfg)


def radar_stills(out, cfg):
    from dataclasses import replace

    from almanac import schedule
    from almanac.conditions import Conditions
    from almanac.hazards import Hazard
    from almanac.providers import bom_radar
    from almanac.render import render
    from panel_fixtures import conds, forecast, now_of, observations, preset

    fixtures = HERE / "fixtures" / "radar"

    class Recorded:
        def list(self, directory, pattern):
            return [p.name for p in fixtures.glob("IDR714.T.*.png")]

        def get(self, directory, name, limit=None):
            return (fixtures / name).read_bytes()

    src = bom_radar.BomRadar(cfg, transport=Recorded())
    src.refresh(now=4e9)
    s = preset("afternoon")
    now = now_of(s)
    radar = replace(src.latest(), issued_at=now.timestamp() - 180)
    day, is_tomorrow = schedule.poster_day(now, cfg.tomorrow_from_hour)
    storm = Hazard("storm", "watch", "Severe Thunderstorm Warning", source="BOM", ident="x", titled=True)
    cases = {"radar": conds(s), "radar-hazards": Conditions(hazards=[storm], fire_ban=True, alert="x")}
    for name, cond in cases.items():
        img = render(now=now, poster_day=day, is_tomorrow=is_tomorrow, forecast=forecast(s), photo=None, cfg=cfg,
                     cond=cond, observed=observations(s), radar=radar)
        to_preview(img).save(out / f"poster-{name}.png", optimize=True)
        print(f"poster-{name}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "stills")
