# Inky Almanac: frame side (library; main.py picks the mode).
#
# WiFi mode: wake, ask the renderer whether the poster changed, and if so
# download it (to the SD card when there is one) and draw it. Then sleep for as
# long as the server says. On battery the board powers off and the RTC wakes
# it; on USB it stays up and polls the buttons while it waits.
#
#   A  refresh now (redraws even if nothing changed)
#   B  different photo for today
#   E  held while powering on: skip the app (safe mode, for mpremote/Thonny)
#
# USB mode: the board does nothing on its own. The renderer, on the machine
# the board is plugged into, copies each new poster over the serial link and
# calls usb_show(). Used while the frame's WiFi can't reach the renderer.
#
# Needs: WIFI_CONFIG.py (SSID, PSK, COUNTRY) and config.py (MODE, SERVER).

import gc
import json
import os
import time

import machine
import inky_frame

import config

STATE_FILE = "/state.json"
OTA_FILE = "/ota.json"  # shared with main.py: {"trial": sha, "boots": n, "bad": [sha, ...]}
CODE_FILE = "almanac_frame.py"
OTA_DOMAIN = b"inky-almanac-ota-v1"
LOG_MAX = 24 * 1024
RETRY_MINUTES = (5, 10, 20, 30)
CYCLE_DEADLINE_MS = 5 * 60 * 1000  # a cycle normally takes ~45 s
ERROR_SCREEN_AFTER_S = 6 * 3600

graphics = None
decoder = None
sd_ok = False
BOOT_MS = None  # ticks_ms at boot, for uptime
FRESH_HEAP_MS = 10 * 60 * 1000  # a MemoryError this soon after boot isn't fragmentation
MAX_UPTIME_MS = 24 * 3600 * 1000  # on USB, start each day on a clean heap


# ------------------------------------------------------------------ storage


def mount_sd():
    global sd_ok
    try:
        import sdcard

        spi = machine.SPI(
            0,
            sck=machine.Pin(18, machine.Pin.OUT),
            mosi=machine.Pin(19, machine.Pin.OUT),
            miso=machine.Pin(16, machine.Pin.OUT),
        )
        os.mount(sdcard.SDCard(spi, machine.Pin(22)), "/sd")
        sd_ok = True
    except Exception as e:
        print("no SD card:", e)
        sd_ok = False


def data_dir():
    return "/sd" if sd_ok else ""


