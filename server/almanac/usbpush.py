"""USB mode: drive a frame plugged into this machine over its serial link.

Used while the frame's WiFi can't reach the renderer. On the frame's own
schedule (the same one /frame.json hands out) it renders the poster, and when
the picture changed, copies it to the board with mpremote and calls
almanac_frame.usb_show() there.
"""

import subprocess
import threading

from . import schedule

PUSH_TIMEOUT = 240  # copy (~10 s) + a full panel refresh (~40 s), with room


def push_command(mpremote, device, local_png, digest):
    show = f"import almanac_frame as a; a.usb_show('/frame.png', '{digest}')"
    # "+" ends cp's variadic argument list so exec is read as a new command.
    return [mpremote, "connect", device, "cp", str(local_png), ":frame.png", "+", "exec", show]


class UsbPusher:
    def __init__(self, app, device, mpremote="mpremote", run=subprocess.run):
        self.app = app
        self.device = device
        self.mpremote = mpremote
        self._run = run
        self._last_path = app.cfg.cache_dir / "usb-last-hash"
        self._png_path = app.cfg.cache_dir / "usb-frame.png"
        self.wake = threading.Event()

    def last_hash(self):
        try:
            return self._last_path.read_text().strip()
        except OSError:
            return None

    def push_once(self, now, force=False):
        """Push if the poster changed. Returns minutes until the next check."""
        entry = self.app.poster(now)  # hash and bytes from the same render
        info = {"hash": entry["hash"], "photo": entry["photo"]}
        if force or info["hash"] != self.last_hash():
            self._png_path.write_bytes(entry["png"])
            cmd = push_command(self.mpremote, self.device, self._png_path, info["hash"])
            r = self._run(cmd, capture_output=True, text=True, timeout=PUSH_TIMEOUT)
            out = (r.stdout or "") + (r.stderr or "")
            if r.returncode != 0 or f"shown {info['hash']}" not in out:
                raise RuntimeError(f"push failed (exit {r.returncode}): {out.strip()[-300:]}")
            self._last_path.write_text(info["hash"])
            print(f"usb: drew {info['hash']} {info.get('photo')}", flush=True)
        return schedule.sleep_minutes(now, self.app.cfg.first_hour, self.app.cfg.last_hour)

    def loop(self):
        while True:
            try:
                minutes = self.push_once(self.app.now())
            except Exception as e:
                print(f"usb: {e}", flush=True)
                minutes = 5
            self.wake.wait(minutes * 60)
            self.wake.clear()

    def start(self):
        t = threading.Thread(target=self.loop, name="usb-push", daemon=True)
        t.start()
        return t
