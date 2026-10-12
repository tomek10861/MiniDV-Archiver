import sys

import pytest

import minidv_archiver.media as media
from minidv_archiver.media import (_ahash16, _fraction_progress, _meta_args, _restore_filter_chain, build_archive,
                                    local_to_utc_iso, max_plausible_year, probe_json, run, scene_metadata)


def test_build_archive_zips_each_source_under_its_tape_id(tmp_path):
    """Scenes land in the zip as <tape_id>/<file> (tape_id = parent dir name), so a
    cross-tape playlist selection never collides two tapes' same-named scenes."""
    import zipfile
    a = tmp_path / "TAPE-A"; a.mkdir()
    (a / "0001_x.mp4").write_bytes(b"clip-a")
    b = tmp_path / "TAPE-B"; b.mkdir()
    (b / "0001_x.mp4").write_bytes(b"clip-b")       # same scene filename, different tape
    out = tmp_path / "out.zip"
    build_archive([a / "0001_x.mp4", b / "0001_x.mp4"], out)
    with zipfile.ZipFile(out) as zf:
        names = sorted(zf.namelist())
        assert names == ["TAPE-A/0001_x.mp4", "TAPE-B/0001_x.mp4"]
        assert zf.read("TAPE-A/0001_x.mp4") == b"clip-a"
        assert zf.read("TAPE-B/0001_x.mp4") == b"clip-b"


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


def test_run_reports_progress_from_ffmpeg_style_time_lines():
    # mimics ffmpeg's periodic "frame=.. time=HH:MM:SS.ms bitrate=.." stderr status
    # line; the first match fires immediately (not after a full throttle second).
    calls = []
    run(["sh", "-c", 'printf "frame=1 fps=25 time=00:01:02.50 bitrate=100kbits/s\\n"'],
        progress=lambda s: calls.append(s))
    assert calls == [62.5]


def test_run_progress_throttles_rapid_lines_to_one_call():
    # several time= lines printed back to back (no real delay between them) should
    # only produce the one immediate call -- not one call per line.
    calls = []
    run(["sh", "-c", 'for i in 1 2 3; do printf "time=00:00:0$i.00\\n"; done'],
        progress=lambda s: calls.append(s))
    assert calls == [1.0]


def test_fraction_progress_wraps_seconds_into_a_clamped_fraction():
    calls = []
    cb = _fraction_progress(lambda f: calls.append(f), 10.0)
    cb(5.0)
    cb(20.0)  # past the end -- clamped to 1.0, not left running over
    assert calls == [0.5, 1.0]


def test_fraction_progress_is_none_without_a_callback_or_a_known_total():
    assert _fraction_progress(None, 10.0) is None
    assert _fraction_progress(lambda f: None, None) is None
    assert _fraction_progress(lambda f: None, 0) is None


def test_concat_mp4_reports_progress_up_to_1(tmp_path):
    import shutil as _sh
    import subprocess as _sp
    if not _sh.which("ffmpeg"):
        pytest.skip("ffmpeg not installed")
    clips = []
    for i in range(2):
        p = tmp_path / f"clip{i}.mp4"
        _sp.run(["ffmpeg", "-y", "-v", "quiet", "-f", "lavfi", "-i", "color=c=black:s=64x64:d=1:r=25",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", str(p)], check=True)
        clips.append(p)
    calls = []
    media.concat_mp4(clips, tmp_path / "out.mp4", progress=calls.append)
    assert calls  # at least one progress callback fired
    assert all(0.0 <= f <= 1.0 for f in calls)
    assert calls[-1] > 0.5  # a 2s join should end up reporting well past the midpoint


def test_scene_metadata_stamps_real_recording_date():
    # the camera's clock has no timezone of its own -- it's read as the given zone
    # (CEST, UTC+2 on this date) and converted to a real UTC instant, not just
    # relabeled "Z" in place (that was the bug: a DV date is never already UTC)
    m = scene_metadata("TAPE-0005", 3, "2004-04-11T13:53:44", "DV_VAUX", "00:01:02:03", "00:03:04:05",
                       "Europe/Warsaw")
    assert m["creation_time"] == "2004-04-11T11:53:44.000000Z"
    assert m["date"] == "2004-04-11"          # the "date" tag stays the *local* calendar day
    assert "TAPE-0005" in m["title"] and "TAPE-0005" in m["comment"]
    args = _meta_args(m)
    assert args.count("-metadata") == 4 and "creation_time=2004-04-11T11:53:44.000000Z" in args


def test_scene_metadata_skips_bogus_clock():
    m = scene_metadata("T", 1, "2067-02-15T22:26:25", "DV_VAUX", None, None)
    assert m["creation_time"] is None and m["date"] is None
    assert _meta_args(m).count("-metadata") == 2  # title + comment only


def test_scene_metadata_accepts_the_current_year():
    """Regression: the plausible-year ceiling used to be a hardcoded 2025, which
    would have started rejecting every real capture made from 2026 onward -- the
    cutoff must track the actual current year instead."""
    import datetime as dt
    this_year = dt.datetime.now().year
    m = scene_metadata("T", 1, f"{this_year}-01-02T03:04:05", "DV_VAUX", None, None)
    assert m["creation_time"] is not None and m["date"] == f"{this_year}-01-02"


def test_max_plausible_year_tracks_the_current_year():
    import datetime as dt
    assert max_plausible_year() == dt.datetime.now().year + 1


def test_local_to_utc_iso_converts_across_dst():
    # CEST (summer, UTC+2) vs CET (winter, UTC+1) -- a fixed offset would get one wrong
    assert local_to_utc_iso("2004-07-11T17:29:09", "Europe/Warsaw") == "2004-07-11T15:29:09.000000Z"
    assert local_to_utc_iso("2004-01-11T17:29:09", "Europe/Warsaw") == "2004-01-11T16:29:09.000000Z"


def test_local_to_utc_iso_utc_zone_is_a_no_op():
    assert local_to_utc_iso("2004-07-11T17:29:09", "UTC") == "2004-07-11T17:29:09.000000Z"


def test_local_to_utc_iso_falls_back_on_bad_zone_rather_than_raising():
    # a typo'd/unknown zone must not take the whole encode down with it
    assert local_to_utc_iso("2004-07-11T17:29:09", "Not/AZone") == "2004-07-11T17:29:09.000000Z"


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
