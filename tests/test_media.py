import sys

import pytest

import minidv_archiver.media as media
from minidv_archiver.media import _ahash16, _meta_args, _restore_filter_chain, probe_json, scene_metadata


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


def test_ahash16_refuses_near_uniform_frames():
    # a near-black (or near-white, or blank-wall) frame carries no real content to
    # fingerprint -- its hash would be noise-sensitive and coincidentally near-match
    # countless unrelated frames, chaining into a false-positive duplicate cluster
    assert _ahash16(bytes([10] * 256)) == ""
    assert _ahash16(bytes([240] * 256)) == ""
    # tiny sensor noise around a flat scene should still count as uniform
    noisy_flat = bytes(10 + (i % 3) for i in range(256))
    assert _ahash16(noisy_flat) == ""


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


def test_restore_filter_chain_unchanged_when_stabilize_and_upscale_off():
    """The whole point: an operator's existing MINIDV_RESTORE_FILTERS override (or
    the plain "Napraw" button) must produce byte-identical -vf output whether or
    not this feature exists, as long as neither new box is checked."""
    base = "bwdif=mode=send_field:parity=bff:deint=all,atadenoise,deblock=filter=strong:block=8"
    assert _restore_filter_chain(base) == base
    assert _restore_filter_chain(base, decimate=True) == f"mpdecimate,{base}"


def test_restore_filter_chain_appends_stabilize_and_upscale_in_order():
    base = "bwdif=...,atadenoise,deblock=..."
    chain = _restore_filter_chain(base, stabilize=True, stabilize_smoothing=8, stabilize_trf="/tmp/x.trf",
                                  upscale=True, upscale_factor=2, upscale_scaler="ewa_lanczossharp")
    assert chain == (
        "bwdif=...,atadenoise,deblock=...,"
        "vidstabtransform=input=/tmp/x.trf:smoothing=8:optzoom=1:interpol=bilinear,"
        "libplacebo=w=iw*2:h=ih*2:upscaler=ewa_lanczossharp")


def test_restore_filter_chain_stabilize_only_and_upscale_only():
    base = "base"
    assert _restore_filter_chain(base, stabilize=True, stabilize_trf="t.trf") == (
        "base,vidstabtransform=input=t.trf:smoothing=20:optzoom=1:interpol=bilinear")
    assert _restore_filter_chain(base, upscale=True, upscale_factor=3, upscale_scaler="spline36") == (
        "base,libplacebo=w=iw*3:h=ih*3:upscaler=spline36")


def test_run_streams_output_in_line_count_batches(monkeypatch):
    """A frozen clock means only the 50-line threshold can flush (never the 0.5s
    one) — isolates the batching logic from real time, so the test isn't flaky."""
    monkeypatch.setattr(media.time, "monotonic", lambda: 0.0)
    calls = []
    cmd = [sys.executable, "-c", "\n".join(["for i in range(120):", "    print(f'line{i}')"])]
    cp = media.run(cmd, log=lambda batch: calls.append(batch))
    assert cp.returncode == 0
    assert cp.stdout.splitlines()[0] == "line0"
    assert cp.stdout.splitlines()[-1] == "line119"
    assert len(calls) == 3  # flushed at 50, at 100, then the trailing 20 at EOF
    assert calls[0].splitlines() == [f"line{i}" for i in range(50)]
    assert calls[1].splitlines() == [f"line{i}" for i in range(50, 100)]
    assert calls[2].splitlines() == [f"line{i}" for i in range(100, 120)]


def test_run_raises_with_truncated_output_on_nonzero_exit():
    cmd = [sys.executable, "-c", "print('boom'); import sys; sys.exit(3)"]
    with pytest.raises(RuntimeError, match=r"command failed \(3\)"):
        media.run(cmd)
