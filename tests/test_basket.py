import csv
import io
import os
import shutil
import subprocess
import time
import zipfile

import pytest

pytest.importorskip("PIL")
needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")

from minidv_archiver import media_library as ml  # noqa: E402
from minidv_archiver.config import Config  # noqa: E402
from minidv_archiver.engine import Engine  # noqa: E402


@needs_ffmpeg
def test_basket_keeps_moments_and_exports_clips_with_a_list(tmp_path):
    from PIL import Image
    src = tmp_path / "zdjecia"
    src.mkdir()
    Image.new("RGB", (640, 480), "red").save(src / "IMG_20050101_120000.jpg")
    film = src / "VID_20080601_120000.mp4"            # 50p, 20 s, with sound
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=50:d=20", "-f", "lavfi",
                    "-i", "sine=d=20", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(film)], check=True)
    for p in src.iterdir():
        t = time.time() - 3600
        os.utime(p, (t, t))
    cfg = Config(storage=tmp_path / "st", media_dirs=str(src), default_recording_tz="Europe/Warsaw")
    cfg.ensure_dirs()
    e = Engine(cfg)
    ml.scan(cfg, e.store)
    ml.process_pending(cfg, e.store)
    ids = {r["kind"]: str(r["id"]) for r in e.store.media_ready()}
    with e.store._lock:
        pid = e.store._conn.execute("INSERT INTO face_persons (workspace, name) VALUES ('default', 'Ania')").lastrowid
        e.store._conn.execute("INSERT INTO face_tracks (person_id, workspace, tape_id, scene_id, embedding, quality, "
                              "times) VALUES (?, 'default', ?, ?, x'00', 1, '[6, 7]')", (pid, ml.MEDIA_TAPE, ids["video"]))
    e.basket_add(None, [{"tape_id": ml.MEDIA_TAPE, "scene_id": ids["video"], "start": 5, "end": 8},
                        {"tape_id": ml.MEDIA_TAPE, "scene_id": ids["video"], "start": 7, "end": 10},   # widens the first
                        {"tape_id": ml.MEDIA_TAPE, "scene_id": ids["video"], "start": 15, "end": 16},
                        {"tape_id": ml.MEDIA_TAPE, "scene_id": ids["photo"], "start": 0, "end": 0}])
    items = e.basket(None)
    assert [(i["start"], i["end"]) for i in items] == [(5, 10), (15, 16), (0, 0)]
    assert items[0]["kind"] == "video" and items[0]["date"] == "2008-06-01"
    with pytest.raises(ValueError):
        e.basket_add(None, [{"tape_id": "../x", "scene_id": "1"}])
    assert e.basket("other") == []                                   # per workspace

    build = e.basket_export(None)
    for _ in range(300):
        b = e.basket_export_status(build["token"])
        if b["status"] in ("READY", "ERROR"):
            break
        time.sleep(0.2)
    assert b["status"] == "READY", b.get("error")
    from minidv_archiver.media import ffprobe
    with zipfile.ZipFile(b["path"]) as zf:
        names = zf.namelist()
        rows = list(csv.reader(io.StringIO(zf.read("lista.csv").decode("utf-8-sig")), delimiter=";"))
        clip = tmp_path / "c.mp4"
        clip.write_bytes(zf.read(names[0]))
    assert len(names) == 4 and names[2].endswith(".jpg")
    assert rows[1][0] == names[0] and rows[1][3] == "film" and rows[1][6:8] == ["4.0", "11.0"]   # 1 s each side
    assert rows[1][8] == "Ania" and rows[2][8] == ""                # she's in 6–7 s only
    info = ffprobe(clip)
    v = next(x for x in info["streams"] if x["codec_type"] == "video")
    assert v["r_frame_rate"] == "50/1" and abs(float(info["format"]["duration"]) - 7.0) < 0.15

    e.basket_remove(None, [items[1]["id"]])
    assert len(e.basket(None)) == 2
    e.basket_remove(None, None)
    assert e.basket(None) == []
    with pytest.raises(FileNotFoundError):
        e.basket_export(None)
