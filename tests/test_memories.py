import json
from array import array
import shutil
import subprocess

import pytest

from minidv_archiver import memories as mem
from minidv_archiver.config import Config
from minidv_archiver.store import JobStore

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")


def test_captions_in_polish_and_english():
    assert mem.caption("1987-04-25") == "25 kwietnia 1987"
    assert mem.caption("2004-07") == "lipiec 2004"
    assert mem.caption("1999") == "1999"
    assert mem.caption("1987-04-25", "en") == "April 25, 1987"
    assert mem.caption(None) is None and mem.caption("unknown") is None


def _cand(i, date, kind="dv", score=1.0, scene=None, start=0.0, length=5.0):
    return {"tape_id": "T", "scene_id": scene or f"s{i}", "date": date, "time": None, "kind": kind,
            "path": f"/x/{i}.mp4", "start": start, "length": length, "duration": 60.0, "score": score}


def test_pick_spreads_over_the_years_in_recording_order():
    # 2004 has many great moments, 1990 / 1995 only a few weaker ones: they still get in
    cands = [_cand(i, f"2004-05-{i % 28 + 1:02d}", score=1.0) for i in range(30)]
    cands += [_cand(100, "1990-06-01", score=0.3), _cand(101, "1995-01-02", score=0.2)]
    got = mem.pick(cands, 40, seed=1)
    years = [c["date"][:4] for c in got]
    assert "1990" in years and "1995" in years
    assert years == sorted(years)                                  # chronological
    assert 40 <= sum(c["length"] - mem.XFADE for c in got) < 50


def test_pick_limits_moments_per_scene_and_photos():
    cands = [_cand(i, "2001-01-01", scene="one", start=i * 12.0, score=1.0) for i in range(6)]
    cands += [_cand(10 + i, f"2001-02-{i + 1:02d}", kind="photo", score=2.0, length=3.2) for i in range(10)]
    cands += [_cand(30 + i, f"2001-03-{i + 1:02d}", score=0.5) for i in range(6)]
    got = mem.pick(cands, 60, seed=3)
    assert sum(1 for c in got if c["scene_id"] == "one") <= 2
    photos = sum(1 for c in got if c["kind"] == "photo")
    assert 0 < photos <= 0.4 * len(got) + 1


def test_another_film_of_the_same_person_picks_other_moments():
    cands = [_cand(i, f"20{i % 20:02d}-01-01", score=1.0) for i in range(60)]
    first = mem.pick(cands, 30, seed=5)
    second = mem.pick(cands, 30, seed=6, avoid=mem.used_keys([first]))
    overlap = {(c["scene_id"], c["start"]) for c in first} & {(c["scene_id"], c["start"]) for c in second}
    assert len(overlap) <= len(first) // 3


def test_pick_honours_period_and_sources():
    cands = [_cand(1, "1990-01-01"), _cand(2, "2000-01-01"), _cand(3, "2010-01-01", kind="photo"),
             _cand(4, "2010-06-01", kind="video")]
    assert {c["date"][:4] for c in mem.pick(cands, 100, seed=0, date_from="1995", date_to="2010")} == {"2000", "2010"}
    assert {c["kind"] for c in mem.pick(cands, 100, seed=0, sources=("photo",))} == {"photo"}


def test_pieces_last_whole_beats_and_the_film_fits_the_music():
    items = [_cand(i, "2000-01-01", length=4.0 + i % 3, score=i) for i in range(20)]
    fitted = mem.fit_to_beats(items, 0.5, 0.0, music_s=60.0)
    for c in fitted:
        beats = (c["d"] - mem.XFADE) / 0.5
        assert abs(beats - round(beats)) < 1e-6 and c["d"] >= mem.MIN_PIECE
    assert mem.total_length(fitted) <= 60.0 - mem.END_S + mem.XFADE + 1e-6
    assert min(c["score"] for c in fitted) >= min(c["score"] for c in items)   # the weakest went first


@needs_ffmpeg
def test_beat_grid_finds_the_tempo_of_a_click_track(tmp_path):
    pytest.importorskip("numpy")
    click = tmp_path / "click.wav"
    # 120 BPM: a 20 ms click every 0.5 s, the first at 0.25 s
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                    "aevalsrc='if(lt(mod(t-0.25+10,0.5),0.02),sin(2*PI*1500*t),0)':s=44100:d=20", str(click)],
                   check=True)
    period, offset = mem.beat_grid(click)
    assert abs(period - 0.5) < 0.02
    assert min((offset - 0.25) % 0.5, (0.25 - offset) % 0.5) < 0.03       # on the clicks, within 30 ms
    assert mem.beat_grid(click) == (period, offset)                      # cached


