"""HTTP service the frame polls.

GET  /frame.json   {hash, size, sleep_minutes, ...}: cheap; the frame compares hash
GET  /frame.png    the poster as a 4-bit indexed PNG whose indices are panel inks
GET  /preview.png  the same poster in the inks' real-world colours, for browsers
POST /shuffle      pick a different photo for the rest of today
GET  /             a small status page with the preview

Add ?at=2026-11-10T21:30 to any GET to render for another moment (debugging).
"""

import hashlib
from html import escape
import io
import json
import threading
import time
from datetime import datetime
from pathlib import Path
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

from . import bom, conditions, photos
from . import schedule, weather
from .config import Config
from .palette import to_preview
from .render import render


ALERT_POLL_MINUTES = 2  # how often a USB-powered frame asks /alert.json
ALERT_REFRESH_S = 90  # how often the renderer re-reads the hazard feeds


class Almanac:
    def __init__(self, cfg, *, weather_loader=None, photo_loader=None, conditions_loader=None, bom_loader=None):
        self.cfg = cfg
        self.tz = ZoneInfo(cfg.timezone)
        self.cfg.cache_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._cache = {}
        # Requests read caches only; refresh_feeds() (a background thread in
        # production) does the network work, so a slow or dead feed can never
        # make the frame's 60 s poll time out.
        self._weather = weather_loader or (lambda: weather.load(cfg, fetch=conditions.offline))
        self._photo = photo_loader or (lambda day, shuffle: photos.photo_for(cfg, day, shuffle))
        self._bom = bom_loader or (lambda: conditions.load_bom_days(cfg, get_text=conditions.offline))
        self._conditions = conditions_loader or (
            lambda day, tomorrow, from_hour: conditions.load(
                cfg, day, tomorrow, from_hour=from_hour, get_text=conditions.offline
            )
        )
        self.ota_dir = Path(__file__).resolve().parents[1] / "ota"  # signed frame release
        self._seen_path = cfg.cache_dir / "frame-last-seen.json"
        try:  # (unix time, client address) of the frame's last poll
            self.last_seen = tuple(json.loads(self._seen_path.read_text()))
        except (OSError, ValueError):
            self.last_seen = None
        self.on_change = []  # callbacks run after /shuffle (USB mode pushes at once)

    # -- shuffle state survives restarts, resets each day
    def _shuffle_path(self):
        return self.cfg.cache_dir / "shuffle.json"

    def shuffle_count(self, day):
        try:
            s = json.loads(self._shuffle_path().read_text())
            return s["n"] if s.get("day") == day.isoformat() else 0
        except (OSError, ValueError, KeyError):
            return 0

    def shuffle(self, now):
        day, _ = schedule.poster_day(now, self.cfg.tomorrow_from_hour)
        n = self.shuffle_count(day) + 1
        tmp = self._shuffle_path().with_suffix(".tmp")
        tmp.write_text(json.dumps({"day": day.isoformat(), "n": n}))
        tmp.replace(self._shuffle_path())
        with self._lock:
            self._cache.clear()
        for callback in self.on_change:
            callback()
        return n

    def saw_frame(self, address):
        self.last_seen = (time.time(), address)
        try:
            self._seen_path.write_text(json.dumps(self.last_seen))
        except OSError:
            pass

    def now(self, at=None):
        if at:
            return datetime.fromisoformat(at).replace(tzinfo=self.tz)
        return datetime.now(self.tz)

    def poster(self, now):
        """Render (or reuse this hour's render of) the poster for `now`."""
        day, is_tomorrow = schedule.poster_day(now, self.cfg.tomorrow_from_hour)
        shuffle = self.shuffle_count(day)
        cond = self.conditions(day, is_tomorrow, now)
        # The alert is part of the key: a warning issued mid-hour must make a
        # new poster now, not at the next slot.
        key = (schedule.slot(now), day, shuffle, cond.alert if cond else "")
        with self._lock:
            hit = self._cache.get(key)
            if hit:
                return hit
            forecast = self._weather()
            if forecast is not None:
                try:  # BOM's official daily numbers over Open-Meteo's (optional)
                    days = self._bom()
                    for d, b in days.items():
                        bom.overlay(forecast.day(d), b)
                except Exception as e:
                    print(f"BOM overlay failed: {e!r}", flush=True)
            photo = self._photo(day, shuffle)
            canvas = render(now=now, poster_day=day, is_tomorrow=is_tomorrow, forecast=forecast, photo=photo, cfg=self.cfg, cond=cond)
            buf = io.BytesIO()
            canvas.save(buf, "PNG", bits=4, optimize=True)
            png = buf.getvalue()
            entry = {
                "png": png,
                "canvas": canvas,
                "hash": hashlib.sha256(png).hexdigest()[:16],
                "photo": None if photo is None else f"{photo.title} ({photo.credit})",
                "conditions": cond,
                "alert": cond.alert if cond else "",
                "day": day.isoformat(),
                "tomorrow": is_tomorrow,
                "rendered_at": time.time(),
            }
            # Keep a few recent renders so /frame.png?h= can always serve the
            # exact bytes a /frame.json answer described, even across the hour.
            self._cache[key] = entry
            while len(self._cache) > 6:
                self._cache.pop(next(iter(self._cache)))
            return entry

    def conditions(self, day, is_tomorrow, now):
        try:  # badges are optional; never let them take the poster down
            return self._conditions(day, is_tomorrow, None if is_tomorrow else now.hour)
        except Exception as e:
            print(f"conditions failed: {e!r}", flush=True)
            return None

    def alert(self, now):
        """The current alert id: cheap, cache-only (what the frame polls)."""
        day, is_tomorrow = schedule.poster_day(now, self.cfg.tomorrow_from_hour)
        cond = self.conditions(day, is_tomorrow, now)
        return cond.alert if cond else ""

    def by_hash(self, digest):
        # No lock: a render in progress can hold it for a while, and list() of
        # a dict's values is atomic under the GIL.
        for entry in list(self._cache.values()):
            if entry["hash"] == digest:
                return entry
        return None

    def ota_manifest(self):
        """The signed over-the-air release (see scripts/sign_ota.py), if any."""
        try:
            m = json.loads((self.ota_dir / "manifest.json").read_text())
            return {k: m[k] for k in ("version", "sha256", "size", "sig", "path")}
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def frame_info(self, now):
        p = self.poster(now)
        info = {
            "hash": p["hash"],
            "size": len(p["png"]),
            "png": f"/frame.png?h={p['hash']}",
            "sleep_minutes": schedule.sleep_minutes(now, self.cfg.first_hour, self.cfg.last_hour),
            "day": p["day"],
            "tomorrow": p["tomorrow"],
            "photo": p["photo"],
            "alert": p["alert"],
            "alert_poll_minutes": ALERT_POLL_MINUTES,
            "server_time": now.isoformat(timespec="seconds"),
        }
        code = self.ota_manifest()
        if code:
            info["code"] = code
        return info


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        server_version = "inky-almanac/1.0"

        def _send(self, status, body, ctype, extra=None):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, obj, status=HTTPStatus.OK):
            self._send(status, json.dumps(obj).encode(), "application/json")

        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            url = urlparse(self.path)
            at = parse_qs(url.query).get("at", [None])[0]
            try:
                now = app.now(at)
            except ValueError:
                return self._json({"error": "bad ?at="}, HTTPStatus.BAD_REQUEST)
            try:
                if url.path == "/frame.json":
                    if not at:
                        app.saw_frame(self.client_address[0])
                    return self._json(app.frame_info(now))
                if url.path == "/frame.png":
                    want = parse_qs(url.query).get("h", [None])[0]
                    if want:
                        p = app.by_hash(want)
                        if p is None:  # gone (shuffled or evicted): the frame re-reads /frame.json
                            return self._json({"error": "unknown hash"}, HTTPStatus.NOT_FOUND)
                    else:
                        p = app.poster(now)
                    return self._send(HTTPStatus.OK, p["png"], "image/png", {"ETag": p["hash"]})
                if url.path == "/preview.png":
                    buf = io.BytesIO()
                    to_preview(app.poster(now)["canvas"]).save(buf, "PNG")
                    return self._send(HTTPStatus.OK, buf.getvalue(), "image/png")
                if url.path == "/ota/almanac_frame.py":
                    try:
                        body = (app.ota_dir / "almanac_frame.py").read_bytes()
                    except OSError:
                        return self._json({"error": "no release"}, HTTPStatus.NOT_FOUND)
                    return self._send(HTTPStatus.OK, body, "text/x-python")
                if url.path == "/alert.json":
                    return self._json({"id": app.alert(now)})
                if url.path == "/healthz":
                    return self._send(HTTPStatus.OK, b"ok\n", "text/plain")
                if url.path == "/":
                    info = app.frame_info(now)
                    info["conditions"] = app.poster(now)["conditions"]
                    if app.last_seen:
                        seen = datetime.fromtimestamp(app.last_seen[0], app.tz)
                        info["frame_last_seen"] = f"{seen:%a %H:%M} from {app.last_seen[1]}"
                    else:
                        info["frame_last_seen"] = "not since the server started"
                    return self._send(HTTPStatus.OK, _index(info).encode(), "text/html; charset=utf-8")
            except Exception as e:  # never let one bad render kill the server
                self.log_error("render failed: %r", e)
                return self._json({"error": "render failed"}, HTTPStatus.INTERNAL_SERVER_ERROR)
            self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)

        def do_POST(self):
            if urlparse(self.path).path == "/shuffle":
                origin = self.headers.get("Origin")
                if origin and urlparse(origin).netloc != self.headers.get("Host"):
                    return self._json({"error": "cross-origin"}, HTTPStatus.FORBIDDEN)
                try:
                    n = app.shuffle(app.now())
                except Exception as e:
                    self.log_error("shuffle failed: %r", e)
                    return self._json({"error": "shuffle failed"}, HTTPStatus.INTERNAL_SERVER_ERROR)
                if "text/html" in self.headers.get("Accept", ""):
                    return self._send(HTTPStatus.SEE_OTHER, b"", "text/plain", {"Location": "/"})
                return self._json({"shuffle": n})
            self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    return Handler


