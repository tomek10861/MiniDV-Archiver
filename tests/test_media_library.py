import os
import pathlib
import shutil
import subprocess
import time

import pytest

from minidv_archiver import media_library as ml
from minidv_archiver.config import Config
from minidv_archiver.store import JobStore


def _cfg(tmp_path, src):
    c = Config(storage=tmp_path / "st", media_dirs=str(src), default_recording_tz="Europe/Warsaw")
    c.ensure_dirs()
    return c


def _age(p, seconds=3600):
    t = time.time() - seconds
    os.utime(p, (t, t))


def test_dates_from_file_names():
    assert ml.date_from_name("IMG_20190704_123456.jpg") == "2019-07-04T12:34:56"
    assert ml.date_from_name("VID-20190704-WA0001.mp4") == "2019-07-04"
    assert ml.date_from_name("2019-07-04 12.34.56.jpg") == "2019-07-04T12:34:56"
    assert ml.date_from_name("PXL_20231231_235959123.jpg") == "2023-12-31T23:59:59"
    assert ml.date_from_name("DSC01234.JPG") is None
    assert ml.date_from_name("IMG_20670215_000000.jpg") is None          # a camera clock gone wild


def test_video_creation_time_is_utc_but_apple_creationdate_is_local():
    utc = {"format": {"tags": {"creation_time": "2019-07-04T10:34:56.000000Z"}}, "streams": []}
    assert ml._video_dt(utc, "Europe/Warsaw") == "2019-07-04T12:34:56"
    apple = {"format": {"tags": {"com.apple.quicktime.creationdate": "2019-07-04T12:34:56+0200",
                                 "creation_time": "2019-07-04T10:34:56Z"}}, "streams": []}
    assert ml._video_dt(apple, "Europe/Warsaw") == "2019-07-04T12:34:56"


def test_operator_date_fix_wins():
    row = {"taken": "2019-07-04T12:34:56", "date_fix": None}
    assert ml.effective_date(row) == ("2019-07-04", "12:34:56")
    assert ml.effective_date({**row, "date_fix": "unknown"}) == (None, None)
    assert ml.effective_date({**row, "date_fix": "2005-01-02"}) == ("2005-01-02", None)


def test_scan_skips_temp_and_settling_files_and_keeps_items_of_a_missing_share(tmp_path):
    PIL = pytest.importorskip("PIL.Image")
    src = tmp_path / "zdjecia"
    (src / "DCIM").mkdir(parents=True)
    PIL.new("RGB", (640, 480), "red").save(src / "DCIM" / "IMG_20190704_123456.jpg")
    PIL.new("RGB", (640, 480), "red").save(src / "DCIM" / ".IMG_tmp.jpg.aB3dE", "JPEG")   # rsync partial
    PIL.new("RGB", (640, 480), "red").save(src / "DCIM" / "fresh.jpg")            # still copying
    (src / "DCIM" / "notes.txt").write_text("x")
    _age(src / "DCIM" / "IMG_20190704_123456.jpg")
    cfg = _cfg(tmp_path, src)
    s = JobStore(cfg.state / "jobs.db")
    res = ml.scan(cfg, s)
    assert res["added"] == 1 and [m["path"].rsplit("/", 1)[1] for m in s.media_paths()] == ["IMG_20190704_123456.jpg"]
    assert ml.scan(cfg, s)["added"] == 0                                     # unchanged -> nothing to redo
    assert ml.process_pending(cfg, s) == 1
    row = s.media_ready()[0]
    assert row["album"] == "zdjecia/DCIM" and row["taken"] == "2019-07-04T12:34:56" and row["date_source"] == "name"
    assert ml.derived(cfg, "thumbs", row["id"], ".jpg").exists()
    shutil.rmtree(src)                                                       # NAS offline: keep everything
    assert ml.scan(cfg, s)["removed"] == [] and len(s.media_paths()) == 1


