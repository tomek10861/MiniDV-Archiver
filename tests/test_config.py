import importlib

import minidv_archiver.config as config_mod


def _reload(monkeypatch, **env):
    for k, v in env.items():
        monkeypatch.delenv(k, raising=False) if v is None else monkeypatch.setenv(k, v)
    return importlib.reload(config_mod).Config()


def test_avc_on_by_default_off_when_disabled(monkeypatch):
    assert _reload(monkeypatch, MINIDV_ALLOW_FCP=None).allow_fcp is True
    assert _reload(monkeypatch, MINIDV_ALLOW_FCP="1").allow_fcp is True
    assert _reload(monkeypatch, MINIDV_ALLOW_FCP="0").allow_fcp is False
    assert _reload(monkeypatch, MINIDV_ALLOW_FCP="").allow_fcp is False


def test_camera_guid_defaults_to_autodetect(monkeypatch):
    assert _reload(monkeypatch, MINIDV_CAMERA_GUID=None).camera_guid == ""
    assert _reload(monkeypatch, MINIDV_CAMERA_GUID="0x0800460101B2603D").camera_guid == "0800460101b2603d"


def test_default_recording_tz_corrects_bare_utc_to_warsaw(monkeypatch):
    """The camera's clock is local time, never UTC -- a container that only has
    bare UTC (the common Docker default; nothing else sets it inside one) is never
    actually where the camera was, so that specific reading gets corrected."""
    assert _reload(monkeypatch, MINIDV_DEFAULT_RECORDING_TZ=None, TZ=None).default_recording_tz == "Europe/Warsaw"
    assert _reload(monkeypatch, MINIDV_DEFAULT_RECORDING_TZ=None, TZ="UTC").default_recording_tz == "Europe/Warsaw"
    assert _reload(monkeypatch, MINIDV_DEFAULT_RECORDING_TZ=None, TZ="Etc/UTC").default_recording_tz == "Europe/Warsaw"


def test_default_recording_tz_honors_an_explicit_non_utc_setting(monkeypatch):
    assert _reload(monkeypatch, MINIDV_DEFAULT_RECORDING_TZ=None, TZ="America/New_York").default_recording_tz \
        == "America/New_York"
    assert _reload(monkeypatch, MINIDV_DEFAULT_RECORDING_TZ="Asia/Tokyo", TZ="America/New_York").default_recording_tz \
        == "Asia/Tokyo"      # the more specific MINIDV_ var wins over the generic TZ


def teardown_module(module):
    importlib.reload(config_mod)
