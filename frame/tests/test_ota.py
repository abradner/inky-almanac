"""Over-the-air update: frame side (install/verify/commit) and main.py's
rollback guard, plus the contract that the Mac's signer and the frame's
verifier agree byte for byte."""

import hashlib
import importlib.util
import json
import os
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
KEY_HEX = "11" * 32


def load_signer():
    spec = importlib.util.spec_from_file_location("sign_ota", ROOT / "scripts" / "sign_ota.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def ota(frame, monkeypatch, tmp_path):
    """A board filesystem in tmp_path with the current code installed."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "almanac_frame.py").write_text("# version 1\n")
    frame.OTA_FILE = str(tmp_path / "ota.json")
    monkeypatch.setitem(sys.modules, "OTA_KEY", types.SimpleNamespace(KEY=KEY_HEX))
    served = {}
    monkeypatch.setattr(frame, "download", lambda path, dest, size: Path(dest).write_bytes(served["body"]))
    frame.served = served
    return frame


def release(body, key_hex=KEY_HEX, tmp=None, version=1000):
    signer = load_signer()
    src = tmp / "src_almanac_frame.py"
    src.write_bytes(body)
    return signer.sign(key_hex, src, tmp / "ota", version=version)


def test_signer_and_frame_verifier_agree(ota, tmp_path):
    m = release(b"# version 2\n", tmp=tmp_path)
    mac = bytes.fromhex(m["sig"])
    board = ota._hmac_sha256(bytes.fromhex(KEY_HEX), ota.ota_message("almanac_frame.py", m["version"], m["sha256"], m["size"]))
    assert board == mac


def test_signed_release_installs_keeps_prev_and_starts_a_trial(ota, tmp_path):
    body = b"# version 2\n"
    m = release(body, tmp=tmp_path)
    ota.served["body"] = body
    state = {}
    assert ota.maybe_update({"code": m}, state) is True
    assert Path("almanac_frame.py").read_bytes() == body
    assert Path("almanac_frame.py.prev").read_text() == "# version 1\n"
    s = json.loads(Path(ota.OTA_FILE).read_text())
    assert s["trial"] == m["sha256"] and s["trial_version"] == m["version"] and "version" not in s
    assert ota.resets == [1] and state["force_next"] is True


def test_same_version_is_a_no_op(ota, tmp_path):
    m = release(b"# version 1\n", tmp=tmp_path)
    assert ota.maybe_update({"code": m}, {}) is False and ota.resets == []


def test_forged_signature_is_refused(ota, tmp_path):
    m = release(b"# evil\n", key_hex="22" * 32, tmp=tmp_path)  # signed with the wrong key
    ota.served["body"] = b"# evil\n"
    assert ota.maybe_update({"code": m}, {}) is False
    assert Path("almanac_frame.py").read_text() == "# version 1\n"


def test_tampered_download_is_refused(ota, tmp_path):
    m = release(b"# version 2\n", tmp=tmp_path)
    ota.served["body"] = b"# version 2 plus a surprise\n"
    assert ota.maybe_update({"code": m}, {}) is False
    assert Path("almanac_frame.py").read_text() == "# version 1\n"
    assert not Path("almanac_frame.py.new").exists()


def test_known_bad_versions_are_never_retried(ota, tmp_path):
    m = release(b"# version 2\n", tmp=tmp_path)
    Path(ota.OTA_FILE).write_text(json.dumps({"bad": [m["sha256"]]}))
    ota.served["body"] = b"# version 2\n"
    assert ota.maybe_update({"code": m}, {}) is False


def test_without_a_key_ota_is_off(ota, tmp_path, monkeypatch):
    monkeypatch.delitem(sys.modules, "OTA_KEY")
    monkeypatch.setattr("builtins.__import__", _no_ota_key(__import__))
    m = release(b"# version 2\n", tmp=tmp_path)
    assert ota.maybe_update({"code": m}, {}) is False


def _no_ota_key(real):
    def imp(name, *a, **k):
        if name == "OTA_KEY":
            raise ImportError("no key on this board")
        return real(name, *a, **k)

    return imp


def test_a_successful_refresh_commits_the_trial_and_moves_the_baseline(ota):
    Path(ota.OTA_FILE).write_text(json.dumps({"trial": "abc", "trial_version": 1500, "version": 1000, "boots": 2}))
    ota.ota_commit()
    assert json.loads(Path(ota.OTA_FILE).read_text()) == {"trial": None, "trial_version": None, "version": 1500, "boots": 0}


def test_no_second_update_while_a_trial_is_unproven(ota, tmp_path):
    Path(ota.OTA_FILE).write_text(json.dumps({"trial": "someone", "boots": 1}))
    m = release(b"# version 3\n", tmp=tmp_path, version=3000)
    ota.served["body"] = b"# version 3\n"
    assert ota.maybe_update({"code": m}, {}) is False


def test_a_rolled_back_release_can_be_retried(ota, tmp_path):
    # after one failed trial (maybe a network outage) the guard drops the
    # trial but keeps the committed baseline, so the same release is allowed
    Path(ota.OTA_FILE).write_text(json.dumps({"version": 500, "trial": None, "attempts": {"x": 1}}))
    m = release(b"# version 2\n", tmp=tmp_path, version=1000)
    ota.served["body"] = b"# version 2\n"
    assert ota.maybe_update({"code": m}, {}) is True


def test_force_next_survives_an_rtc_wake(ota, monkeypatch):
    from test_frame import run_main

    ota.save_state({"force_next": True})
    ota.inky.rtc_wake = True  # the update reboot read as an RTC wake on battery
    calls = run_main(ota, monkeypatch, cycles=1)
    assert calls[0]["force"] is True


def test_update_reboot_forces_a_draw_so_the_trial_proves_the_draw_path(ota, monkeypatch):
    from test_frame import run_main

    ota.inky.rtc_wake = False  # machine.reset() after an install is a power-on
    calls = run_main(ota, monkeypatch, cycles=1)
    assert calls[0]["force"] is True


def test_an_older_signed_release_is_refused(ota, tmp_path):
    Path(ota.OTA_FILE).write_text(json.dumps({"version": 2000}))
    m = release(b"# old but validly signed\n", tmp=tmp_path, version=1500)
    ota.served["body"] = b"# old but validly signed\n"
    assert ota.maybe_update({"code": m}, {}) is False
    assert Path("almanac_frame.py").read_text() == "# version 1\n"


def test_tampering_with_the_version_breaks_the_signature(ota, tmp_path):
    m = dict(release(b"# version 2\n", tmp=tmp_path, version=1000))
    m["version"] = 999999  # claim to be newer than everything
    ota.served["body"] = b"# version 2\n"
    assert ota.maybe_update({"code": m}, {}) is False


def test_trial_is_recorded_before_the_files_are_swapped(ota, tmp_path, monkeypatch):
    m = release(b"# version 2\n", tmp=tmp_path)
    ota.served["body"] = b"# version 2\n"
    real_rename = os.rename
    seen = []

    def rename(a, b):
        if b == "almanac_frame.py.prev":
            seen.append(json.loads(Path(ota.OTA_FILE).read_text()).get("trial"))
        real_rename(a, b)

    monkeypatch.setattr(ota.os, "rename", rename)
    ota.maybe_update({"code": m}, {})
    assert seen == [m["sha256"]]


def test_an_oversized_download_is_refused(frame, monkeypatch, tmp_path):
    class Raw:
        def __init__(self):
            self.left = 5

        def readinto(self, buf):
            if not self.left:
                return 0
            self.left -= 1
            buf[:4] = b"xxxx"
            return 4

    class Resp:
        status_code = 200
        raw = Raw()

        def close(self):
            pass

    monkeypatch.setitem(sys.modules, "requests", types.SimpleNamespace(get=lambda *a, **k: Resp()))
    with pytest.raises(OSError):
        frame.download("/x", str(tmp_path / "f"), 8)  # promised 8, sends 20
    assert not (tmp_path / "f").exists()
    assert Resp.raw.left > 0, "kept reading past the promised size"


def test_constant_time_compare(frame):
    _same = frame._same
    assert _same("abc", "abc") and not _same("abc", "abd") and not _same("abc", "ab")


def test_cycle_survives_an_update_error(ota, monkeypatch):
    from test_frame import stub_network

    stub_network(ota, monkeypatch, info={"hash": "h", "size": 1, "sleep_minutes": 60, "code": {"sha256": "x", "size": 1, "sig": "y"}})

    def boom(info, state):
        raise OSError("flash full")

    monkeypatch.setattr(ota, "maybe_update", boom)
    assert ota.cycle({}) == 60  # the refresh still counts


# ---- main.py: the boot guard ------------------------------------------------


@pytest.fixture
def guard(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setitem(sys.modules, "config", types.SimpleNamespace(MODE="wifi"))
    spec = importlib.util.spec_from_file_location("frame_main", ROOT / "frame" / "main.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.OTA_FILE = str(tmp_path / "ota.json")
    (tmp_path / "almanac_frame.py").write_text("# new\n")
    (tmp_path / "almanac_frame.py.prev").write_text("# old\n")
    mod.resets = []
    mod.ran = []
    mod.app = types.SimpleNamespace(run=lambda: mod.ran.append(1))
    return mod


def boot(g, importer=None):
    g.guard(lambda: g.resets.append(1), importer or (lambda: g.app))


def test_guard_runs_the_app_normally(guard):
    boot(guard)
    assert guard.ran == [1] and guard.resets == []


def sha_of(text):
    return hashlib.sha256(text.encode()).hexdigest()


NEW = sha_of("# new\n")


def test_trial_boots_are_counted_then_rolled_back_but_retryable_once(guard):
    Path(guard.OTA_FILE).write_text(json.dumps({"trial": NEW, "boots": 0}))
    for _ in range(guard.MAX_TRIAL_BOOTS):
        boot(guard)
    assert guard.resets == [] and Path("almanac_frame.py").read_text() == "# new\n"
    boot(guard)  # one boot too many without a refresh that drew
    assert guard.resets == [1]
    assert Path("almanac_frame.py").read_text() == "# old\n"
    s = json.loads(Path(guard.OTA_FILE).read_text())
    # maybe just a network outage: not bad yet, but the attempt is counted
    assert s["trial"] is None and NEW not in s.get("bad", []) and s["attempts"][NEW] == 1


def test_a_second_failed_trial_marks_the_version_bad(guard):
    Path(guard.OTA_FILE).write_text(json.dumps({"trial": NEW, "boots": guard.MAX_TRIAL_BOOTS, "attempts": {NEW: 1}}))
    boot(guard)
    assert NEW in json.loads(Path(guard.OTA_FILE).read_text())["bad"]


def test_power_cut_mid_swap_restores_the_previous_file(guard):
    Path("almanac_frame.py").unlink()  # renamed away, new file never landed
    Path(guard.OTA_FILE).write_text(json.dumps({"trial": NEW, "boots": 0}))
    boot(guard)
    assert Path("almanac_frame.py").read_text() == "# old\n" and guard.ran == [1]
    s = json.loads(Path(guard.OTA_FILE).read_text())
    assert s["trial"] is None and not s.get("bad") and not s.get("attempts")


def test_trial_recorded_but_never_swapped_is_not_a_trial(guard):
    Path(guard.OTA_FILE).write_text(json.dumps({"trial": sha_of("# something else\n"), "boots": 0}))
    boot(guard)
    s = json.loads(Path(guard.OTA_FILE).read_text())
    assert s["trial"] is None and s.get("boots", 0) == 0 and guard.ran == [1]


def test_trial_arms_the_guard_timer_and_it_stands_down_after_commit(guard):
    armed = []
    Path(guard.OTA_FILE).write_text(json.dumps({"trial": NEW, "boots": 0}))
    guard.guard(lambda: None, lambda: guard.app, armed.append)
    assert armed == [guard.TRIAL_DEADLINE_MS]

    class FakeTimer:
        ONE_SHOT = 0

        def init(self, mode=None, period=None, callback=None):
            self.cb = callback

    fired = []
    t = FakeTimer()
    TimerClass = type("Timer", (), {"ONE_SHOT": 0, "__new__": lambda cls: t})
    fake_machine = types.SimpleNamespace(Timer=TimerClass, reset=lambda: fired.append(1))
    guard._arm(10, machine=fake_machine)
    assert guard._trial_timer is t  # kept referenced, or rp2's GC would deinit it
    t.cb(None)
    assert fired == [1]  # still on trial: resets
    Path(guard.OTA_FILE).write_text(json.dumps({"trial": None}))
    t.cb(None)
    assert fired == [1]  # committed: stands down


def test_corrupt_ota_state_does_not_crash_the_guard(guard):
    Path(guard.OTA_FILE).write_text(json.dumps({"trial": NEW, "boots": "lots", "bad": "nope", "attempts": []}))
    boot(guard)
    assert guard.ran == [1]


def test_a_version_that_will_not_import_is_rolled_back_at_once(guard):
    Path(guard.OTA_FILE).write_text(json.dumps({"trial": NEW, "boots": 0}))

    def broken():
        raise SyntaxError("bad update")

    boot(guard, broken)
    assert guard.resets == [1] and Path("almanac_frame.py").read_text() == "# old\n"
    assert NEW in json.loads(Path(guard.OTA_FILE).read_text())["bad"]


def test_import_failure_without_a_trial_is_not_masked(guard):
    def broken():
        raise SyntaxError("hand-deployed typo")

    with pytest.raises(SyntaxError):
        boot(guard, broken)
    assert Path("almanac_frame.py").read_text() == "# new\n"


def test_only_a_cycle_that_drew_commits_a_trial(ota, monkeypatch):
    from test_frame import stub_network

    Path(ota.OTA_FILE).write_text(json.dumps({"trial": "v2", "boots": 1}))
    stub_network(ota, monkeypatch, fail=OSError(110))
    ota.cycle({})
    assert json.loads(Path(ota.OTA_FILE).read_text())["trial"] == "v2"  # failed: still on trial
    stub_network(ota, monkeypatch, info={"hash": "h", "size": 1, "sleep_minutes": 60})
    ota.cycle({"hash": "h"})  # unchanged, not forced: nothing drawn
    assert json.loads(Path(ota.OTA_FILE).read_text())["trial"] == "v2"
    ota.cycle({"hash": "h"}, force=True)  # drew
    assert json.loads(Path(ota.OTA_FILE).read_text())["trial"] is None


def test_signer_refuses_to_overwrite_an_existing_key(tmp_path):
    signer = load_signer()
    k = tmp_path / "key.txt"
    signer.new_key(k)
    assert len(k.read_text()) == 64 and (k.stat().st_mode & 0o777) == 0o600
    with pytest.raises(SystemExit):
        signer.new_key(k)
