"""Radar mode on the frame: button C, the mode round trip with the server,
and the server's min_gap_s for alert redraws (normal mode unchanged)."""

import pytest

from test_frame import run_main, stub_network


# ---- button C ---------------------------------------------------------------


def test_rest_returns_c_on_a_c_press(frame, monkeypatch):
    monkeypatch.setattr(frame.inky.button_a, "raw", lambda: False)
    monkeypatch.setattr(frame.inky.button_b, "raw", lambda: False)
    seq = [False, False, True]
    monkeypatch.setattr(frame.inky.button_c, "raw", lambda: seq.pop(0) if seq else False)
    assert frame.rest(1) == "c"


def test_rest_ignores_c_already_held_at_entry(frame, monkeypatch):
    clock = [0]
    monkeypatch.setattr(__import__("time"), "ticks_ms", lambda: clock.__setitem__(0, clock[0] + 20000) or clock[0])
    monkeypatch.setattr(frame.inky.button_c, "raw", lambda: True)
    assert frame.rest(1) is None


def test_c_while_waiting_asks_for_radar_and_forces_a_redraw(frame, monkeypatch):
    frame.inky.rtc_wake = True
    calls = run_main(frame, monkeypatch, presses=["c"], cycles=2)
    assert calls[1]["mode"] == "radar" and calls[1]["force"] is True and calls[1]["shuffle"] is False
    assert calls[0]["mode"] is None  # the scheduled wake before it was a normal cycle
    assert "button c" in frame.logged


def test_c_in_radar_mode_asks_for_normal(frame, monkeypatch):
    frame.inky.rtc_wake = True
    frame.save_state({"mode": "radar"})
    calls = run_main(frame, monkeypatch, presses=["c"], cycles=2)
    assert calls[1]["mode"] == "normal"


def test_a_c_press_that_woke_the_board_counts(frame, monkeypatch):
    # On battery a button press powers the board on; main() reads it at boot.
    frame.inky.rtc_wake = False
    frame.inky.button_c.pressed = True
    calls = run_main(frame, monkeypatch, cycles=1)
    assert calls[0]["mode"] == "radar" and calls[0]["force"] is True


def test_a_and_b_never_change_the_mode(frame, monkeypatch):
    frame.inky.rtc_wake = True
    calls = run_main(frame, monkeypatch, presses=["a", "b", None], cycles=4)
    assert [c["mode"] for c in calls] == [None, None, None, None]


# ---- cycle(): the mode round trip ---------------------------------------------


def _posts(frame, monkeypatch, fail=None):
    sent = []

    def post(path):
        sent.append(path)
        if fail:
            raise fail

    monkeypatch.setattr(frame, "post", post)
    return sent


def test_cycle_tells_the_server_then_draws_and_records_the_mode(frame, monkeypatch):
    shown = stub_network(frame, monkeypatch, info={"hash": "h1", "size": 1, "sleep_minutes": 60, "mode": "radar",
                                                    "alert": "radar:1"})
    sent = _posts(frame, monkeypatch)
    monkeypatch.setattr(__import__("time"), "time", lambda: 5000)
    state = {"hash": "h1"}  # unchanged poster: the C press still redraws
    frame.cycle(state, force=True, mode="radar")
    assert sent == ["/mode?set=radar"] and len(shown) == 1
    assert state["mode"] == "radar" and state["alert_mode"] == "radar"
    assert state["alert_drawn_at"] == 5000  # the next radar frame waits its gap from this draw


def test_a_mode_change_always_redraws_even_unforced(frame, monkeypatch):
    shown = stub_network(frame, monkeypatch, info={"hash": "h1", "size": 1, "sleep_minutes": 60, "mode": "radar"})
    _posts(frame, monkeypatch)
    frame.cycle({"hash": "h1"}, mode="radar")
    assert len(shown) == 1


def test_back_to_normal_round_trip(frame, monkeypatch):
    stub_network(frame, monkeypatch, info={"hash": "h2", "size": 1, "sleep_minutes": 60, "mode": "normal"})
    sent = _posts(frame, monkeypatch)
    state = {"mode": "radar"}
    frame.cycle(state, force=True, mode=frame.radar_toggle(state))
    assert sent == ["/mode?set=normal"] and state["mode"] == "normal"


def test_an_older_server_without_modes_still_redraws(frame, monkeypatch):
    # An older server 404s the POST (post() doesn't raise for that) and its
    # /frame.json has no "mode": the frame stays in normal mode and redraws.
    shown = stub_network(frame, monkeypatch, info={"hash": "h1", "size": 1, "sleep_minutes": 60})
    _posts(frame, monkeypatch)
    state = {"hash": "h1"}
    assert frame.cycle(state, force=True, mode="radar") == 60
    assert len(shown) == 1 and state["mode"] == "normal"


