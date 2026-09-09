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


def teardown_module(module):
    importlib.reload(config_mod)