def _index(info):
    rows = "".join(f"<tr><th>{escape(str(k))}</th><td>{escape(str(v))}</td></tr>" for k, v in info.items())
    return f"""<!doctype html><meta charset=utf-8><title>Inky Almanac</title>
<style>body{{font:15px system-ui;margin:2em;max-width:820px}}img{{width:800px;border:1px solid #888}}
th{{text-align:left;padding-right:1em}}</style>
<h1>Inky Almanac</h1><img src="/preview.png" alt="poster preview">
<table>{rows}</table>
<form method=post action=/shuffle><button>Different photo</button></form>
<p>The frame picks up a change at its next wake (or press A on the frame).</p>"""


def main():
    cfg = Config()
    app = Almanac(cfg)
    if cfg.usb_device:
        from .usbpush import UsbPusher

        pusher = UsbPusher(app, cfg.usb_device)
        app.on_change.append(pusher.wake.set)
        pusher.start()
        print(f"usb mode: driving the frame on {cfg.usb_device}", flush=True)
    def refresh_feeds():
        """Fetch every feed into its disk cache (the only network path)."""
        now = app.now()
        day, tomorrow = schedule.poster_day(now, cfg.tomorrow_from_hour)
        for name, job in (
            ("weather", lambda: weather.load(cfg)),
            ("bom", lambda: conditions.load_bom_days(cfg)),
            ("conditions", lambda: conditions.load(cfg, day, tomorrow)),
            ("photo", lambda: photos.photo_for(cfg, day, app.shuffle_count(day))),
        ):
            try:
                job()
            except Exception as e:
                print(f"feed refresh ({name}) failed: {e!r}", flush=True)

    def keep_feeds_fresh():
        while True:
            time.sleep(5 * 60)
            refresh_feeds()

    def keep_warnings_fresh():
        """Hazards and warning maps every ALERT_REFRESH_S: the frame checks
        /alert.json every couple of minutes, so this sets how fast a new
        warning reaches the wall."""
        while True:
            time.sleep(ALERT_REFRESH_S)
            now = app.now()
            day, tomorrow = schedule.poster_day(now, cfg.tomorrow_from_hour)
            try:
                conditions.load(cfg, day, tomorrow)
            except Exception as e:
                print(f"warning refresh failed: {e!r}", flush=True)

    threading.Thread(target=keep_warnings_fresh, name="warnings", daemon=True).start()

    refresh_feeds()  # warm the caches before the first request
    threading.Thread(target=keep_feeds_fresh, name="feeds", daemon=True).start()

    def keep_pool_fresh():  # the fortnightly Commons crawl takes minutes; never in a request
        while True:
            try:
                photos.load_pool(cfg, refresh=True)
            except Exception as e:
                print(f"photo pool refresh failed: {e!r}", flush=True)
            time.sleep(6 * 3600)

    threading.Thread(target=keep_pool_fresh, name="pool", daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", cfg.port), make_handler(app))
    print(f"inky-almanac listening on :{cfg.port} for {cfg.place}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
