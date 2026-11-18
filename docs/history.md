# History

When and why each piece arrived. PR numbers restart in this repo, so they
are always given with the repo. Dates are Sydney time.

## Before the restart: `abradner/inky-almanac-deprecated`

Built as one stack on 9–10 November 2026 and merged together on the 10th.
That history holds real location details, which is why this repo began
again ([design.md §17](design.md#17-the-clean-public-safe-repository)).

| PR | What | Why |
|---|---|---|
| #1 | v1: renderer in docker, Commons photo + Open-Meteo weather, indexed-PNG poster, frame app | the first poster; the Pico W can't lay out or dither, so the server renders |
| #2 | Fire danger, Total Fire Ban and air-quality badges | safety conditions at a glance, only when they matter |
| #3 | Fixes from three independent reviews | junk-proof caches, no hourly redraw from a clock, hash-addressed PNGs, DST-safe sleep, fire feed dated by fetch time |
| #4 | Redraw on power-on, reliable button A, frame tests | a replug looked like it did nothing; button A stalled |
| #5 | Hazards near home (RFS, SES via HazardWatch, BOM warnings) and BOM's town forecast | "hazards in general", and BOM is the better local forecaster |
| #6 | Review of #4/#5 | per-feature failure isolation, edge distance, 90-minute staleness, cache-only requests, a cycle watchdog, raw-edge buttons |
| #7 | Signed over-the-air updates with a rollback guard | change frame code without a USB cable, safely |
| #8 | Retry the Wi-Fi join once | half the boots after a reset missed the first join; the first OTA release |
| #9 | README rewrite | a brochure for users and a guide for developers |

## This repo: `abradner/inky-almanac`

The initial commit (`e6dbf50`, 10 Nov) is the whole of the above, with
central-Sydney defaults and the real configuration gitignored.

| PR | Merged | What | Why |
|---|---|---|---|
| #1 | 12 Nov | One PNG decoder per boot; fresh-heap resets | the frame hit `MemoryError` after long USB uptime |
| #2 | 13 Nov | Specific vs broad hazards, `/alert.json` polling, BOM warning maps in fixed inks | a live severe thunderstorm warning: warnings should reach the wall within minutes, with the map |
| #3 | 13 Nov | Condensed type, a next-hour line, a wider panel beside maps | text was clipping; the hourly poster should say what the next hour holds |
| #4 | 13 Nov | Advice is a notice; cancellations clear at once with no map | a cancelled warning's map lingered; SES advice wasn't worth a redraw |
| #5 | 16 Nov | Hourly-by-month climate normals (ERA5 + ARPANSA UV) | "above normal" needs a normal for this month and hour |
| #6 | 16 Nov | Side panel take A2: the coming hour leads, ranked facts, tiered re-checks | hourly refreshes make the coming hour the most useful thing to show |
| #7 | 16 Nov | Providers behind adapters; BOM station observations | forkability, and measured rain and southerly changes instead of model output |
| #8 | 16 Nov | `docs/design.md` | keep the reasons from being lost |
| #9 | 16 Nov | BOM wins where it publishes a figure; the day's rain is back | the header said 21° while the chart said 18° |
| #10 | 16 Nov | Radar mode on button C | see rain arriving, on demand |

The panel redesign behind #6 was worked out in an interactive mockup
("Inky Panel Lab", a private claude.ai artifact) with worst-case presets
and a clipping detector; the renderer's own fixtures are the source of truth
now.
