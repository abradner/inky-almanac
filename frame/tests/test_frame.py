import json

import pytest


class Stop(Exception):
    pass


def run_main(a, monkeypatch, presses=(), cycles=2):
    """Run main() with cycle() recorded and rest() returning `presses` in turn."""
    calls = []
    presses = list(presses)

    def fake_cycle(state, force=False, shuffle=False):
        calls.append({"force": force, "shuffle": shuffle, "busy": a.inky.led_busy.lit})
        return 60

    def fake_rest(minutes):
        if len(calls) >= cycles:
            raise Stop
        return presses.pop(0) if presses else None

    monkeypatch.setattr(a, "cycle", fake_cycle)
    monkeypatch.setattr(a, "rest", fake_rest)
    with pytest.raises(Stop):
        a.main()
    return calls


def test_power_on_forces_a_redraw_even_if_unchanged(frame, monkeypatch):
    frame.inky.rtc_wake = False
    calls = run_main(frame, monkeypatch, cycles=1)
    assert calls[0]["force"] is True
    assert any("power-on" in line for line in frame.logged)


def test_scheduled_rtc_wake_does_not_force(frame, monkeypatch):
    frame.inky.rtc_wake = True
    calls = run_main(frame, monkeypatch, cycles=1)
    assert calls[0]["force"] is False


def test_button_a_while_waiting_forces_and_is_logged_first(frame, monkeypatch):
    frame.inky.rtc_wake = True
    calls = run_main(frame, monkeypatch, presses=["a"], cycles=2)
    assert [c["force"] for c in calls] == [False, True]
    assert "button a" in frame.logged


def test_button_b_shuffles(frame, monkeypatch):
    frame.inky.rtc_wake = True
    calls = run_main(frame, monkeypatch, presses=["b"], cycles=2)
    assert calls[1]["shuffle"] is True and calls[1]["force"] is False


def test_a_press_that_woke_the_board_counts(frame, monkeypatch):
    frame.inky.rtc_wake = False
    frame.inky.button_a.pressed = True
    calls = run_main(frame, monkeypatch, cycles=1)
    assert calls[0]["force"] is True and "button a" in frame.logged


def test_only_the_first_cycle_after_power_on_is_forced(frame, monkeypatch):
    frame.inky.rtc_wake = False
    calls = run_main(frame, monkeypatch, presses=[None], cycles=2)
    assert [c["force"] for c in calls] == [True, False]


# ---- cycle(): WiFi hygiene and feedback ---------------------------------


def stub_network(a, monkeypatch, info=None, fail=None):
    def get_json(path):
        if fail:
            raise fail
        return info

    monkeypatch.setattr(a, "get_json", get_json)
    monkeypatch.setattr(a, "download", lambda path, dest, size: None)
    shown = []
    monkeypatch.setattr(a, "show_png", lambda path: shown.append(path))
    monkeypatch.setattr(a, "sync_clock", lambda state: None)
    return shown


def test_cycle_drops_wifi_afterwards(frame, monkeypatch):
    stub_network(frame, monkeypatch, info={"hash": "h1", "size": 10, "sleep_minutes": 60})
    frame.cycle({})
    wlan = __import__("network").WLAN()
    assert ("disconnect",) in wlan.events and wlan.events[-1] == ("active", False)
    assert not frame.inky.led_busy.lit


def test_cycle_drops_wifi_even_when_the_fetch_fails(frame, monkeypatch):
    stub_network(frame, monkeypatch, fail=OSError(110))
    minutes = frame.cycle({})
    assert minutes == frame.RETRY_MINUTES[0]
    wlan = __import__("network").WLAN()
    assert wlan.events[-1] == ("active", False)
    assert not frame.inky.led_busy.lit


def test_unchanged_skips_the_draw_but_forced_draws(frame, monkeypatch):
    shown = stub_network(frame, monkeypatch, info={"hash": "h1", "size": 10, "sleep_minutes": 60})
    state = {"hash": "h1"}
    frame.cycle(state)
    assert shown == []
    frame.cycle(state, force=True)
    assert len(shown) == 1


