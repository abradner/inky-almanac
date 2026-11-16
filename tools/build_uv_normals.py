#!/usr/bin/env python3
"""Build server/almanac/data/uv_normals.json: hourly UV normals for Sydney.

    tools/build_uv_normals.py                 # ARPANSA, falling back to NASA POWER
    tools/build_uv_normals.py --source power  # NASA POWER only

Primary source: ARPANSA's measured 1-minute UV index for Sydney, yearly CSVs
on data.gov.au (dataset "ultraviolet-radiation-index-sydney", CC BY 2.5 AU),
2007 onwards. Every complete year is used. Minutes are averaged to hourly
means (an hour needs 45 readings), then each (month, clock hour) cell gets
the mean and p90 of those hourly means. The CSV timestamps are Australian
Eastern Standard Time all year; cells are Sydney clock time.

Fallback, if data.gov.au refuses: NASA POWER hourly ALLSKY_SFC_UV_INDEX at
Sydney Observatory Hill, 2001-2020, converted from POWER's LST (no daylight
saving) to clock time.

The table describes a public central-Sydney site, so it is committed. Raw
downloads are kept under --raw-dir (gitignored cache/) so a rerun doesn't
fetch again. Requests are sequential, with retries and a pause between.
"""

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))
from almanac.providers import climate  # noqa: E402

TZ = "Australia/Sydney"
UA = "inky-almanac/1.0 (https://github.com/abradner/inky-almanac; UV normals build)"
CKAN = "https://data.gov.au/data/api/3/action/package_show?id=ultraviolet-radiation-index-sydney"
POWER = "https://power.larc.nasa.gov/api/temporal/hourly/point"
OBS_HILL = (-33.86, 151.21)
GAP_S = 2.0


def get(url, attempts=4):
    for i in range(attempts):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=300) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code in (401, 403, 404) or i == attempts - 1:
                raise
        except Exception:
            if i == attempts - 1:
                raise
        time.sleep(10 * 3**i)


def arpansa(raw_dir, last_year):
    meta = json.loads(get(CKAN))["result"]
    by_year = {}
    for r in meta["resources"]:
        name = r.get("name", "")
        if name.startswith("UV - Sydney-") and name.endswith(".csv"):
            by_year[int(name[len("UV - Sydney-") : -4])] = r["url"]
    years = sorted(y for y in by_year if y <= last_year)
    if not years:
        raise RuntimeError("no ARPANSA Sydney CSVs listed")
    hour_means, peaks, site = {}, {}, None
    for y in years:
        path = raw_dir / f"uv-sydney-{y}.csv"
        if not path.exists():
            print(f"  fetching ARPANSA {y}", flush=True)
            data = get(by_year[y])
            path.write_bytes(data)
            time.sleep(GAP_S)
        with path.open(encoding="utf-8-sig") as f:
            if site is None:
                f.readline()
                first = f.readline().split(",")
                site = {"lat": float(first[1]), "lon": float(first[2])}
                f.seek(0)
            got = climate.uv_hours(climate.parse_arpansa_csv(f), TZ, peaks=peaks)
        print(f"  {y}: {len(got)} hourly means", flush=True)
        hour_means.update(got)
    # A day's peak counts only if the meter ran most of the day.
    return hour_means, {d: p for d, (p, n) in peaks.items() if n >= 1200}, {
        "source": "ARPANSA measured UV index, Sydney (data.gov.au ultraviolet-radiation-index-sydney)",
        "licence": meta.get("license_title", ""),
        "site": site,
        "years": [years[0], years[-1]],
        "time_basis": "CSV timestamps are AEST (UTC+10) all year; cells are Australia/Sydney clock time",
    }


def power(raw_dir, first=2001, last=2020):
    lat, lon = OBS_HILL
    hour_means, peaks = {}, {}
    for y in range(first, last + 1):
        path = raw_dir / f"power-uv-{y}.json"
        if not path.exists():
            print(f"  fetching NASA POWER {y}", flush=True)
            q = {
                "parameters": "ALLSKY_SFC_UV_INDEX", "community": "RE", "latitude": lat, "longitude": lon,
                "start": f"{y}0101", "end": f"{y}1231", "format": "JSON", "time-standard": "LST",
            }
            path.write_bytes(get(POWER + "?" + urllib.parse.urlencode(q)))
            time.sleep(GAP_S)
        # POWER is already hourly: each sample is its own hour mean.
        samples = climate.parse_power(json.loads(path.read_text()), lon)
        hour_means.update(climate.uv_hours(samples, TZ, min_minutes=1, peaks=peaks))
    return hour_means, {d: p for d, (p, n) in peaks.items() if n >= 20}, {
        "source": "NASA POWER hourly ALLSKY_SFC_UV_INDEX (modelled, all-sky), Sydney Observatory Hill",
        "licence": "NASA POWER, open data",
        "site": {"lat": lat, "lon": lon},
        "years": [first, last],
        "time_basis": "POWER LST (UTC+10, no daylight saving) converted to Australia/Sydney clock time",
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--source", choices=("auto", "arpansa", "power"), default="auto")
    ap.add_argument("--raw-dir", type=Path, default=ROOT / "cache" / "uv-raw")
    ap.add_argument("--out", type=Path, default=Path(climate.UV_PATH))
    ap.add_argument("--last-year", type=int, default=date.today().year - 1, help="last complete year (ARPANSA)")
    a = ap.parse_args()
    a.raw_dir.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    if a.source in ("auto", "arpansa"):
        try:
            hour_means, peaks, meta = arpansa(a.raw_dir, a.last_year)
        except Exception as e:
            if a.source == "arpansa":
                raise
            print(f"ARPANSA unavailable ({e!r}); falling back to NASA POWER", flush=True)
            hour_means, peaks, meta = power(a.raw_dir)
    else:
        hour_means, peaks, meta = power(a.raw_dir)
    meta.update(
        {
            "credit": climate.UV_CREDIT if meta["source"].startswith("ARPANSA") else "UV estimates from NASA POWER",
            "method": (
                "hourly: mean and p90 of hourly means (hours with >= 45 one-minute readings) per month x clock hour; "
                "daily_peak: median and p90 of each day's highest reading (days with >= 1200 readings)"
            ),
            "generated": date.today().isoformat(),
            "generator": "tools/build_uv_normals.py",
        }
    )
    table = climate.uv_table(hour_means, meta, peaks)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps({k: v for k, v in table.items() if k != "hourly"}, indent=1)[:-2]
    rows = ",\n".join("  " + json.dumps(row, separators=(",", ":")) for row in table["hourly"])
    a.out.write_text(body + ',\n "hourly": [\n' + rows + "\n ]\n}\n")
    print(f"wrote {a.out} from {meta['source']} in {time.monotonic() - started:.0f} s")
    for m, label in ((1, "Jan"), (7, "Jul"), (12, "Dec")):
        noon = max(range(24), key=lambda h: table["hourly"][m - 1][h]["mean"] or 0)
        c = table["hourly"][m - 1][noon]
        p = table["daily_peak"][m - 1]
        print(f"  {label}: peak hour {noon}:00 mean {c['mean']} p90 {c['p90']} (n={c['n']}); daily peak p50 {p['p50']} p90 {p['p90']}")


if __name__ == "__main__":
    main()