def test_search_keeps_reusable_music_only():
    def fake(url):
        assert "license=cc0%2Cpdm%2Cby%2Cby-sa" in url and "category=music" in url
        return {"results": [
            {"id": "7bdb9782-2d15-4680-99b0-204b39d66166", "title": "Song", "creator": "A", "license": "by",
             "license_version": "3.0", "url": "https://prod-1.storage.jamendo.com/?trackid=1&format=mp32",
             "foreign_landing_url": "https://www.jamendo.com/track/1", "source": "jamendo", "duration": 180000,
             "attribution": "\"Song\" by A is licensed under CC BY 3.0."},
            {"id": "00000000-0000-0000-0000-000000000000", "title": "Jingle", "url": "https://x/y.mp3",
             "duration": 12000, "license": "cc0"}]}
    got = mem.search("happy", fetch=fake)
    assert [t["id"] for t in got] == ["ov-7bdb9782-2d15-4680-99b0-204b39d66166"]   # 12 s jingle left out
    assert got[0]["credit"].startswith('"Song" by A') and got[0]["license"] == "CC BY 3.0"


def test_tracks_are_fetched_by_id_only(tmp_path):
    cfg = Config(storage=tmp_path)
    for bad in ("https://evil/x.mp3", "../etc/passwd", "ov-not-a-uuid", "kml-nope"):
        with pytest.raises(ValueError):
            mem.ensure_track(cfg, bad, fetch=lambda u: pytest.fail("no network"),
                             download=lambda u, d: pytest.fail("no download"))


@needs_ffmpeg
def test_catalogue_track_is_downloaded_once_and_own_files_are_listed(tmp_path):
    cfg = Config(storage=tmp_path)
    calls = []

    def download(url, dest):
        calls.append(url)
        assert url.startswith("https://incompetech.com/")
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=d=2", "-f", "mp3", str(dest)], check=True)
    tid = mem.builtin_catalogue()[0]["id"]
    t, p = mem.ensure_track(cfg, tid, download=download)
    assert p.exists() and t["credit"].startswith("„Heartwarming” Kevin MacLeod")
    mem.ensure_track(cfg, tid, download=download)
    assert len(calls) == 1
    own = tmp_path / "music" / "own"
    own.mkdir(parents=True)
    shutil.copy(p, own / "Nasza piosenka.mp3")
    listed = {t["id"]: t for t in mem.catalogue(cfg)}
    assert listed[tid]["cached"] and listed["own-nasza-piosenka"]["source"] == "own"
    assert mem.ensure_track(cfg, "own-nasza-piosenka")[1].name == "Nasza piosenka.mp3"


def _clip(path, *, size, rate, seconds, sar="1/1", tone=None):
    audio = (["-f", "lavfi", "-i", f"sine=frequency={tone}:r=48000:d={seconds}"] if tone else [])
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc=size={size}:rate={rate}:d={seconds}",
                    *audio, "-vf", f"setsar={sar}", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    *(["-c:a", "aac"] if tone else []), str(path)], check=True)


def _band_level(path, freq, start, length):
    out = subprocess.run(["ffmpeg", "-v", "info", "-ss", str(start), "-t", str(length), "-i", str(path), "-af",
                          f"bandpass=f={freq}:w=300,astats=metadata=0", "-vn", "-f", "null", "-"],
                         capture_output=True, text=True).stderr
    levels = [float(line.split(":")[-1]) for line in out.splitlines() if "RMS level dB" in line
              and "-inf" not in line]
    return levels[-1]


@needs_ffmpeg
def test_render_makes_a_50p_film_with_ducked_music(tmp_path):
    pytest.importorskip("PIL")
    pytest.importorskip("numpy")
    from PIL import Image
    from minidv_archiver.media import ffprobe
    talk = tmp_path / "dv.mp4"               # 4:3 anamorphic MiniDV-like piece, somebody "talking" (1 kHz)
    _clip(talk, size="720x576", rate=50, seconds=8, sar="16/15", tone=1000)
    phone = tmp_path / "phone.mp4"           # vertical, no sound
    _clip(phone, size="180x320", rate=30, seconds=6)
    photo = tmp_path / "p.jpg"
    Image.new("RGB", (400, 300), "orange").save(photo)
    music = tmp_path / "m.wav"               # music only at 6 kHz, so it can be told apart from the talk
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=6000:r=48000:d=60", str(music)],
                   check=True)
    plan = [{"kind": "dv", "path": str(talk), "start": 0.0, "length": 6, "duration": 8.0, "date": "1987-04-25",
             "score": 1},
            {"kind": "photo", "path": str(photo), "start": 0.0, "length": 3.2, "date": "1987-04-25", "score": 1},
            {"kind": "video", "path": str(phone), "start": 0.0, "length": 5, "duration": 6.0, "date": "2019-08-01",
             "score": 1}]
    items = mem.fit_to_beats(plan, 0.5, 0.0, 60.0)
    out = tmp_path / "film.mp4"
    seen = []
    dur = mem.render(Config(storage=tmp_path), items, out, title="Mama", subtitle="1987–2019",
                     music=({"credit": "„Test” — CC0"}, music), w=320, h=180, progress=seen.append, nice=False)
    info = ffprobe(out)
    v = next(s for s in info["streams"] if s["codec_type"] == "video")
    assert v["r_frame_rate"] == "50/1" and (v["width"], v["height"]) == (320, 180)
    assert abs(float(info["format"]["duration"]) - dur) < 0.15
    assert abs(dur - mem.total_length(items)) < 1e-6
    assert seen and seen[-1] > 0.95 and seen == sorted(seen)
    assert "CC0" in info["format"]["tags"].get("comment", "")
    # while the first piece "talks" the music is held down; over the silent photo it comes back up
    during_talk = _band_level(out, 6000, 2.0, 2.0)
    photo_at = items[0]["d"] - mem.XFADE
    during_photo = _band_level(out, 6000, photo_at + 1.0, 1.2)
    assert during_photo - during_talk > 6, (during_talk, during_photo)


