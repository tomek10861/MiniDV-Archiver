import json
from array import array
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from minidv_archiver import faces
from minidv_archiver.config import Config
from minidv_archiver.engine import Engine
from minidv_archiver.store import FACES_KEY, JobStore


def _store(tmp_path):
    return JobStore(tmp_path / "jobs.db")


def _emb(*xs):
    """A tiny normalized embedding (the store and the stdlib helpers don't care about length)."""
    return faces._normalized(array("f", xs)).tobytes()


def _scene(store, tape, scene, ws, person, times=(1, 2)):
    return store.face_save_scene(tape, scene, faces.MODEL_TAG, [
        {"person_id": person, "workspace": ws, "embedding": _emb(1, 0, 0), "quality": 10.0, "times": list(times)}])


def test_face_scan_is_a_singleton_and_cancellable(tmp_path):
    s = _store(tmp_path)
    s.enqueue_face_scan("manual")
    with pytest.raises(RuntimeError, match="już trwa"):
        s.enqueue_face_scan("cron")               # cron (or a second click) can't start another
    b = s.claim_face_scan()
    assert b["status"] == "RUNNING"
    with pytest.raises(RuntimeError):
        s.enqueue_face_scan("manual")
    s.request_build_cancel(FACES_KEY)
    assert s.build_cancelled(FACES_KEY)
    # the worker writing its own (stale, cancel=False) copy must not wipe the request
    b["progress"] = 0.5
    s.put_build(b)
    assert s.build_cancelled(FACES_KEY)
    b.update(status="CANCELLED", cancel=False)
    s.put_build(b)
    assert s.enqueue_face_scan("manual")["status"] == "QUEUED"   # finished -> a new one may start


def test_cancelling_a_queued_scan_ends_it_immediately(tmp_path):
    s = _store(tmp_path)
    s.enqueue_face_scan("manual")
    assert s.request_build_cancel(FACES_KEY)["status"] == "CANCELLED"
    assert s.claim_face_scan() is None


def test_regular_build_queue_never_claims_the_face_scan(tmp_path):
    s = _store(tmp_path)
    s.enqueue_face_scan("manual")
    assert s.claim_next_build() is None
    s.put_build({"key": "T/TAPE", "tape_id": "T", "token": "TAPE", "mode": "share", "status": "QUEUED", "path": "x"})
    assert s.claim_next_build()["key"] == "T/TAPE"
    assert s.get_build(FACES_KEY)["status"] == "QUEUED"


def test_persons_never_cross_workspaces(tmp_path):
    s = _store(tmp_path)
    a = s.face_person_create("default", _emb(1, 0, 0), 1)
    k = s.face_person_create("Kasia", _emb(1, 0, 0), 1)
    _scene(s, "TAPE-A", "0001", "default", a)
    ids = _scene(s, "TAPE-K", "0001", "Kasia", k)
    assert [p["id"] for p in s.face_persons("default")] == [a]
    assert [p["id"] for p in s.face_persons("Kasia")] == [k]
    with pytest.raises(ValueError, match="workspace"):
        s.face_merge(k, a)
    with pytest.raises(ValueError, match="workspace"):
        s.face_move_track(ids[0], a)


def test_merge_keeps_a_name_and_bumps_the_generation(tmp_path):
    s = _store(tmp_path)
    a = s.face_person_create("default")
    b = s.face_person_create("default")
    s.face_person_update(b, name="Ola")
    _scene(s, "T", "0001", "default", a)
    _scene(s, "T", "0002", "default", b)
    gen = s.faces_gen()
    s.face_merge(b, a)
    assert s.faces_gen() == gen + 1
    assert s.face_person(a)["name"] == "Ola" and s.face_person(b) is None
    assert {t["scene_id"] for t in s.face_tracks(a)} == {"0001", "0002"}


def test_save_scene_refuses_a_person_that_vanished(tmp_path):
    s = _store(tmp_path)
    assert _scene(s, "T", "0001", "default", 999) is None
    assert ("T", "0001") not in s.face_scanned_keys(faces.MODEL_TAG)


def test_drop_tracks_forgets_scenes_and_unnamed_leftovers(tmp_path):
    s = _store(tmp_path)
    a = s.face_person_create("default")
    named = s.face_person_create("default")
    s.face_person_update(named, name="Ola")
    _scene(s, "T", "0001", "default", a)
    _scene(s, "T", "0002", "default", named)
    ids, persons = s.face_drop_tracks("T", ["0001"])
    assert len(ids) == 1 and persons == {a}
    assert s.face_person(a) is None                  # unnamed and empty -> gone
    s.face_drop_tracks("T")
    assert s.face_person(named)["name"] == "Ola"     # a named person survives losing every track
    assert s.face_scanned_keys(faces.MODEL_TAG) == set()


