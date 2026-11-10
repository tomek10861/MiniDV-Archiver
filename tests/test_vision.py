import json
import os
import shutil
import subprocess
import time

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("PIL")
needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")

from minidv_archiver import media_library as ml  # noqa: E402
from minidv_archiver import vision  # noqa: E402
from minidv_archiver.config import Config  # noqa: E402
from minidv_archiver.store import JobStore  # noqa: E402

AXES = {"red": 0, "green": 1, "blue": 2, "white": 3}


class FakeEncoder:
    """A picture becomes the direction of its main colour; a word its colour's
    direction. Enough to check what the index and the search do with vectors."""

    def image(self, rgb):
        v = np.zeros(vision.DIM, np.float32)
        r, g, b = (float(x) for x in rgb.reshape(-1, 3).mean(axis=0))
        if min(r, g, b) > 200:
            v[3] = 1
        else:
            v[int(np.argmax([r, g, b]))] = 1
        return v

    def text(self, q):
        v = np.zeros(vision.DIM, np.float32)
        v[AXES.get(q.split()[-1].lower(), 10)] = 1
        return v


@pytest.fixture
def vocab(monkeypatch):
    tags = [{"id": c, "pl": c, "en": c, "cat": "things", "prompts": [f"something {c}"]} for c in ("red", "blue")]
    monkeypatch.setattr(vision, "TAGS", tags)
    return tags


def _age(p):
    t = time.time() - 3600
    os.utime(p, (t, t))


def _video(path, colours, seconds=4):
    """Colour pictures with a thin grid (a flat colour counts as blank); black stays blank."""
    inputs = [x for c in colours for x in ("-f", "lavfi", "-i", f"color=c={c}:s=320x240:r=25:d={seconds}")]
    grid = ";".join(f"[{i}]{'null' if c == 'black' else 'drawgrid=w=32:h=32:t=2:c=gray'}[g{i}]"
                    for i, c in enumerate(colours))
    subprocess.run(["ffmpeg", "-v", "error", "-y", *inputs, "-filter_complex",
                    grid + ";" + "".join(f"[g{i}]" for i in range(len(colours)))
                    + f"concat=n={len(colours)}:v=1,format=yuv420p", "-c:v", "libx264", str(path)], check=True)


def _photo(path, rgb):
    from PIL import Image, ImageDraw
    im = Image.new("RGB", (640, 480), rgb)
    d = ImageDraw.Draw(im)
    for x in range(0, 640, 64):
        d.line((x, 0, x, 480), fill=(128, 128, 128), width=4)
    im.save(path)


def _library(tmp_path):
    src = tmp_path / "zdjecia"
    src.mkdir()
    _photo(src / "IMG_20050101_120000.jpg", (220, 30, 30))       # red
    _photo(src / "IMG_20100101_120000.jpg", (30, 200, 40))       # green
    _video(src / "VID_20080601_120000.mp4", ["red", "black", "blue", "blue"])
    for p in src.iterdir():
        _age(p)
    cfg = Config(storage=tmp_path / "st", media_dirs=str(src), default_recording_tz="Europe/Warsaw")
    cfg.ensure_dirs()
    s = JobStore(cfg.state / "jobs.db")
    ml.scan(cfg, s)
    ml.process_pending(cfg, s)
    ids = {r["name"]: r["id"] for r in s.media_ready()}
    return cfg, s, src, ids


@needs_ffmpeg
def test_frames_are_taken_when_the_picture_changes_and_blank_ones_skipped(tmp_path):
    v = tmp_path / "v.mp4"
    _video(v, ["red", "black", "blue", "blue", "blue", "blue"])          # 24 s
    got = [(a, b) for a, b, _ in vision.sample_frames(str(v), photo=False, step=2, max_gap=10, nice=False)]
    # red 0–4; black 4–8 skipped; blue from 8, the same picture looked at again 10 s later,
    # lasting to the end
    assert got == [(0, 4), (8, 18), (18, 24)]


def test_tag_runs_join_close_frames_and_split_far_ones():
    ts = [0, 2, 4, 20, 22]
    spans = [(t, t + 2) for t in ts]
    sims = np.array([[0.2], [0.2], [0.01], [0.2], [0.15]])
    assert vision.tag_ranges(spans, sims, ["x"], 0.1, 2.0) == [("x", 0, 4, 0.2), ("x", 20, 24, 0.2)]