@needs_ffmpeg
def test_engine_films_a_person_and_swaps_the_music(tmp_path, monkeypatch):
    pytest.importorskip("PIL")
    pytest.importorskip("numpy")
    from PIL import Image
    from minidv_archiver import media_library as ml
    from minidv_archiver.engine import Engine
    src = tmp_path / "zdjecia"
    src.mkdir()
    for i, (name, colour) in enumerate([("IMG_20000101_120000.jpg", "red"), ("IMG_20050101_120000.jpg", "blue"),
                                        ("IMG_20100101_120000.jpg", "green")]):
        Image.new("RGB", (640, 480), colour).save(src / name)
    vid = src / "VID_20080601_120000.mp4"
    _clip(vid, size="320x240", rate=25, seconds=10, tone=500)
    import os
    import time
    for p in src.iterdir():
        t = time.time() - 3600
        os.utime(p, (t, t))
    cfg = Config(storage=tmp_path / "st", media_dirs=str(src), default_recording_tz="Europe/Warsaw")
    cfg.ensure_dirs()
    e = Engine(cfg)
    ml.scan(cfg, e.store)
    ml.process_pending(cfg, e.store)
    rows = e.store.media_ready()
    with e.store._lock:
        pid = e.store._conn.execute("INSERT INTO face_persons (workspace, name) VALUES ('default', 'Mama')").lastrowid
        for r in rows:
            times = [2, 3, 4] if r["kind"] == "video" else [0]
            e.store._conn.execute("INSERT INTO face_tracks (person_id, workspace, tape_id, scene_id, embedding, "
                                  "quality, times) VALUES (?, 'default', ?, ?, ?, 0.9, ?)",
                                  (pid, ml.MEDIA_TAPE, str(r["id"]), array("f", [1.0] * 128).tobytes(),
                                   json.dumps(times)))

    def fake_track(config, tid, **_):
        p = config.storage / "music" / "cache" / f"{tid}.wav"
        p.parent.mkdir(parents=True, exist_ok=True)
        if not p.exists():
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                            "aevalsrc='if(lt(mod(t,0.5),0.02),sin(2*PI*1500*t),0)':s=44100:d=90", str(p)], check=True)
        return {"id": tid, "title": tid, "artist": "x", "credit": f"„{tid}” CC0", "mood": "happy"}, p
    monkeypatch.setattr(mem, "ensure_track", fake_track)
    monkeypatch.setattr(mem, "W", 320)
    monkeypatch.setattr(mem, "H", 180)
    def wait(mid):
        for _ in range(600):
            m = e.store.memory_get(mid)
            if m["status"] in ("READY", "ERROR"):
                return m
            time.sleep(0.2)
        pytest.fail("film not made")
    made = e.create_memory(pid, {"length": "short", "music": "kml-carefree"})
    assert made["status"] == "QUEUED"                  # the build worker renders it
    assert wait(made["id"])["status"] == "READY", e.store.memory_get(made["id"])["error"]
    m = e.memories_of(pid)[0]
    assert m["status"] == "READY" and m["pieces"] >= 3 and m["music"]["id"] == "kml-carefree"
    first = e.memory_file(m["id"])
    assert first and first.exists() and e.memory_file(m["id"], poster=True).exists()
    plan = e.store.memory_get(m["id"])["plan"]
    # other music: the same moments, a new file, the old one gone
    e.memory_music(m["id"], "kml-wallpaper")
    again = wait(m["id"])
    assert again["plan"] == plan and again["music"]["id"] == "kml-wallpaper" and not first.exists()
    with e.store._lock:
        other = e.store._conn.execute("INSERT INTO face_persons (workspace, name) VALUES ('default', 'Ania')").lastrowid
    e.merge_persons(pid, other)                       # merged into someone: the films follow
    assert [x["id"] for x in e.memories_of(other)] == [m["id"]]
    e.delete_persons([other])                         # deleted: the films go too
    assert not e.memories_of(other) and not list((cfg.storage / "memories").glob("*.*"))