def log(*parts):
    t = time.localtime()
    line = "%04d-%02d-%02d %02d:%02d:%02dZ %s" % (t[0], t[1], t[2], t[3], t[4], t[5], " ".join(str(p) for p in parts))
    print(line)
    path = data_dir() + "/almanac.log"
    try:
        try:
            if os.stat(path)[6] > LOG_MAX:
                try:
                    os.remove(path + ".1")  # FAT won't rename onto an existing file
                except OSError:
                    pass
                os.rename(path, path + ".1")
        except OSError:
            pass
        with open(path, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def load_state():
    try:
        with open(STATE_FILE) as f:
            s = json.load(f)
            if isinstance(s, dict):
                return s
    except (OSError, ValueError):
        pass
    return {}


def save_state(s):
    with open(STATE_FILE + ".tmp", "w") as f:
        json.dump(s, f)
    os.rename(STATE_FILE + ".tmp", STATE_FILE)


# ------------------------------------------------------------------ network


def connect_wifi():
    import network
    import rp2
    import WIFI_CONFIG

    rp2.country(getattr(WIFI_CONFIG, "COUNTRY", "AU"))
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    if wlan.isconnected():
        return wlan
    inky_frame.led_wifi.on()
    status = None
    # Two tries: straight after a reset the radio sometimes sits in
    # "connecting" (1) or "link fail" (-1) for the whole first 30 s (seen on
    # 2026-11-10, 2 of 4 boots). Power-cycling the interface and trying again
    # joins at once, instead of losing the cycle to a retry up to 30 min away.
    for attempt in range(2):
        if attempt:
            log("wifi: retrying after status", status)
            wlan.disconnect()
            wlan.active(False)
            time.sleep(1)
            wlan.active(True)
        wlan.connect(WIFI_CONFIG.SSID, WIFI_CONFIG.PSK)
        for _ in range(60):
            if wlan.isconnected():
                inky_frame.led_wifi.off()
                return wlan
            time.sleep(0.5)
        status = wlan.status()
    inky_frame.led_wifi.off()
    raise OSError("wifi: no connection (status %d)" % status)


def wifi_off():
    """Drop the link after every cycle. On USB the board stays up for hours,
    and a link left idle that long can look connected while it isn't, which
    can leave a DNS lookup blocked with no timeout (the "button A does
    nothing" bug). Each wake starts from a fresh association instead."""
    try:
        import network

        wlan = network.WLAN(network.STA_IF)
        wlan.disconnect()
        wlan.active(False)
    except Exception as e:
        log("wifi off failed:", repr(e))


def sync_clock(state):
    # Once a day is plenty; the RTC drifts seconds per week.
    if time.time() - state.get("ntp_at", 0) < 86400 and time.localtime()[0] >= 2025:
        return
    try:
        inky_frame.set_time()
        state["ntp_at"] = time.time()
        log("clock synced")
    except Exception as e:
        log("ntp failed:", e)


def get_json(path):
    import requests

    r = requests.get(config.SERVER + path, timeout=60)
    try:
        if r.status_code != 200:
            raise OSError("HTTP %d for %s" % (r.status_code, path))
        return r.json()
    finally:
        r.close()


def post(path):
    import requests

    r = requests.post(config.SERVER + path, timeout=60)
    r.close()


def download(path, dest, size):
    import requests

    r = requests.get(config.SERVER + path, timeout=60)
    got = 0
    try:
        if r.status_code != 200:
            raise OSError("HTTP %d for %s" % (r.status_code, path))
        buf = bytearray(1024)
        mv = memoryview(buf)
        with open(dest + ".tmp", "wb") as f:
            while True:
                n = r.raw.readinto(buf)
                if not n:
                    break
                got += n
                if got > size:  # a bad server must not be able to fill flash
                    raise OSError("download larger than the %d bytes promised" % size)
                f.write(mv[:n])
    finally:
        r.close()
    if got != size:
        raise OSError("short download: %d of %d bytes" % (got, size))
    try:
        os.remove(dest)
    except OSError:
        pass
    os.rename(dest + ".tmp", dest)


# ------------------------------------------------------------------ OTA


def _sha256_file(path):
    import hashlib
    import binascii

    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1024)
            if not chunk:
                break
            h.update(chunk)
    return binascii.hexlify(h.digest()).decode()


def _hmac_sha256(key, msg):
    import hashlib

    if len(key) > 64:
        key = hashlib.sha256(key).digest()
    key = key + b"\x00" * (64 - len(key))
    inner = hashlib.sha256(bytes([b ^ 0x36 for b in key]) + msg).digest()
    return hashlib.sha256(bytes([b ^ 0x5C for b in key]) + inner).digest()


def ota_message(name, version, sha, size):
    return b":".join([OTA_DOMAIN, name.encode(), str(version).encode(), sha.encode(), str(size).encode()])


def _same(a, b):
    """Constant-time string compare."""
    if len(a) != len(b):
        return False
    r = 0
    for x, y in zip(a, b):
        r |= ord(x) ^ ord(y)
    return r == 0


def _ota_key():
    """The signing key from OTA_KEY.py (never in the repo). None disables OTA."""
    try:
        import OTA_KEY
        import binascii

        return binascii.unhexlify(OTA_KEY.KEY)
    except Exception:
        return None


def load_ota():
    try:
        with open(OTA_FILE) as f:
            s = json.load(f)
            if isinstance(s, dict):
                return s
    except (OSError, ValueError):
        pass
    return {}


def save_ota(s):
    with open(OTA_FILE + ".tmp", "w") as f:
        json.dump(s, f)
    os.rename(OTA_FILE + ".tmp", OTA_FILE)


def ota_commit():
    """Called after a refresh that drew: a trial version has proved itself."""
    s = load_ota()
    if s.get("trial"):
        log("ota: committed", s["trial"][:12])
        if s.get("trial_version"):
            s["version"] = s["trial_version"]  # the baseline only moves on commit
        s["trial"] = None
        s["trial_version"] = None
        s["boots"] = 0
        save_ota(s)