@needs_ffmpeg
def test_index_tags_and_search_by_text_tag_kind_date_and_people(tmp_path, vocab):
    cfg, s, src, ids = _library(tmp_path)
    svc = vision.VisionService(cfg, s, encoder=FakeEncoder())
    assert svc.index_once() == 3
    assert svc.db.counts() == {"scenes": 3, "frames": 4}                 # 1 + 1 + red, blue (black skipped)
    assert svc.index_once() == 0                                       # nothing new
    red = svc.search({"q": "red"})["results"]
    assert {(r["kind"], r["media_id"]) for r in red} == {("photo", ids["IMG_20050101_120000.jpg"]),
                                                          ("video", ids["VID_20080601_120000.mp4"])}
    clip = next(r for r in red if r["kind"] == "video")
    assert clip["moments"] == [[0.0, 4.0]] and clip["tags"] == ["blue", "red"]
    assert [r["kind"] for r in svc.search({"q": "red", "kinds": ["photo"]})["results"]] == ["photo"]
    assert svc.search({"q": "red", "date_to": "2006"})["results"][0]["date"] == "2005-01-01"
    # by tag alone: where it is in the film
    blue = svc.search({"tags": ["blue"]})["results"]
    assert [(r["media_id"], r["moments"]) for r in blue] == [(ids["VID_20080601_120000.mp4"], [[8.0, 16.0]])]
    assert svc.search({"tags": ["red"], "not_tags": ["blue"]})["results"][0]["kind"] == "photo"
    # people: who must / mustn't be there
    with s._lock:
        s._conn.execute("INSERT INTO face_tracks (person_id, workspace, tape_id, scene_id, embedding, quality, times) "
                        "VALUES (7, 'default', ?, ?, x'00', 1, '[1, 2]')", (ml.MEDIA_TAPE, str(ids["VID_20080601_120000.mp4"])))
    assert [r["kind"] for r in svc.search({"q": "red", "with": [7]})["results"]] == ["video"]
    assert [r["kind"] for r in svc.search({"q": "red", "without": [7]})["results"]] == ["photo"]
    only = svc.search({"with": [7]})["results"]
    assert only[0]["moments"] == [[1.0, 3.0]] and only[0]["people"] == [7]
    assert {t["id"]: t["count"] for t in svc.tags()} == {"red": 2, "blue": 1}
    assert svc.search({})["results"] == []                             # nothing asked, nothing listed


@needs_ffmpeg
def test_changed_and_deleted_files_and_pausing(tmp_path, vocab):
    cfg, s, src, ids = _library(tmp_path)
    svc = vision.VisionService(cfg, s, encoder=FakeEncoder())
    s.set_meta("vision_paused", "1")
    assert svc.index_once() == 0 and s.get_build(vision.VISION_KEY)["status"] == "PAUSED"
    s.set_meta("vision_paused", "0")
    svc.index_once()
    # the red photo becomes blue (its thumbnail changes) -> looked at again
    photo = ids["IMG_20050101_120000.jpg"]
    thumb = ml.derived(cfg, "thumbs", photo, ".jpg")
    _photo(thumb, (20, 20, 230))
    os.utime(thumb, (time.time() + 5, time.time() + 5))
    assert svc.index_once() == 1
    assert ml.MEDIA_TAPE in {r["tape_id"] for r in svc.search({"q": "blue", "kinds": ["photo"]})["results"]}
    # the video disappears from the share -> out of the index
    (src / "VID_20080601_120000.mp4").unlink()
    ml.scan(cfg, s)
    svc.index_once()
    assert svc.db.counts()["scenes"] == 2 and not svc.search({"tags": ["blue"], "kinds": ["video"]})["results"]


@needs_ffmpeg
def test_a_new_vocabulary_retags_from_the_stored_vectors(tmp_path, vocab, monkeypatch):
    cfg, s, src, ids = _library(tmp_path)
    svc = vision.VisionService(cfg, s, encoder=FakeEncoder())
    svc.index_once()
    monkeypatch.setattr(vision, "TAGS", vocab + [{"id": "white", "pl": "w", "en": "w", "cat": "things",
                                                  "prompts": ["something green"]}])
    calls = []
    monkeypatch.setattr(vision, "sample_frames", lambda *a, **k: calls.append(a) or iter(()))
    assert svc.retag_all() == 3 and not calls                         # no frame decoded again
    assert svc.search({"tags": ["white"]})["results"][0]["media_id"] == ids["IMG_20100101_120000.jpg"]


def test_models_are_checked_before_use(tmp_path, monkeypatch):
    monkeypatch.setattr(vision, "MODEL_FILES", {"m.onnx": ("m.onnx", "0" * 64)})
    with pytest.raises(RuntimeError, match="sha256"):
        vision.fetch_models(tmp_path, download=lambda url, dest: dest.write_bytes(b"not the model"))
    assert not list(tmp_path.glob("m.onnx*"))
    import hashlib
    monkeypatch.setattr(vision, "MODEL_FILES", {"m.onnx": ("m.onnx", hashlib.sha256(b"ok").hexdigest())})
    calls = []
    for _ in range(2):
        vision.fetch_models(tmp_path, download=lambda url, dest: calls.append(url) or dest.write_bytes(b"ok"))
    assert len(calls) == 1 and calls[0].endswith("/m.onnx")


@needs_ffmpeg
def test_frame_for_a_result_card(tmp_path):
    cfg, s, src, ids = _library(tmp_path)
    vid = ids["VID_20080601_120000.mp4"]
    p = vision.frame_jpg(cfg, s, ml.MEDIA_TAPE, str(vid), 9.0)
    assert p and p.exists()
    from PIL import Image
    r, g, b = Image.open(p).convert("RGB").resize((1, 1)).getpixel((0, 0))
    assert b > 150 and r < 80                                          # blue part of the film
    assert vision.frame_jpg(cfg, s, "../etc", "passwd", 0) is None