def test_exif_date_and_heic_display_copy(tmp_path):
    PIL = pytest.importorskip("PIL.Image")
    pytest.importorskip("pillow_heif").register_heif_opener()
    src = tmp_path / "zdjecia"
    src.mkdir()
    im = PIL.new("RGB", (800, 600), "blue")
    exif = im.getexif()
    exif[0x0132] = "2012:05:06 07:08:09"
    im.save(src / "photo.heic", exif=exif)
    _age(src / "photo.heic")
    cfg = _cfg(tmp_path, src)
    s = JobStore(cfg.state / "jobs.db")
    ml.scan(cfg, s)
    ml.process_pending(cfg, s)
    row = s.media_ready()[0]
    assert row["taken"] == "2012-05-06T07:08:09" and row["date_source"] == "exif"
    assert ml.derived(cfg, "display", row["id"], ".jpg").exists()           # browsers can't show HEIC


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
def test_video_needing_a_proxy_gets_one(tmp_path):
    src = tmp_path / "zdjecia"
    src.mkdir()
    clip = src / "MOV_0001.avi"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10", "-t", "1",
                    "-c:v", "mpeg4", str(clip)], check=True)
    _age(clip)
    cfg = _cfg(tmp_path, src)
    s = JobStore(cfg.state / "jobs.db")
    ml.scan(cfg, s)
    ml.process_pending(cfg, s)
    row = s.media_ready()[0]
    assert row["playable"] == 0 and ml.video_path(cfg, row) is None and ml.face_source(cfg, s, str(row["id"])) is None
    assert ml.build_proxies(cfg, s) == 1
    row = s.media_get(row["id"])
    assert ml.video_path(cfg, row).name == f"{row['id']}.mp4"
    assert ml.face_source(cfg, s, str(row["id"]))[0] == "video"


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
def test_montage_mixes_dv_video_and_photo_into_one_frame_size(tmp_path):
    PIL = pytest.importorskip("PIL.Image")
    from minidv_archiver.media import ffprobe, montage_mp4
    dv = tmp_path / "dv.mp4"            # 4:3 PAL-ish scene with sound
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=720x576:rate=25", "-f", "lavfi",
                    "-i", "sine=frequency=440", "-t", "1", "-c:v", "libx264", "-c:a", "aac", "-shortest", str(dv)],
                   check=True)
    phone = tmp_path / "phone.mp4"      # vertical, no audio
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=360x640:rate=30", "-t", "1",
                    "-c:v", "libx264", str(phone)], check=True)
    photo = tmp_path / "p.jpg"
    PIL.new("RGB", (300, 400), "green").save(photo)
    out = tmp_path / "out.mp4"
    montage_mp4([{"path": dv, "kind": "dv"}, {"path": phone, "kind": "video"}, {"path": photo, "kind": "photo"}],
                out, width=320, height=180, photo_s=1.0)
    info = ffprobe(out)
    v = next(s for s in info["streams"] if s["codec_type"] == "video")
    assert (v["width"], v["height"]) == (320, 180)
    assert any(s["codec_type"] == "audio" for s in info["streams"])
    assert 2.7 < float(info["format"]["duration"]) < 3.6