def maybe_update(info, state):
    """Install a newer signed almanac_frame.py if the server offers one.

    Order matters: refuse anything not newer than what's installed (the
    signed version number stops replaying an old release); verify the
    signature over (name, version, sha, size) with the key only the Mac and
    this board hold; download (capped at size); check sha256; record the
    trial BEFORE touching files; keep the current file as .prev and swap with
    atomic renames; reset. main.py rolls back if the trial doesn't complete a
    refresh that draws within a few boots. Returns quietly (logging why) on
    anything unexpected."""
    code = info.get("code")
    if not code:
        return False
    key = _ota_key()
    if key is None:
        return False
    sha, size, sig, version = code.get("sha256"), code.get("size"), code.get("sig"), code.get("version")
    if not (sha and size and sig and isinstance(version, int)) or sha == _sha256_file(CODE_FILE):
        return False
    s = load_ota()
    if s.get("trial"):
        return False  # one unproven version at a time: .prev must stay known-good
    if sha in s.get("bad", []):
        return False
    if version <= int(s.get("version") or 0):
        return False  # not newer than the committed version: never go backwards
    import binascii

    want = binascii.hexlify(_hmac_sha256(key, ota_message(CODE_FILE, version, sha, size))).decode()
    if not _same(want, sig):
        log("ota: bad signature, ignoring", sha[:12])
        return False
    tmp = CODE_FILE + ".new"
    download(code.get("path", "/ota/almanac_frame.py"), tmp, int(size))
    if _sha256_file(tmp) != sha:
        log("ota: hash mismatch, ignoring", sha[:12])
        os.remove(tmp)
        return False
    s.update({"trial": sha, "trial_version": version, "boots": 0})
    save_ota(s)  # first: a power cut after this is recoverable by main.py
    state["force_next"] = True  # the trial must draw to be committed, whatever the wake reason
    save_state(state)
    os.rename(CODE_FILE, CODE_FILE + ".prev")  # replaces any older .prev atomically
    os.rename(tmp, CODE_FILE)
    log("ota: installed", sha[:12], "v%d - rebooting" % version)
    machine.reset()
    return True  # not reached on the board


# ------------------------------------------------------------------ display


def display():
    global graphics
    if graphics is None:
        from picographics import PicoGraphics, DISPLAY_INKY_FRAME_7

        graphics = PicoGraphics(DISPLAY_INKY_FRAME_7)
    return graphics


def png_decoder():
    """One PNG decoder for the life of the boot.

    pngdec.PNG() needs a single ~48 KB block. On USB power the board can run
    for days, and once the heap fragments that block stops being available
    ("memory allocation failed, allocating 48156 bytes", 12 Nov 2026).
    Allocating it once, at boot while the heap is clean, and reusing it
    removes the per-draw allocation."""
    global decoder
    if decoder is None:
        import pngdec

        gc.collect()
        decoder = pngdec.PNG(display())
    return decoder


def show_png(path):
    import pngdec

    g = display()
    png = png_decoder()
    png.open_file(path)
    png.decode(0, 0, mode=pngdec.PNG_COPY)  # indices are already panel inks
    g.update()


def uptime_ms():
    return time.ticks_diff(time.ticks_ms(), BOOT_MS) if BOOT_MS is not None else 0


def restart_fresh(state, why):
    """Reset to get a clean heap; the boot after it redraws."""
    log("restarting:", why)
    state["force_next"] = True
    try:
        save_state(state)
    except OSError:
        pass
    machine.reset()


def show_error(message, since):
    g = display()
    g.set_pen(inky_frame.WHITE)
    g.clear()
    g.set_font("bitmap8")
    g.set_pen(inky_frame.RED)
    g.text("Inky Almanac can't update", 40, 60, 720, 4)
    g.set_pen(inky_frame.BLACK)
    t = time.localtime(since)
    g.text("Failing since %02d:%02d UTC %d/%d" % (t[3], t[4], t[2], t[1]), 40, 140, 720, 3)
    g.text(message[:160], 40, 190, 720, 2)
    g.text("Server: " + config.SERVER, 40, 300, 720, 2)
    g.text("Still retrying. Press A to retry now.", 40, 400, 720, 2)
    inky_frame.led_busy.on()
    g.update()
    inky_frame.led_busy.off()


# ------------------------------------------------------------------ one cycle


def _watchdog():
    """Reset the board if a cycle hasn't finished in CYCLE_DEADLINE_MS.

    The hardware WDT tops out at 8.3 s, too short for a 40 s panel refresh,
    so this is a one-shot machine.Timer. A stalled network call (the "button
    A did nothing" report) then recovers by itself: the reset is a power-on,
    which redraws. Returns the timer, or None if timers aren't available."""
    try:
        t = machine.Timer()
        t.init(mode=machine.Timer.ONE_SHOT, period=CYCLE_DEADLINE_MS, callback=_deadline_hit)
        return t
    except Exception as e:
        log("watchdog unavailable:", repr(e))
        return None


