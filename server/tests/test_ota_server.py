import json
import urllib.error
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from almanac.app import Almanac
from almanac.config import Config

from test_almanac import _serve, payload, photos, vivid, weather

SYD = ZoneInfo("Australia/Sydney")


def app_with(tmp_path, manifest=None, body=None):
    a = Almanac(
        Config(cache_dir=tmp_path / "cache"),
        weather_loader=lambda: weather.parse(payload(), datetime(2026, 11, 10, 9, tzinfo=SYD).timestamp()),
        photo_loader=lambda d, s: photos.Photo("1", "P", "c", vivid()),
        conditions_loader=lambda d, t, h: None,
        bom_loader=lambda: {},
    )
    a.ota_dir = tmp_path / "ota"
    a.ota_dir.mkdir()
    if manifest is not None:
        (a.ota_dir / "manifest.json").write_text(json.dumps(manifest))
    if body is not None:
        (a.ota_dir / "almanac_frame.py").write_bytes(body)
    return a


def test_frame_json_advertises_the_signed_release(tmp_path):
    m = {"name": "almanac_frame.py", "version": 1794000000, "sha256": "ab" * 32, "size": 10, "sig": "cd" * 32, "path": "/ota/almanac_frame.py"}
    info = app_with(tmp_path, m).frame_info(datetime(2026, 11, 10, 9, 1, tzinfo=SYD))
    assert info["code"] == {k: m[k] for k in ("version", "sha256", "size", "sig", "path")}


def test_no_or_broken_manifest_means_no_release(tmp_path):
    t = datetime(2026, 11, 10, 9, 1, tzinfo=SYD)
    assert "code" not in app_with(tmp_path).frame_info(t)
    (tmp_path / "x").mkdir()
    broken = app_with(tmp_path / "x", {"sha256": "only"})
    assert "code" not in broken.frame_info(t)


def test_release_file_is_served_and_absent_release_is_404(tmp_path):
    srv, base = _serve(app_with(tmp_path, body=b"# v2\n"))
    try:
        assert urllib.request.urlopen(base + "/ota/almanac_frame.py").read() == b"# v2\n"
    finally:
        srv.shutdown()
    (tmp_path / "y").mkdir()
    srv, base = _serve(app_with(tmp_path / "y"))
    try:
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(base + "/ota/almanac_frame.py")
        assert e.value.code == 404
    finally:
        srv.shutdown()
