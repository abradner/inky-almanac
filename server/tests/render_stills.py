"""Render the side-panel fixtures as stills in estimated ink colours.

    docker run --rm -v "$PWD/server:/app" -v "$PWD/docs/img:/out" inky-almanac-test \
        python tests/render_stills.py /out

Writes panel-<case>.png for every case in panel_fixtures.CASES. The stills
in docs/img come from this script; regenerate them rather than edit them.
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


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "stills")
