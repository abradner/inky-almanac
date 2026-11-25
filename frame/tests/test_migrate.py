"""The one-off server move: the board rewrites SERVER in config.py once the
release that carries MIGRATE_TO has committed, never during its trial."""

import json
import sys
from pathlib import Path

import pytest

from test_frame import stub_network

NEW = "http://new-host.example:8077"
CONFIG = '# comment\nMODE = "wifi"\nSERVER = "http://server"\nOTHER = 1\n'


@pytest.fixture
def mig(frame, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.py").write_text(CONFIG)
    frame.OTA_FILE = str(tmp_path / "ota.json")
    frame.MIGRATE_TO = NEW
    return frame


def config_text():
    return Path("config.py").read_text()


def test_the_shipped_release_targets_the_athena_name():
    src = (Path(__file__).resolve().parents[1] / "almanac_frame.py").read_text()
    assert 'MIGRATE_TO = "http://inky.asn.casa:8077"' in src


def test_rewrites_only_the_server_line_and_switches_the_running_config(mig):
    assert mig.maybe_migrate() is True
    assert config_text() == CONFIG.replace('SERVER = "http://server"', 'SERVER = "%s"' % NEW)
    assert sys.modules["config"].SERVER == NEW
    assert not Path("config.py.tmp").exists()


def test_already_migrated_is_a_no_op(mig):
    sys.modules["config"].SERVER = NEW
    assert mig.maybe_migrate() is False
    assert config_text() == CONFIG  # untouched


def test_off_when_migrate_to_is_none(mig):
    mig.MIGRATE_TO = None
    assert mig.maybe_migrate() is False
    assert config_text() == CONFIG


def test_never_during_a_trial(mig):
    Path(mig.OTA_FILE).write_text(json.dumps({"trial": "v2", "boots": 1}))
    assert mig.maybe_migrate() is False
    assert config_text() == CONFIG
    assert sys.modules["config"].SERVER == "http://server"


def test_a_missing_server_line_is_appended(mig):
    Path("config.py").write_text('MODE = "wifi"\n')
    mig.maybe_migrate()
    assert config_text() == 'MODE = "wifi"\nSERVER = "%s"\n' % NEW


def test_an_interrupted_write_leaves_the_old_config(mig, monkeypatch):
    def cut(*a):
        raise OSError("power cut")

    monkeypatch.setattr(mig.os, "rename", cut)
    with pytest.raises(OSError):
        mig.maybe_migrate()
    assert config_text() == CONFIG
    assert sys.modules["config"].SERVER == "http://server"


def test_the_trial_draw_commits_first_then_the_same_cycle_moves(mig, monkeypatch):
    Path(mig.OTA_FILE).write_text(json.dumps({"trial": "v2", "trial_version": 2, "boots": 1}))
    stub_network(mig, monkeypatch, info={"hash": "h", "size": 1, "sleep_minutes": 60})
    mig.cycle({}, force=True)  # the trial's forced draw, against the old server
    assert json.loads(Path(mig.OTA_FILE).read_text())["trial"] is None
    assert sys.modules["config"].SERVER == NEW
    assert 'SERVER = "%s"' % NEW in config_text()


def test_a_failed_trial_cycle_does_not_move(mig, monkeypatch):
    Path(mig.OTA_FILE).write_text(json.dumps({"trial": "v2", "boots": 1}))
    stub_network(mig, monkeypatch, fail=OSError(110))
    mig.cycle({})
    assert config_text() == CONFIG


def test_a_rewrite_failure_never_costs_the_refresh(mig, monkeypatch):
    stub_network(mig, monkeypatch, info={"hash": "h", "size": 1, "sleep_minutes": 60})
    Path("config.py").unlink()  # unreadable config
    assert mig.cycle({}, force=True) == 60
    assert any("migrate failed" in line for line in mig.logged)


def test_an_unresolvable_new_host_just_retries(mig, monkeypatch):
    """Until inky.asn.casa exists (or the renderer is up) every fetch fails:
    the board backs off and keeps trying, it doesn't crash or move again."""
    sys.modules["config"].SERVER = NEW
    stub_network(mig, monkeypatch, fail=OSError(-2))  # getaddrinfo failure on MicroPython
    state = {}
    waits = [mig.cycle(state) for _ in range(5)]
    assert waits == [5, 10, 20, 30, 30]
    assert not mig.resets
    assert config_text() == CONFIG  # still the file as it was; nothing rewritten
    stub_network(mig, monkeypatch, info={"hash": "h", "size": 1, "sleep_minutes": 60})
    assert mig.cycle(state) == 60  # host back: normal service
    assert "fail_count" not in state