def test_rename_tape_carries_tracks_along(tmp_path):
    s = _store(tmp_path)
    a = s.face_person_create("default")
    _scene(s, "OLD", "0001", "default", a)
    s.face_rename_tape("OLD", "NEW")
    assert s.face_tracks(a)[0]["tape_id"] == "NEW"
    assert ("NEW", "0001") in s.face_scanned_keys(faces.MODEL_TAG)


def test_recompute_person_is_the_normalized_mean(tmp_path):
    s = _store(tmp_path)
    p = s.face_person_create("default")
    s.face_save_scene("T", "0001", faces.MODEL_TAG, [
        {"person_id": p, "workspace": "default", "embedding": _emb(1, 0), "quality": 1, "times": [0]},
        {"person_id": p, "workspace": "default", "embedding": _emb(0, 1), "quality": 1, "times": [1]}])
    faces.recompute_person(s, p)
    (_, centroid, n), = s.face_person_centroids("default")
    assert n == 2 and list(faces._vec(centroid)) == pytest.approx([0.7071, 0.7071], abs=1e-3)


def test_segments_join_nearby_seconds():
    assert faces.segments([9, 3, 4, 5, 7, 20, 21]) == [[3, 9], [20, 21]]   # gaps of <=2 s join
    assert faces.segments([]) == []


def test_daily_schedule_fires_once_inside_its_window(tmp_path):
    s = _store(tmp_path)
    cfg = Config(storage=tmp_path, faces_scan_at="03:00", default_recording_tz="Europe/Warsaw")
    tz = ZoneInfo("Europe/Warsaw")
    assert not faces.due_today(cfg, s, datetime(2026, 10, 21, 2, 59, tzinfo=tz))
    assert faces.due_today(cfg, s, datetime(2026, 10, 21, 3, 0, tzinfo=tz))
    assert not faces.due_today(cfg, s, datetime(2026, 10, 21, 14, 0, tzinfo=tz))   # restart mid-day: no scan
    faces.mark_auto_run(cfg, s, datetime(2026, 10, 21, 3, 0, tzinfo=tz))
    assert not faces.due_today(cfg, s, datetime(2026, 10, 21, 4, 0, tzinfo=tz))
    assert faces.due_today(cfg, s, datetime(2026, 10, 22, 3, 5, tzinfo=tz))
    assert not faces.due_today(Config(storage=tmp_path, faces_scan_at=""), s)


def test_engine_renaming_to_an_existing_name_merges(tmp_path):
    eng = Engine(Config(storage=tmp_path, min_free_gib=0, camera_wait_timeout=0, faces_enabled=False))
    s = eng.store
    a, b = s.face_person_create("default"), s.face_person_create("default")
    _scene(s, "T", "0001", "default", a)
    _scene(s, "T", "0002", "default", b)
    eng.update_person(a, name="Ola")
    merged = eng.update_person(b, name="  ola ")
    assert merged["id"] == a and s.face_person(b) is None
    moved = eng.move_face(s.face_tracks(a)[0]["id"], None)       # "to nie ta osoba"
    assert moved["from"] == a and s.face_person(moved["to"])["name"] is None


def test_engine_delete_tape_drops_its_faces(tmp_path):
    eng = Engine(Config(storage=tmp_path, min_free_gib=0, camera_wait_timeout=0, faces_enabled=False))
    d = eng.config.tapes / "TAPE-X"
    d.mkdir(parents=True)
    (d / "tape.json").write_text(json.dumps({"tape_id": "TAPE-X"}))
    p = eng.store.face_person_create("default")
    tid, = _scene(eng.store, "TAPE-X", "0001", "default", p)
    faces.thumb_path(eng.config, tid).write_bytes(b"jpg")
    eng.delete_tape("TAPE-X")
    assert eng.store.face_person(p) is None
    assert not faces.thumb_path(eng.config, tid).exists()


def test_matcher_assigns_within_workspace_only(tmp_path):
    np = pytest.importorskip("numpy")
    s = _store(tmp_path)
    m = faces.Matcher(s, threshold=0.4)
    e = np.array([1, 0, 0], np.float32)
    p1 = m.assign("default", e)
    assert m.assign("default", np.array([0.9, 0.1, 0], np.float32) / 0.906) == p1
    assert m.assign("Kasia", e) != p1                                 # same face, other workspace
    assert m.assign("default", np.array([0, 1, 0], np.float32)) != p1  # different face
    s.face_merge(p1, s.face_person_create("default"))                  # operator edit mid-scan
    assert m.assign("default", e) != p1                                # cache reloaded, p1 is gone
