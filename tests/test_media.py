import pytest

from minidv_archiver.media import _meta_args, probe_json, scene_metadata


def test_probe_json_parses_stdout_and_ignores_stderr_noise():
    # ffprobe writes DV decoder warnings to stderr; they must not reach json.loads.
    out = probe_json(["sh", "-c", 'printf "[dvvideo] Concealing bitstream errors\\n" >&2; printf "{\\"ok\\":1}"'])
    assert out == {"ok": 1}


def test_probe_json_raises_clear_error_on_non_json():
    with pytest.raises(RuntimeError, match="non-JSON"):
        probe_json(["sh", "-c", 'printf "[dv @ 0x0] Estimating duration from bitrate"'])


def test_scene_metadata_stamps_real_recording_date():
    m = scene_metadata("TAPE-0005", 3, "2004-04-11T13:53:44", "DV_VAUX", "00:01:02:03", "00:03:04:05")
    assert m["creation_time"] == "2004-04-11T13:53:44.000000Z"
    assert m["date"] == "2004-04-11"
    assert "TAPE-0005" in m["title"] and "TAPE-0005" in m["comment"]
    args = _meta_args(m)
    assert args.count("-metadata") == 4 and "creation_time=2004-04-11T13:53:44.000000Z" in args


def test_scene_metadata_skips_bogus_clock():
    m = scene_metadata("T", 1, "2067-02-15T22:26:25", "DV_VAUX", None, None)
    assert m["creation_time"] is None and m["date"] is None
    assert _meta_args(m).count("-metadata") == 2  # title + comment only


def test_meta_args_drops_empty_values():
    assert _meta_args({"title": "x", "date": None, "comment": ""}) == ["-metadata", "title=x"]
    assert _meta_args(None) == []