def test_plan_clips_cuts_on_the_strongest_change_and_keeps_the_tail():
    cuts = [(30, 0.9), (100, 0.4), (130, 0.8), (290, 0.5), (420, 0.95), (560, 0.6)]
    clips = ml.plan_clips(cuts, 600, min_len=45, max_len=300)
    assert clips[0] == (0, 130) and clips[1] == (130, 420)       # strongest change within 45..300 s each time
    assert clips[-1][1] == 600 and all(b - a >= 45 for a, b in clips)
    assert ml.plan_clips([], 200) == [(0, 200)]                   # short: one piece
    assert ml.plan_clips([], 620, max_len=300) == [(0, 300), (300, 620)]   # no cuts: 20 s tail joins


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
def test_long_video_becomes_scene_clips(tmp_path):
    src = tmp_path / "zdjecia"
    src.mkdir()
    film = src / "VID_20240815_150000.mp4"
    colors = ["red", "blue", "green", "white", "black", "yellow", "purple"]
    inputs = [x for c in colors for x in ("-f", "lavfi", "-i", f"color=c={c}:s=64x48:r=5:d=60")]
    subprocess.run(["ffmpeg", "-v", "error", *inputs, "-filter_complex",
                    "".join(f"[{i}]" for i in range(len(colors))) + f"concat=n={len(colors)}:v=1",
                    "-c:v", "libx264", "-g", "5", str(film)], check=True)
    _age(film)
    cfg = Config(storage=tmp_path / "st", media_dirs=str(src), media_split_min_s=300,
                 default_recording_tz="Europe/Warsaw")
    cfg.ensure_dirs()
    s = JobStore(cfg.state / "jobs.db")
    ml.scan(cfg, s)
    ml.process_pending(cfg, s)
    parent = s.media_ready()[0]
    assert ml.split_long_videos(cfg, s) == [parent["id"]]
    clips = s.media_ready()
    assert len(clips) >= 2 and all(c["parent_id"] == parent["id"] for c in clips)   # the film itself is hidden
    assert clips[0]["taken"] == "2024-08-15T15:00:00" and clips[1]["taken"] > clips[0]["taken"]
    assert all(pathlib.Path(c["path"]).exists() and ml.face_source(cfg, s, str(c["id"])) for c in clips)
    film.unlink()                                                  # source gone -> clips go with it
    (src / "keep.jpg").write_bytes(b"x")
    removed = ml.scan(cfg, s)["removed"]
    assert set(removed) == {parent["id"], *[c["id"] for c in clips]}
    assert not any(pathlib.Path(c["path"]).exists() for c in clips)


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
def test_dvd_title_set_is_one_video_named_and_dated_by_its_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(ml, "DVD_MIN_BYTES", 0)
    src = tmp_path / "zdjecia"
    ts = src / "Wesele rodziców 15.08.1998" / "VIDEO_TS"
    ts.mkdir(parents=True)
    vob = tmp_path / "title.vob"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=720x576:rate=25", "-f", "lavfi",
                    "-i", "sine", "-t", "4", "-c:v", "mpeg2video", "-flags", "+ilme+ildct", "-top", "1",
                    "-aspect", "4:3", "-c:a", "ac3", "-f", "vob", str(vob)], check=True)   # interlaced, like VHS
    data = vob.read_bytes()
    (ts / "VTS_01_1.VOB").write_bytes(data[: len(data) // 2])       # DVDs split a title at 1 GB
    (ts / "VTS_01_2.VOB").write_bytes(data[len(data) // 2:])
    (ts / "VTS_01_0.VOB").write_bytes(data[:2048])                  # menu: not part of the film
    (ts / "VIDEO_TS.VOB").write_bytes(data[:2048])
    for f in ts.iterdir():
        _age(f)
    cfg = _cfg(tmp_path, src)
    s = JobStore(cfg.state / "jobs.db")
    ml.scan(cfg, s)
    assert ml.process_pending(cfg, s) == 1
    row = s.media_ready()[0]
    assert row["name"] == "Wesele rodziców 15.08.1998 — DVD" and row["album"] == "zdjecia/Wesele rodziców 15.08.1998"
    assert row["taken"] == "1998-08-15" and 3.5 < row["duration"] < 4.5 and row["playable"] == 0
    assert ml.build_proxies(cfg, s) == 1
    assert ml.face_source(cfg, s, str(row["id"]))[0] == "video"
    from minidv_archiver.media import ffprobe
    v = next(x for x in ffprobe(ml.video_path(cfg, s.media_get(row["id"])))["streams"] if x["codec_type"] == "video")
    # every field became a frame (50p), native size with its pixel aspect -> still 4:3
    assert v["r_frame_rate"] == "50/1" and (v["width"], v["height"]) == (720, 576)
    assert v["sample_aspect_ratio"] == "16:15" and s.media_get(row["id"])["fps"] == 50
    assert ml.public(s.media_get(row["id"]))["fps"] == 50
    # the same VOBs in a folder that isn't called VIDEO_TS (a disc copied as-is) are a DVD too,
    # and the menu-only VOBs never show up as videos of their own
    slub = src / "backup" / "slub"
    shutil.copytree(ts, slub)
    for f in slub.iterdir():
        _age(f)
    ml.scan(cfg, s)
    menu_only = src / "cd1" / "VIDEO_TS"                          # a disc with nothing but its menu
    menu_only.mkdir(parents=True)
    for name in ("VIDEO_TS.VOB", "VTS_01_0.VOB"):
        (menu_only / name).write_bytes(data[:2048])
        _age(menu_only / name)
    ml.scan(cfg, s)
    names = sorted(r["name"] for r in s._media_rows())
    assert names == ["Wesele rodziców 15.08.1998 — DVD", "slub — DVD"]


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
def test_conversion_reports_progress(tmp_path):
    src = tmp_path / "zdjecia"
    src.mkdir()
    clip = src / "MOV_0001.avi"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10", "-t", "3",
                    "-c:v", "mpeg4", str(clip)], check=True)
    _age(clip)
    cfg = _cfg(tmp_path, src)
    s = JobStore(cfg.state / "jobs.db")
    ml.scan(cfg, s)
    ml.process_pending(cfg, s)
    seen = []
    ml.build_proxies(cfg, s, report=lambda stage, i, total, detail, frac=0.0: seen.append((stage, total, frac)))
    assert seen[0][:2] == ("convert", 1) and max(f for *_, f in seen) > 0.5


def _row(s, path, album, kind, mtime, **fields):
    s.media_seen(path, album=album, name=path.rsplit("/", 1)[1], kind=kind, size=1, mtime=mtime, workspace="default")
    mid = next(m["id"] for m in s.media_paths() if m["path"] == path)
    s.media_update(mid, status="ready", **fields)
    return mid


def test_file_times_date_untagged_videos_only_where_photos_vouch_for_them(tmp_path):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    cfg = Config(storage=tmp_path, default_recording_tz="Europe/Warsaw")
    cfg.ensure_dirs()
    s = JobStore(cfg.state / "jobs.db")
    waw = ZoneInfo("Europe/Warsaw")
    t = lambda iso: datetime.fromisoformat(iso).replace(tzinfo=waw).timestamp()  # noqa: E731
    # Sony folder: photo file times == EXIF (camera clock), the MPG has no tags at all
    for i, iso in enumerate(["2009-07-18T14:10:00", "2009-07-18T14:20:00", "2009-07-18T14:30:00"]):
        _row(s, f"/m/lurt/DSC0736{i}.JPG", "lurt", "photo", t(iso), taken=iso, date_source="exif")
    mpg = _row(s, "/m/lurt/MOV07362.MPG", "lurt", "video", t("2009-07-18T14:40:00"))
    # a folder copied in bulk: every file has the copy time, no EXIF to check against
    copied = [_row(s, f"/m/kopia/MOV{i}.AVI", "kopia", "video", t("2021-03-06T12:00:00") + i) for i in range(4)]
    assert ml.date_from_file_times(cfg, s) == 1
    assert s.media_get(mpg)["taken"] == "2009-07-18T14:40:00" and s.media_get(mpg)["date_source"] == "file"
    assert all(s.media_get(c)["taken"] is None and s.media_get(c)["date_source"] == "none" for c in copied)
    assert ml.date_from_file_times(cfg, s) == 0                      # nothing left to look at


def test_neighbours_date_a_file_between_two_numbered_photos(tmp_path):
    cfg = Config(storage=tmp_path, default_recording_tz="Europe/Warsaw")
    cfg.ensure_dirs()
    s = JobStore(cfg.state / "jobs.db")
    _row(s, "/m/x/DSC07359.JPG", "x", "photo", 1.0, taken="2009-07-18T15:01:11", date_source="exif")
    _row(s, "/m/x/DSC07363.JPG", "x", "photo", 1.0, taken="2009-07-18T16:41:11", date_source="exif")
    _row(s, "/m/x/DSC09000.JPG", "x", "photo", 1.0, taken="2010-01-01T10:00:00", date_source="exif")
    mid = _row(s, "/m/x/MOV07362.MPG", "x", "video", 1.0, date_source="none")
    lone = _row(s, "/m/x/MOV08000.MPG", "x", "video", 1.0, date_source="none")      # nobody close by
    assert ml.date_from_neighbors(cfg, s) == 1
    assert s.media_get(mid)["taken"] == "2009-07-18T15:51:11" and s.media_get(mid)["date_source"] == "neighbors"
    assert s.media_get(lone)["taken"] is None and s.media_get(lone)["date_source"] == "nodate"


def test_album_date_fills_only_undated_files(tmp_path):
    cfg = Config(storage=tmp_path)
    cfg.ensure_dirs()
    s = JobStore(cfg.state / "jobs.db")
    dated = _row(s, "/m/egipt/DSCN0001.JPG", "egipt", "photo", 1.0, taken="2012-03-01T10:00:00", date_source="exif")
    blank = _row(s, "/m/egipt/DSCN0002.JPG", "egipt", "photo", 1.0, date_source="nodate")
    other = _row(s, "/m/inny/DSCN0003.JPG", "inny", "photo", 1.0, date_source="nodate")
    assert s.media_set_album_date("default", "egipt", "2010-02-14") == 1
    assert ml.effective_date(s.media_get(blank)) == ("2010-02-14", None)
    assert ml.effective_date(s.media_get(dated))[0] == "2012-03-01"
    assert s.media_get(other)["date_fix"] is None


def test_screenshots_software_folders_and_icons_stay_out(tmp_path):
    PIL = pytest.importorskip("PIL.Image")
    src = tmp_path / "zdjecia"
    for d in ("DCIM/Screenshots", "Dysk/Program Files/Winamp", "wakacje"):
        (src / d).mkdir(parents=True)
    PIL.new("RGB", (1080, 1920), "red").save(src / "DCIM/Screenshots/IMG_1.png")
    PIL.new("RGB", (1080, 1920), "red").save(src / "wakacje/Screenshot_20190704-123456.png")
    PIL.new("RGB", (1080, 1920), "red").save(src / "wakacje/Zrzut ekranu 2020-01-01.png")
    PIL.new("RGB", (640, 480), "red").save(src / "Dysk/Program Files/Winamp/skin.png")
    PIL.new("RGB", (44, 34), "red").save(src / "wakacje/button.png")
    PIL.new("RGB", (640, 480), "red").save(src / "wakacje/DSC0001.JPG")
    for f in src.rglob("*.*"):
        _age(f)
    cfg = _cfg(tmp_path, src)
    s = JobStore(cfg.state / "jobs.db")
    ml.scan(cfg, s)
    ml.process_pending(cfg, s)
    assert [r["name"] for r in s.media_ready()] == ["DSC0001.JPG"]


def _vob_of_colours(path, colours, seconds_each=1.0):
    """An interlaced 4:3 MPEG-2 programme stream, one solid colour per second, with a tone."""
    inputs = [x for c in colours for x in ("-f", "lavfi", "-i", f"color=c={c}:s=720x576:r=25:d={seconds_each}")]
    subprocess.run(["ffmpeg", "-v", "error", "-y", *inputs, "-f", "lavfi", "-i", "sine=frequency=440",
                    "-filter_complex", "".join(f"[{i}]" for i in range(len(colours))) + f"concat=n={len(colours)}:v=1[v]",
                    "-map", "[v]", "-map", f"{len(colours)}:a", "-t", str(len(colours) * seconds_each),
                    "-c:v", "mpeg2video", "-b:v", "4M", "-flags", "+ilme+ildct", "-top", "1", "-aspect", "4:3",
                    "-c:a", "ac3", "-f", "vob", str(path)], check=True)


def _colour_at(video, t):
    """Mean (R, G, B) of the frame shown at ``t`` seconds."""
    raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t:.3f}", "-i", str(video), "-frames:v", "1",
                          "-vf", "scale=8:8", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                         capture_output=True, check=True).stdout
    return tuple(round(sum(raw[i::3]) / 64 / 32) for i in range(3))     # coarse buckets: codec noise drops out


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
def test_reencoding_a_split_dvd_keeps_parts_and_face_times_on_the_same_moment(tmp_path, monkeypatch):
    monkeypatch.setattr(ml, "DVD_MIN_BYTES", 0)
    src = tmp_path / "zdjecia"
    ts = src / "wesele" / "VIDEO_TS"
    ts.mkdir(parents=True)
    colours = ["red", "lime", "blue", "white", "black", "yellow", "magenta", "cyan", "gray", "orange", "navy", "pink"]
    _vob_of_colours(ts / "VTS_01_1.VOB", colours)
    _age(ts / "VTS_01_1.VOB")
    cfg = _cfg(tmp_path, src)
    s = JobStore(cfg.state / "jobs.db")
    ml.scan(cfg, s)
    ml.process_pending(cfg, s)
    film = s.media_ready()[0]
    # a copy made the old way (25p, a keyframe only every 10 s) and parts cut from it losslessly:
    # each old part file starts on the keyframe before its bound (here 0 s)
    proxy = ml.derived(cfg, "proxy", film["id"], ".mp4")
    proxy.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", ml.source(film), "-vf",
                    "bwdif=mode=send_frame:deint=interlaced,scale='trunc(iw*sar/2)*2':ih,setsar=1,format=yuv420p",
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-g", "250", "-sc_threshold", "0",
                    "-c:a", "aac", str(proxy)],
                   check=True)
    s.media_update(film["id"], proxy=1)
    film = s.media_get(film["id"])
    bounds = [(0.0, 4.3), (4.3, 8.6), (8.6, 12.0)]
    clip_dir = cfg.storage / "media" / "clips"
    clip_dir.mkdir(parents=True, exist_ok=True)
    ids = []
    for k, (a, b) in enumerate(bounds):
        out = clip_dir / f"{film['id']}_{k:03d}.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{a}", "-i", str(proxy), "-t", f"{b - a}", "-c", "copy",
                        "-avoid_negative_ts", "make_zero", str(out)], check=True)
        ids.append(s.media_add_clip(film, path=str(out), start=a, end=b, taken=None, name=f"part {k + 1}"))
    s.media_update(film["id"], split=1)
    # a face seen in part 2 at second 6 of its old file (= 6 s into the film: 4.3 s of preroll + 1.7)
    with s._lock:
        s._conn.execute("INSERT INTO face_tracks (person_id, workspace, tape_id, scene_id, embedding, quality, times) "
                        "VALUES (1, 'default', ?, ?, x'00', 1, '[6, 7]')", (ml.MEDIA_TAPE, str(ids[1])))
    seen_old = [_colour_at(s.media_get(ids[1])["path"], t + 0.5) for t in (6, 7)]

    s.media_update(film["id"], reproxy=1)
    assert ml.reencode_dvd(cfg, s, s.media_reproxy_pending()[0])

    film = s.media_get(film["id"])
    assert film["reproxy"] == 0 and film["fps"] == 50 and not s.media_reproxy_pending()
    parts = s.media_children(film["id"])
    assert [p["id"] for p in parts] == ids                         # same parts, same bounds
    assert [(p["clip_start"], p["clip_end"]) for p in parts] == bounds
    from minidv_archiver.media import ffprobe
    for p, (a, b) in zip(parts, bounds):
        assert p["fps"] == 50
        info = ffprobe(p["path"])
        # the new part starts on its bound (a lossless cut still ends on a whole packet: a few frames over)
        assert -0.05 < float(info["format"]["duration"]) - (b - a) < 0.2
        v = next(x for x in info["streams"] if x["codec_type"] == "video")
        assert v["r_frame_rate"] == "50/1" and v["sample_aspect_ratio"] == "16:15"
    times = s.face_scene_tracks(ml.MEDIA_TAPE, str(ids[1]))[0]["times"]
    assert times == [2, 3]                                         # 6 - 4.3 = 1.7 -> moved by the 4.3 s preroll
    # ...and the moment they point at shows the same picture as before
    seen_new = [_colour_at(parts[1]["path"], t + 0.5 - 0.3) for t in times]
    assert seen_new == seen_old
    assert not list(clip_dir.glob("*.next.mp4")) and not list(proxy.parent.glob("*.next.mp4"))


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
def test_failed_reencode_leaves_the_old_copy(tmp_path, monkeypatch):
    monkeypatch.setattr(ml, "DVD_MIN_BYTES", 0)
    src = tmp_path / "zdjecia"
    ts = src / "slub" / "VIDEO_TS"
    ts.mkdir(parents=True)
    _vob_of_colours(ts / "VTS_01_1.VOB", ["red", "blue"])
    _age(ts / "VTS_01_1.VOB")
    cfg = _cfg(tmp_path, src)
    s = JobStore(cfg.state / "jobs.db")
    ml.scan(cfg, s)
    ml.process_pending(cfg, s)
    ml.build_proxies(cfg, s)
    film = s.media_ready()[0]
    proxy = ml.derived(cfg, "proxy", film["id"], ".mp4")
    before = proxy.read_bytes()
    monkeypatch.setattr(ml, "dvd_proxy_cmd", lambda *a, **k: ["ffmpeg", "-v", "error", "-i", "/nonexistent", "x.mp4"])
    s.media_update(film["id"], reproxy=1)
    assert not ml.reencode_dvd(cfg, s, s.media_get(film["id"]))
    assert proxy.read_bytes() == before and s.media_get(film["id"])["reproxy"] == -1
    assert not s.media_reproxy_pending()


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
def test_fps_is_backfilled_for_videos_indexed_before_it_was_kept(tmp_path):
    src = tmp_path / "zdjecia"
    src.mkdir()
    clip = src / "VID_20200101_120000.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=160x120:rate=30", "-t", "1",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(clip)], check=True)
    _age(clip)
    cfg = _cfg(tmp_path, src)
    s = JobStore(cfg.state / "jobs.db")
    ml.scan(cfg, s)
    ml.process_pending(cfg, s)
    row = s.media_ready()[0]
    assert row["fps"] == 30
    s.media_update(row["id"], fps=None)                             # as rows from before the column
    assert ml.backfill_fps(cfg, s) == 1 and s.media_get(row["id"])["fps"] == 30
    assert ml.backfill_fps(cfg, s) == 0


