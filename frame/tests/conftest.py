"""Run almanac_frame under CPython with the Pico's modules stubbed out.

Only the control flow is tested here (boot, buttons, retries, WiFi hygiene);
drawing and networking are verified on the real board.
"""

import sys
import time
import types
from pathlib import Path

import pytest

FRAME = Path(__file__).resolve().parents[1]


class Led:
    def __init__(self):
        self.lit = False

    def on(self):
        self.lit = True

    def off(self):
        self.lit = False

    led_on, led_off = on, off


class Button(Led):
    def __init__(self):
        super().__init__()
        self.pressed = False

    def read(self):
        p, self.pressed = self.pressed, False
        return p

    def raw(self):
        return self.pressed

    @property
    def led(self):
        return self


class WLAN:
    instances = []

    def __init__(self, *a):
        self.connected = False
        self.is_active = False
        self.events = []
        WLAN.instances.append(self)

    def active(self, on=None):
        if on is not None:
            self.is_active = on
            self.events.append(("active", on))
            if not on:
                self.connected = False
        return self.is_active

    def isconnected(self):
        return self.connected

    def connect(self, ssid, psk):
        self.events.append(("connect",))
        self.connected = True

    def disconnect(self):
        self.events.append(("disconnect",))
        self.connected = False

    def status(self):
        return 3


@pytest.fixture
def frame(monkeypatch, tmp_path):
    WLAN.instances.clear()
    inky = types.SimpleNamespace(
        led_busy=Led(), led_wifi=Led(),
        button_a=Button(), button_b=Button(), button_c=Button(), button_e=Button(),
        rtc_wake=False, WHITE=1, BLACK=0, RED=4,
    )
    inky.woken_by_rtc = lambda: inky.rtc_wake
    inky.pcf_to_pico_rtc = lambda: True
    inky.set_time = lambda: None
    inky.turn_off = lambda: None
    class Timer:
        ONE_SHOT = 0
        made = []

        def __init__(self, *a):
            self.armed = False
            Timer.made.append(self)

        def init(self, mode=None, period=None, callback=None):
            self.armed, self.period, self.callback = True, period, callback

        def deinit(self):
            self.armed = False

    resets = []
    machine = types.SimpleNamespace(reset=lambda: resets.append(1), Pin=lambda *a, **k: None, SPI=lambda *a, **k: None, Timer=Timer)
    network = types.SimpleNamespace(STA_IF=0, WLAN=lambda *a: (WLAN.instances[0] if WLAN.instances else WLAN()))
    stubs = {
        "inky_frame": inky,
        "machine": machine,
        "network": network,
        "rp2": types.SimpleNamespace(country=lambda c: None),
        "WIFI_CONFIG": types.SimpleNamespace(SSID="s", PSK="p", COUNTRY="AU"),
        "config": types.SimpleNamespace(MODE="wifi", SERVER="http://server"),
    }
    for name, mod in stubs.items():
        monkeypatch.setitem(sys.modules, name, mod)
    monkeypatch.syspath_prepend(str(FRAME))
    sys.modules.pop("almanac_frame", None)
    import almanac_frame as a

    a.STATE_FILE = str(tmp_path / "state.json")
    a.MIGRATE_TO = None  # off unless a test turns it on (test_migrate.py)
    a.sd_ok = False
    monkeypatch.setattr(a, "mount_sd", lambda: None)
    monkeypatch.setattr(a, "data_dir", lambda: str(tmp_path))
    a.logged = []
    monkeypatch.setattr(a, "log", lambda *p: a.logged.append(" ".join(str(x) for x in p)))
    # MicroPython's time has ticks_*; CPython's doesn't.
    monkeypatch.setattr(time, "ticks_ms", lambda: int(time.monotonic() * 1000), raising=False)
    monkeypatch.setattr(time, "ticks_add", lambda t, d: t + d, raising=False)
    monkeypatch.setattr(time, "ticks_diff", lambda a_, b: a_ - b, raising=False)
    monkeypatch.setattr(time, "sleep_ms", lambda ms: None, raising=False)
    a.inky = inky
    a.timers = Timer.made
    a.resets = resets
    Timer.made.clear()
    yield a
    sys.modules.pop("almanac_frame", None)