def test_cycle_clamps_the_servers_sleep(frame, monkeypatch):
    stub_network(frame, monkeypatch, info={"hash": "h", "size": 1, "sleep_minutes": 0})
    assert frame.cycle({}) == 5
    stub_network(frame, monkeypatch, info={"hash": "h", "size": 1, "sleep_minutes": 99999})
    assert frame.cycle({"hash": "h"}) == 720


def test_busy_led_is_on_while_working(frame, monkeypatch):
    seen = []
    stub_network(frame, monkeypatch, info={"hash": "h2", "size": 1, "sleep_minutes": 60})
    monkeypatch.setattr(frame, "show_png", lambda p: seen.append(frame.inky.led_busy.lit))
    frame.cycle({})
    assert seen == [True]


# ---- review fixes: watchdog and button edges --------------------------------


def test_cycle_arms_a_watchdog_and_disarms_it(frame, monkeypatch):
    stub_network(frame, monkeypatch, info={"hash": "h", "size": 1, "sleep_minutes": 60})
    frame.cycle({})
    assert len(frame.timers) == 1 and frame.timers[0].period == frame.CYCLE_DEADLINE_MS
    assert frame.timers[0].armed is False  # disarmed once the cycle finished
    frame.timers[0].callback(None)  # what happens if it ever fires
    assert frame.resets == [1]


def test_watchdog_is_disarmed_even_when_the_cycle_fails(frame, monkeypatch):
    stub_network(frame, monkeypatch, fail=OSError(110))
    frame.cycle({})
    assert frame.timers[0].armed is False


def _press_sequence(frame, monkeypatch, samples):
    """Feed button A raw() values one per poll; returns rest()'s result."""
    seq = list(samples)
    monkeypatch.setattr(frame.inky.button_a, "raw", lambda: seq.pop(0) if seq else False)
    monkeypatch.setattr(frame.inky.button_b, "raw", lambda: False)
    return frame.rest(1)


def test_rest_sees_a_press_as_a_rising_edge(frame, monkeypatch):
    assert _press_sequence(frame, monkeypatch, [False, False, True]) == "a"


def test_rest_ignores_a_button_already_held_at_entry(frame, monkeypatch):
    clock = [0]
    monkeypatch.setattr(__import__("time"), "ticks_ms", lambda: clock.__setitem__(0, clock[0] + 20000) or clock[0])
    # held throughout: no edge, so rest() waits out its minute
    assert _press_sequence(frame, monkeypatch, [True] * 10) is None


def test_rest_never_uses_pimoroni_button_read(frame, monkeypatch):
    # Button.read() debounces with plain ticks subtraction and goes deaf once
    # ticks_ms wraps (~12.4 days of USB uptime); rest() must use raw() edges.
    def deaf():
        raise AssertionError("rest() called Button.read()")

    monkeypatch.setattr(frame.inky.button_a, "read", deaf)
    monkeypatch.setattr(frame.inky.button_b, "read", deaf)
    assert _press_sequence(frame, monkeypatch, [False, True]) == "a"


# ---- WiFi join retry -----------------------------------------------------------


def test_wifi_join_retries_once_after_power_cycling_the_radio(frame, monkeypatch):
    import network

    wlan = network.WLAN()
    joins = []

    def connect(ssid, psk):
        joins.append(1)
        wlan.events.append(("connect",))
        wlan.connected = len(joins) >= 2  # the first attempt never joins

    monkeypatch.setattr(wlan, "connect", connect)
    monkeypatch.setattr(__import__("time"), "sleep", lambda s: None)
    frame.connect_wifi()
    assert len(joins) == 2
    assert ("active", False) in wlan.events  # radio was power-cycled between tries
    assert any("retrying" in line for line in frame.logged)


def test_wifi_gives_up_after_two_tries(frame, monkeypatch):
    import network

    wlan = network.WLAN()
    joins = []

    def connect(ssid, psk):
        joins.append(1)

    monkeypatch.setattr(wlan, "connect", connect)
    monkeypatch.setattr(__import__("time"), "sleep", lambda s: None)
    with pytest.raises(OSError):
        frame.connect_wifi()
    assert len(joins) == 2