def _flash_and_beep(path, *, at, video_s, audio_s, rate=50, sar="16/15"):
    """720x576 (anamorphic) black picture with one white flash and a tone burst at ``at``."""
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"color=c=black:s=720x576:r={rate}:d={video_s}",
                    "-f", "lavfi", "-i", f"anullsrc=r=48000:cl=stereo:d={audio_s}",
                    "-f", "lavfi", "-i", f"sine=frequency=1000:r=48000:d={audio_s}",
                    "-filter_complex",
                    f"[0]drawbox=c=white:t=fill:enable='between(t,{at},{at + 0.1})',setsar={sar}[v];"
                    f"[2]volume=enable='not(between(t,{at},{at + 0.1}))':volume=0,aformat=channel_layouts=stereo[b];"
                    "[1][b]amix=inputs=2:normalize=0[a]",
                    "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(path)],
                   check=True)


def _events(path):
    """(first bright frame time, first loud audio time) of each flash/beep, in order."""
    v = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-vf",
                        "signalstats,metadata=print:key=lavfi.signalstats.YAVG:file=-", "-an", "-f", "null", "-"],
                       capture_output=True, text=True).stdout
    a = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-af",
                        "asetnsamples=480,astats=metadata=1:reset=1,"
                        "ametadata=print:key=lavfi.astats.Overall.RMS_level:file=-", "-vn", "-f", "null", "-"],
                       capture_output=True, text=True).stdout

    def onsets(text, test):
        out, t, prev = [], None, False
        for line in text.splitlines():
            if "pts_time:" in line:
                t = float(line.split("pts_time:")[1].split()[0])
            elif "=" in line and t is not None:
                on = test(line.split("=")[1])
                if on and not prev:
                    out.append(t)
                prev = on
        return out
    flashes = onsets(v, lambda x: float(x) > 128)
    beeps = onsets(a, lambda x: x not in ("-inf", "nan") and float(x) > -30)
    return flashes, beeps


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
def test_montage_keeps_50p_4_3_and_sound_in_sync_across_pieces(tmp_path):
    from minidv_archiver.media import ffprobe, montage_mp4
    pieces = []
    # MiniDV-like 50p anamorphic pieces, sound a bit shorter / longer than the picture
    for i, (vs, as_) in enumerate([(2.0, 1.9), (2.0, 2.15), (1.5, 1.37), (2.0, 2.0)]):
        p = tmp_path / f"s{i}.mp4"
        _flash_and_beep(p, at=0.5, video_s=vs, audio_s=as_)
        pieces.append({"path": p, "kind": "dv"})
    out = tmp_path / "montage.mp4"
    montage_mp4(pieces, out, width=320, height=180)
    v = next(x for x in ffprobe(out)["streams"] if x["codec_type"] == "video")
    assert v["r_frame_rate"] == "50/1" and (v["width"], v["height"]) == (320, 180)
    flashes, beeps = _events(out)
    assert len(flashes) == len(beeps) == 4
    assert all(abs(f - b) < 0.04 for f, b in zip(flashes, beeps)), (flashes, beeps)   # within 2 frames, no drift
    assert abs(flashes[-1] - (2.0 + 2.0 + 1.5 + 0.5)) < 0.06                          # pieces keep their length
    # a 4:3 picture made of 16:15 pixels fills 240 of the 320 columns (not 225 = squeezed)
    raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{flashes[0] + 0.04:.2f}", "-i", str(out), "-frames:v", "1",
                          "-f", "rawvideo", "-pix_fmt", "gray", "-"], capture_output=True, check=True).stdout
    row = raw[90 * 320:91 * 320]
    assert 236 <= sum(1 for px in row if px > 128) <= 244


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
def test_montage_without_50p_sources_stays_25p(tmp_path):
    from minidv_archiver.media import ffprobe, montage_mp4
    p = tmp_path / "phone.mp4"
    _flash_and_beep(p, at=0.2, video_s=1.0, audio_s=1.0, rate=30, sar="1/1")
    out = tmp_path / "m.mp4"
    montage_mp4([{"path": p, "kind": "video"}], out, width=320, height=180)
    v = next(x for x in ffprobe(out)["streams"] if x["codec_type"] == "video")
    assert v["r_frame_rate"] == "25/1"