def _deadline_hit(timer):
    log("cycle overran", CYCLE_DEADLINE_MS // 1000, "s; resetting")
    machine.reset()


def cycle(state, force=False, shuffle=False):
    """Returns minutes to sleep."""
    inky_frame.led_busy.on()  # instant feedback: the frame is working
    dog = _watchdog()
    try:
        log("fetch", "forced" if force else "", "shuffle" if shuffle else "")
        connect_wifi()
        sync_clock(state)
        if shuffle:
            post("/shuffle")
            force = True
        info = get_json("/frame.json")
        changed = info["hash"] != state.get("hash")
        drew = False
        if changed or force:
            dest = data_dir() + "/frame.png"
            gc.collect()
            download(info.get("png", "/frame.png"), dest, info["size"])
            log("draw", info["hash"])
            show_png(dest)
            state["hash"] = info["hash"]
            drew = True
            log("drew", info["hash"], info.get("photo"))
        else:
            log("unchanged", info["hash"])
        state.pop("fail_since", None)
        state.pop("fail_count", None)
        state.pop("error_shown", None)
        state["alert"] = info.get("alert", "")
        state["alert_poll"] = int(info.get("alert_poll_minutes") or 0)
        if drew:
            ota_commit()  # this version just completed a refresh that drew
        try:
            maybe_update(info, state)
        except Exception as e:  # an update problem must never cost a refresh
            log("ota: failed:", repr(e))
            try:
                os.remove(CODE_FILE + ".new")
            except OSError:
                pass
        # Never trust the server to keep us sane: 0 would spin, huge would
        # overflow the RTC alarm's day-of-month.
        return max(5, min(720, int(info.get("sleep_minutes", 60))))
    except MemoryError as e:
        # Heap fragmentation after a long uptime. A reset is the only cure;
        # but straight after boot that won't help, so fall through to the
        # normal failure path rather than reset-looping.
        if uptime_ms() > FRESH_HEAP_MS:
            restart_fresh(state, "out of memory (%r)" % (e,))
        return _failed(state, e)
    except Exception as e:
        return _failed(state, e)
    finally:
        if dog:
            dog.deinit()
        wifi_off()
        inky_frame.led_busy.off()


def _failed(state, e):
    """Count a failed cycle; returns minutes until the retry."""
    n = state.get("fail_count", 0)
    state["fail_count"] = n + 1
    if time.localtime()[0] < 2025:  # clock not set yet: don't start the 6 h timer
        state.pop("fail_since", None)
        since = None
    else:
        since = state.setdefault("fail_since", time.time())
    log("cycle failed (%d):" % (n + 1), repr(e))
    if since and time.time() - since > ERROR_SCREEN_AFTER_S and not state.get("error_shown"):
        try:
            show_error(repr(e), since)
            state["error_shown"] = True
            state["hash"] = None  # redraw the poster as soon as it's back
        except Exception as e2:
            log("error screen failed:", repr(e2))
    return RETRY_MINUTES[min(n, len(RETRY_MINUTES) - 1)]


ALERT_MIN_GAP_S = 10 * 60  # at most one alert-driven redraw per 10 min (the first is immediate)


def check_alert(state=None):
    """The renderer's current alert id, or None if it couldn't be reached.
    Guarded by the watchdog like a cycle: it runs hundreds of times a day."""
    dog = _watchdog()
    try:
        connect_wifi()
        return get_json("/alert.json").get("id", "")
    except MemoryError as e:
        if uptime_ms() > FRESH_HEAP_MS:
            restart_fresh(state if state is not None else {}, "out of memory checking alerts (%r)" % (e,))
        return None
    except Exception as e:
        log("alert check failed:", repr(e))
        return None
    finally:
        if dog:
            dog.deinit()
        wifi_off()


def alert_redraw_due(state, new):
    """A changed alert redraws at once when a warning first appears; further
    changes (reissues, moving storms, edge-of-radius flicker) at most every
    ALERT_MIN_GAP_S, so the panel can't flash itself to death."""
    if new is None or new == state.get("alert", ""):
        return False
    if not state.get("alert"):
        return True
    return time.time() - state.get("alert_drawn_at", 0) >= ALERT_MIN_GAP_S


def rest(minutes, state=None):
    """Sleep `minutes`. Returns "a"/"b" if a button cut it short, or "alert"
    if a new specific hazard warning appeared (USB power only: on battery the
    board is off until the RTC alarm)."""
    try:
        # Arm the RTC alarm and drop the power latch. On battery this is the
        # last thing that happens until the alarm or a button wakes the board.
        year, month, day, hour, minute, second, dow = inky_frame.rtc.datetime()
        if second >= 55:
            minute += 1
        t = time.mktime((year, month, day, hour, minute, second, dow, 0)) + minutes * 60
        a = time.localtime(t)
        inky_frame.rtc.clear_alarm_flag()
        inky_frame.rtc.set_alarm(0, a[4], a[3], a[2])
        inky_frame.rtc.enable_alarm_interrupt(True)
        inky_frame.turn_off()
    except Exception as e:
        log("alarm setup failed:", repr(e))
    # Still running: we're on USB power. Wait, watching the buttons. Our own
    # edge detection on raw(): Pimoroni's Button.read() debounces with plain
    # ticks subtraction, which stops seeing presses once ticks_ms wraps
    # (~12.4 days of uptime).
    deadline = time.ticks_add(time.ticks_ms(), minutes * 60000)
    was = {"a": True, "b": True}  # a press already held at entry doesn't count
    # No alert polling while cycles are failing: the retry backoff stands.
    poll = 0 if (state or {}).get("fail_count") else ((state or {}).get("alert_poll") or 0)
    next_poll = time.ticks_add(time.ticks_ms(), max(1, poll) * 60000)
    while time.ticks_diff(deadline, time.ticks_ms()) > 0:
        if poll and time.ticks_diff(time.ticks_ms(), next_poll) >= 0:
            new = check_alert(state)
            next_poll = time.ticks_add(time.ticks_ms(), poll * 60000)
            if alert_redraw_due(state, new):
                log("alert changed:", new or "(cleared)")
                state["alert_drawn_at"] = time.time()
                return "alert"
        for name, button in (("a", inky_frame.button_a), ("b", inky_frame.button_b)):
            down = button.raw()
            if down and not was[name]:
                return name
            was[name] = down
        time.sleep_ms(50)
    return None


def main():
    global BOOT_MS
    BOOT_MS = time.ticks_ms()
    inky_frame.pcf_to_pico_rtc()  # the external RTC keeps time across power-offs
    mount_sd()
    state = load_state()
    rtc_wake = inky_frame.woken_by_rtc()
    # A button press that woke the board (battery) counts as a press.
    press = "a" if inky_frame.button_a.read() else "b" if inky_frame.button_b.read() else None
    # Plugged in or powered on (not a scheduled RTC wake): redraw even if the
    # poster is unchanged, so a power cycle visibly does something.
    force = press == "a" or (press is None and not rtc_wake) or bool(state.pop("force_next", False))
    log("boot", "sd" if sd_ok else "no-sd", "rtc-wake" if rtc_wake else "power-on", press or "")
    try:
        png_decoder()  # claim the decoder's big block while the heap is clean
    except Exception as e:
        log("decoder preallocation failed:", repr(e))
    while True:
        if press is None and uptime_ms() > MAX_UPTIME_MS:
            restart_fresh(state, "daily fresh start after %d h" % (uptime_ms() // 3600000))
        if press:
            log("button", press)
            inky_frame.button_a.led_on() if press == "a" else inky_frame.button_b.led_on()
        minutes = cycle(state, force=force, shuffle=press == "b")
        inky_frame.button_a.led_off()
        inky_frame.button_b.led_off()
        try:
            save_state(state)
        except OSError as e:
            log("state save failed:", e)
        log("sleep", minutes, "min")
        press = rest(minutes, state)
        if press == "alert":
            press = None  # a normal cycle: the poster changed, so it redraws
        force = press == "a"


def usb_show(path, digest):
    """Called by the renderer over the serial link (USB mode)."""
    show_png(path)
    state = load_state()
    state["hash"] = digest
    save_state(state)
    print("shown", digest)


def run():
    if inky_frame.button_e.raw():
        print("safe mode: button E held, app not started")
        return
    try:
        main()
    except Exception as e:  # last resort: log, wait, start over
        try:
            log("fatal:", repr(e))
        except Exception:
            pass
        time.sleep(1800)  # don't hammer anything if this keeps happening
        machine.reset()
