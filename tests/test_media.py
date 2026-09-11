import pytest

from minidv_archiver.media import _ahash16, _meta_args, probe_json, scene_metadata


def test_ahash16_is_64bit_hex_tolerant_of_brightness_shift_sensitive_to_inversion():
    base = bytes((x * 7) % 256 for x in range(256))
    h1 = _ahash16(base)
    assert len(h1) == 16
    int(h1, 16)  # valid hex

    # a uniform brightness shift (different capture gain) barely moves the hash --
    # this is the whole point vs. the old exact-byte hash, which never matched two
    # separate captures of the same footage in practice
    shifted = bytes((b + 3) % 256 for b in base)
    dist = bin(int(h1, 16) ^ int(_ahash16(shifted), 16)).count("1")
    assert dist <= 8

    # genuinely different content (inverted) should flip most of the 64 bits
    inverted = bytes(255 - b for b in base)
    dist2 = bin(int(h1, 16) ^ int(_ahash16(inverted), 16)).count("1")
    assert dist2 >= 32


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