def test_old_dates_in_names_count_but_clock_resets_do_not():
    assert ml.date_from_name("Wesele 25.04.1987") == "1987-04-25"
    assert ml.date_from_name("IMG_19870425_160000.jpg") == "1987-04-25T16:00:00"
    assert ml.date_from_name("IMG_19800101_000012.jpg") is None          # a reset camera clock
    assert ml._exif_dt("1987:04:25 12:00:00") is None                    # no digital camera then


def test_scans_and_tape_transfers_are_recognised():
    assert ml.analog_origin("/z/fotki/stare zdjęcia, skany/Picture 014.jpg")
    assert ml.analog_origin("/z/fotki/2010-06-08 (H)/Skanuj0013.jpg")
    assert ml.analog_origin("/z/kasety VHS/wakacje.mpg")
    assert ml.analog_origin("/z/x/IMG_1.jpg", "EPSON Perfection V600")
    assert ml.analog_origin("/z/x/IMG_1.jpg", None, "EPSON Scan")
    assert not ml.analog_origin("/z/Skandynawia 2015/IMG_1.jpg", "Canon EOS 5D")
    assert not ml.analog_origin("/z/2019/IMG_2.jpg", "Apple iPhone 12")


def test_a_scan_is_dated_by_its_name_or_folder_never_by_when_it_was_scanned():
    p = pathlib.Path("/z/Wesele 25.04.1987/skany/Skanuj0001.jpg")
    assert ml._pick_date(p, "2012-03-04T10:00:00", "exif", True) == ("1987-04-25", "folder")
    # the folder is only the scanning session (same time as the scanner's clock): no date
    p = pathlib.Path("/z/2010-06-08 (H)/szkoła Skany/Bez_tytułu_3.jpg")
    assert ml._pick_date(p, "2010-06-08T11:00:00", "exif", True) == (None, "scan")
    # a camera's own clock still wins for camera files
    p = pathlib.Path("/z/Wesele 25.04.1987/IMG_0001.jpg")
    assert ml._pick_date(p, "2012-03-04T10:00:00", "exif", False) == ("2012-03-04T10:00:00", "exif")


