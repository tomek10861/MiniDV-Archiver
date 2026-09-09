import subprocess

import minidv_archiver.camera as cam
from minidv_archiver.camera import Camera, SAFE_COMMANDS, TRANSPORT, _response_bytes


def test_info_is_passive_never_touches_avc(monkeypatch):
    # /api/status calls Camera.info(); it must never spawn firewire-request / send FCP.
    def boom(*a, **k):
        raise AssertionError("info() must not run a subprocess")
    monkeypatch.setattr(subprocess, "run", boom)
    info = Camera("0800460101b2603d").info()  # must not raise -> no subprocess spawned
    assert isinstance(info["connected"], bool) and "guid" in info


def test_match_node_by_guid_or_autodetects_avc_unit(tmp_path, monkeypatch):
    root = tmp_path / "fw"
    (root / "fw0").mkdir(parents=True)      # local OHCI controller: no AV/C tape unit
    (root / "fw0" / "guid").write_text("0x0011229900aabbcc\n")
    (root / "fw0" / "units").write_text("0x00609e:0x010483\n")
    (root / "fw1").mkdir()                  # the camcorder: AV/C tape unit
    (root / "fw1" / "guid").write_text("0x0800460101B2603D\n")
    (root / "fw1" / "units").write_text("0x00a02d:0x010001\n")
    monkeypatch.setattr(cam, "FW_DEVICES", root)

    assert Camera("")._match_node().name == "fw1"                  # auto-detect -> the AV/C device
    assert Camera("0800460101b2603d")._match_node().name == "fw1"  # explicit GUID (case-insensitive)
    assert Camera("dead" * 4)._match_node() is None                # unknown GUID


def test_response_parser():
    assert _response_bytes("response: 000: 0c 20 c4 60   . .`") == ["0c", "20", "c4", "60"]


def test_safe_commands_have_no_record_opcode():
    assert set(SAFE_COMMANDS) == {"play", "stop", "pause", "rewind", "ff"}
    assert all(" c2 " not in f" {value} " for value in SAFE_COMMANDS.values())


def test_transport_mapping():
    assert TRANSPORT[("c3", "75")] == "PLAYING"
    assert TRANSPORT[("c4", "60")] == "STOPPED"

