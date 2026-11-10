# Inky Almanac boot guard. Deliberately tiny and never updated over the air:
# it is what recovers the board when an over-the-air update goes wrong, so it
# must never fall to the REPL on its own (that needs someone with a cable).
#
# almanac_frame.py can replace itself (maybe_update there). A new version
# starts "on trial" in /ota.json, recorded BEFORE the files are swapped. The
# trial ends when the new version completes a refresh that drew (ota_commit).
# This guard:
#   - restores almanac_frame.py from .prev if a power cut left it missing;
#   - counts trial boots, and arms a timer that resets the board if a trial
#     isn't committed within TRIAL_DEADLINE_MS (USB power never reboots by
#     itself, so a failing trial would otherwise never be counted);
#   - restores .prev if the trial won't import, or hasn't committed within
#     MAX_TRIAL_BOOTS boots. A version is marked bad (never retried) when it
#     won't import, or after MAX_ATTEMPTS failed trials; a single failed
#     trial may just have met a network outage, so it may be retried.
# Hold button E while powering on to skip everything (safe mode).

import json
import os

OTA_FILE = "/ota.json"
CODE = "almanac_frame.py"
PREV = CODE + ".prev"
MAX_TRIAL_BOOTS = 3
MAX_ATTEMPTS = 2
TRIAL_DEADLINE_MS = 30 * 60 * 1000
_trial_timer = None  # must stay referenced: rp2 deinits a collected Timer


def _exists(path):
    try:
        os.stat(path)
        return True
    except OSError:
        return False


def _load():
    try:
        with open(OTA_FILE) as f:
            s = json.load(f)
            return s if isinstance(s, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(s):
    with open(OTA_FILE + ".tmp", "w") as f:
        json.dump(s, f)
    os.rename(OTA_FILE + ".tmp", OTA_FILE)


def _sha(path):
    import binascii
    import hashlib

    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1024)
            if not chunk:
                break
            h.update(chunk)
    return binascii.hexlify(h.digest()).decode()


def _as_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def restore_prev(s, why, mark_bad):
    """Put .prev back as the app. rename() replaces atomically on LittleFS,
    so there is no moment without an almanac_frame.py."""
    print("ota: restoring previous version:", why)
    trial = s.get("trial")
    attempts = s.get("attempts") if isinstance(s.get("attempts"), dict) else {}
    bad = s.get("bad") if isinstance(s.get("bad"), list) else []
    if trial:
        attempts[trial] = _as_int(attempts.get(trial)) + 1
        if (mark_bad or attempts[trial] >= MAX_ATTEMPTS) and trial not in bad:
            bad.append(trial)
    os.rename(PREV, CODE)
    # "version" (the committed one) stays; only the trial's is dropped, so
    # the same release may be retried unless it is now marked bad.
    s.update({"trial": None, "trial_version": None, "boots": 0, "bad": bad[-20:], "attempts": attempts,
              "rolled_back": str(why)[:120]})
    _save(s)


def guard(reset, importer, arm_timer=None):
    """Recover if needed, then import and run the app. Never returns to the
    REPL by itself: the caller resets the board if this raises."""
    s = _load()
    if not _exists(CODE) and _exists(PREV):
        # A power cut mid-swap: the new file never landed. Not the new
        # version's fault, so don't count it against it.
        s["trial"] = None
        restore_prev(s, "app file missing after a power cut", mark_bad=False)
    trial = s.get("trial")
    if trial and _exists(CODE) and _sha(CODE) != trial:
        # The trial was recorded but power went before the swap: the old
        # version is still installed, so there is nothing on trial.
        s["trial"] = None
        s["trial_version"] = None
        _save(s)
        trial = None
    if trial:
        s["boots"] = _as_int(s.get("boots")) + 1
        _save(s)
        if s["boots"] > MAX_TRIAL_BOOTS and _exists(PREV):
            restore_prev(s, "no successful refresh in %d boots" % MAX_TRIAL_BOOTS, mark_bad=False)
            return reset()
        if arm_timer:
            arm_timer(TRIAL_DEADLINE_MS)
    try:
        app = importer()
    except Exception as e:
        if trial and _exists(PREV):
            restore_prev(s, "import failed: %r" % (e,), mark_bad=True)
            return reset()
        raise
    app.run()


def _arm(ms, machine=None):
    if machine is None:
        import machine

    def fire(t):
        if not _load().get("trial"):
            return  # committed in the meantime: stand down
        print("ota: trial not committed in time; resetting")
        machine.reset()

    global _trial_timer
    try:
        _trial_timer = machine.Timer()
        _trial_timer.init(mode=machine.Timer.ONE_SHOT, period=ms, callback=fire)
    except Exception as e:
        print("ota: no trial timer:", e)


def _safe_mode():
    try:
        import inky_frame

        return inky_frame.button_e.raw()
    except Exception:
        return False


if __name__ == "__main__":
    import config

    if _safe_mode():
        print("safe mode: button E held, app not started")
    elif getattr(config, "MODE", "wifi") != "wifi":
        print("Inky Almanac: USB mode; the renderer drives the display over serial.")
    else:
        import machine
        import time

        try:
            guard(machine.reset, lambda: __import__("almanac_frame"), _arm)
        except Exception as e:  # never strand the board at the REPL
            print("guard failed:", repr(e), "- resetting in 60 s (Ctrl-C for the REPL)")
            time.sleep(60)
            machine.reset()