def test_files_indexed_before_are_redated_once(tmp_path):
    cfg = _cfg(tmp_path, tmp_path / "z")
    s = JobStore(cfg.state / "jobs.db")
    with s._lock:
        for path, taken, fix in [("/z/stare zdjęcia, skany/Picture 012.jpg", "2012-05-05T10:00:00", None),
                                 ("/z/Ślub 12.06.1979/skany/a.jpg", "2012-05-05T10:00:00", None),
                                 ("/z/stare zdjęcia, skany/b.jpg", "2012-05-05T10:00:00", "1975-01-01"),
                                 ("/z/2019/IMG_1.jpg", "2019-05-05T10:00:00", None)]:
            s._conn.execute("INSERT INTO media (path, album, name, kind, status, taken, date_source, date_fix) "
                            "VALUES (?, 'a', ?, 'photo', 'ready', ?, 'exif', ?)",
                            (path, path.rsplit("/", 1)[1], taken, fix))
    assert len(ml.redate_analog(s)) == 2
    rows = {r["path"]: r for r in s._media_rows()}
    scan = rows["/z/stare zdjęcia, skany/Picture 012.jpg"]
    assert scan["taken"] is None and scan["date_source"] == "scan"
    assert rows["/z/Ślub 12.06.1979/skany/a.jpg"]["taken"] == "1979-06-12"
    assert rows["/z/stare zdjęcia, skany/b.jpg"]["taken"] == "2012-05-05T10:00:00"   # fixed by hand: untouched
    assert rows["/z/2019/IMG_1.jpg"]["taken"] == "2019-05-05T10:00:00"
    assert ml.redate_analog(s) == []