def test_a_failed_mode_post_still_redraws(frame, monkeypatch):
    shown = stub_network(frame, monkeypatch, info={"hash": "h1", "size": 1, "sleep_minutes": 60})
    _posts(frame, monkeypatch, fail=OSError("reset"))
    state = {"hash": "h1"}
    assert frame.cycle(state, force=True, mode="radar") == 60
    assert len(shown) == 1 and "fail_count" not in state


def test_a_normal_cycle_posts_nothing_and_leaves_alert_drawn_at(frame, monkeypatch):
    stub_network(frame, monkeypatch, info={"hash": "h3", "size": 1, "sleep_minutes": 60})
    sent = _posts(frame, monkeypatch)
    state = {"alert_drawn_at": 123}
    frame.cycle(state)
    assert sent == [] and state["alert_drawn_at"] == 123 and state["mode"] == "normal"


# ---- min_gap_s ----------------------------------------------------------------


@pytest.mark.parametrize("value, gap", [
    (300, 300), (240, 240), (60, 240), (0, 240), (-5, 240),  # bounded below at 4 min
    (900, 600), (1e12, 600),  # never longer than the usual cap
    (None, 600), ("300", 600), ("x", 600), (True, 600), ([], 600), ({}, 600), (float("nan"), 600),
])
def test_alert_gap_is_clamped_and_garbage_means_ten_minutes(frame, value, gap):
    assert frame.alert_gap_s(value) == gap


def test_radar_gap_lets_each_new_frame_redraw(frame, monkeypatch):
    monkeypatch.setattr(__import__("time"), "time", lambda: 10_000)
    s = {"alert": "radar:1", "alert_drawn_at": 10_000 - 300, "mode": "radar", "alert_mode": "radar", "alert_gap": 240}
    assert frame.alert_redraw_due(s, "radar:2")  # 5 min on, new frame: draw
    s["alert_drawn_at"] = 10_000 - 200
    assert not frame.alert_redraw_due(s, "radar:3")  # under the 4 min floor: wait


def test_normal_mode_keeps_the_ten_minute_cap(frame, monkeypatch):
    monkeypatch.setattr(__import__("time"), "time", lambda: 10_000)
    s = {"alert": "storm-1", "alert_drawn_at": 10_000 - 300, "mode": "normal", "alert_mode": "normal"}
    assert not frame.alert_redraw_due(s, "storm-2")  # no min_gap_s: 10 minutes, as before
    s["alert_drawn_at"] = 10_000 - 600
    assert frame.alert_redraw_due(s, "storm-2")
    # A state saved by the frame before radar mode existed behaves the same.
    old = {"alert": "storm-1", "alert_drawn_at": 10_000 - 300}
    assert not frame.alert_redraw_due(old, "storm-2")


def test_a_junk_gap_falls_back_to_ten_minutes(frame, monkeypatch):
    monkeypatch.setattr(__import__("time"), "time", lambda: 10_000)
    s = {"alert": "radar:1", "alert_drawn_at": 10_000 - 300, "mode": "radar", "alert_mode": "radar", "alert_gap": "soon"}
    assert not frame.alert_redraw_due(s, "radar:2")


def test_leaving_radar_mode_on_the_server_redraws_at_once(frame, monkeypatch):
    # Midnight, the rain gone or a new warning: the poll's mode differs from
    # what was drawn, so the warning (or the photo) shows now, not in 10 min.
    monkeypatch.setattr(__import__("time"), "time", lambda: 10_000)
    s = {"alert": "radar:1", "alert_drawn_at": 10_000 - 30, "mode": "radar", "alert_mode": "normal"}
    assert frame.alert_redraw_due(s, "abc123")


def test_check_alert_records_the_gap_and_mode(frame, monkeypatch):
    monkeypatch.setattr(frame, "get_json", lambda p: {"id": "radar:9", "mode": "radar", "min_gap_s": 240})
    state = {}
    assert frame.check_alert(state) == "radar:9"
    assert state["alert_gap"] == 240 and state["alert_mode"] == "radar"
    monkeypatch.setattr(frame, "get_json", lambda p: {"id": ""})  # an older server, or normal mode
    assert frame.check_alert(state) == ""
    assert state["alert_gap"] is None and state["alert_mode"] == "normal"


def test_rest_redraws_for_a_new_radar_frame_after_the_short_gap(frame, monkeypatch):
    t = [0]

    def ticks():
        t[0] += 30000
        return t[0]

    monkeypatch.setattr(__import__("time"), "ticks_ms", ticks)
    monkeypatch.setattr(__import__("time"), "time", lambda: 10_000)
    monkeypatch.setattr(frame.inky.button_a, "raw", lambda: False)
    monkeypatch.setattr(frame.inky.button_b, "raw", lambda: False)
    monkeypatch.setattr(frame, "get_json", lambda p: {"id": "radar:2", "mode": "radar", "min_gap_s": 240})
    state = {"alert": "radar:1", "alert_poll": 2, "alert_drawn_at": 10_000 - 250, "mode": "radar", "alert_mode": "radar"}
    assert frame.rest(30, state) == "alert"
