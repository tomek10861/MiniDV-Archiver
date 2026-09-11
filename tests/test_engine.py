import json
import pathlib
import time

import minidv_archiver.engine as em
from minidv_archiver.config import Config
from minidv_archiver.engine import Engine, now


def _engine(tmp_path, **cfg):
    return Engine(Config(storage=tmp_path, min_free_gib=0, camera_wait_timeout=0, **cfg))


def test_tape_sequence(tmp_path):
    eng = _engine(tmp_path)
    assert eng._next_tape_id() == "TAPE-0001"
    (eng.config.tapes / "TAPE-0007").mkdir()
    assert eng._next_tape_id() == "TAPE-0008"
    # a failed capture leaves a working dir; its number must not be handed out again
    (eng.config.working / "TAPE-0008").mkdir()
    assert eng._next_tape_id() == "TAPE-0009"


def test_compat_override_forces_manual_when_avc_disabled():
    assert Engine._compat_override(False, manual_transport=False, rewind=True) == (True, False)
    assert Engine._compat_override(False, manual_transport=True, rewind=False) == (True, False)


def test_compat_override_passthrough_when_avc_enabled():
    assert Engine._compat_override(True, manual_transport=False, rewind=True) == (False, True)


def test_start_forces_manual_and_spawns_capture(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(Engine, "_capture_job",
                        lambda self, job, rewind, duration, manual: seen.update(rewind=rewind, manual=manual))
    eng = _engine(tmp_path, allow_fcp=False)
    job = eng.start(tape_id="TAPE-9001", rewind=True, manual_transport=False)
    time.sleep(0.1)
    assert job["manual_transport"] is True and job["stage"] == "capture"
    assert seen == {"rewind": False, "manual": True}
    assert eng.capture_tape == "TAPE-9001"


def test_second_capture_blocked_only_while_a_capture_holds_the_slot(tmp_path, monkeypatch):
    monkeypatch.setattr(Engine, "_capture_job", lambda *a, **k: None)
    eng = _engine(tmp_path, allow_fcp=True)
    eng.start(tape_id="TAPE-9001")
    try:
        eng.start(tape_id="TAPE-9002")
        assert False, "expected refusal while a capture is active"
    except RuntimeError as exc:
        assert "capture is already in progress" in str(exc)
    # processing does NOT block a new capture
    eng.capture_tape = None
    eng.store.put_job({**eng.store.get_job("TAPE-9001"), "stage": "process", "status": "ANALYZING_DV"})
    eng.processing_tape = "TAPE-9001"
    assert eng.start(tape_id="TAPE-9002")["stage"] == "capture"


def test_jobs_list_and_lookup(tmp_path):
    eng = _engine(tmp_path)
    eng.jobs = {
        "TAPE-1": {"id": "aaa", "tape_id": "TAPE-1", "status": "COMPLETED", "updated_at": "2026-01-01T00:00:00"},
        "TAPE-2": {"id": "bbb", "tape_id": "TAPE-2", "status": "ENCODING_MP4", "updated_at": "2026-01-02T00:00:00"},
    }
    assert [j["tape_id"] for j in eng.jobs_list()] == ["TAPE-2", "TAPE-1"]  # newest first
    assert eng.job_by_ref("TAPE-1")["id"] == "aaa"
    assert eng.job_by_ref("bbb")["tape_id"] == "TAPE-2"
    assert eng.job_by_ref("nope") is None


def test_load_jobs_resumes_processing_when_raw_dv_present(tmp_path, monkeypatch):
    monkeypatch.setattr(Engine, "_process_worker", lambda self: None)  # don't drain the queue
    eng = _engine(tmp_path)
    dv = eng.config.working / "TAPE-5" / "capture001.dv"
    dv.parent.mkdir(parents=True)
    dv.write_bytes(b"x" * 10)
    (eng.config.state / "jobs.json").write_text(
        '{"TAPE-5": {"id": "x", "tape_id": "TAPE-5", "stage": "process", "status": "COMPRESSING", '
        '"history": [], "logs": "", "updated_at": "2026-01-01T00:00:00"}}')
    eng2 = _engine(tmp_path)
    assert eng2.jobs["TAPE-5"]["status"] == "QUEUED"
    assert "TAPE-5" in eng2.pending


def test_load_jobs_marks_completed_when_tape_json_already_written(tmp_path, monkeypatch):
    monkeypatch.setattr(Engine, "_process_worker", lambda self: None)
    eng = _engine(tmp_path)
    (eng.config.tapes / "TAPE-9").mkdir(parents=True)
    (eng.config.tapes / "TAPE-9" / "tape.json").write_text("{}")
    (eng.config.working / "TAPE-9").mkdir(parents=True)
    (eng.config.working / "TAPE-9" / "capture001.dv").write_bytes(b"x" * 10)
    (eng.config.state / "jobs.json").write_text(
        '{"TAPE-9": {"id": "z", "tape_id": "TAPE-9", "stage": "process", "status": "BUILDING_TAPE_PROXY", '
        '"history": [], "logs": "", "updated_at": "2026-01-01T00:00:00"}}')
    eng2 = _engine(tmp_path)
    assert eng2.jobs["TAPE-9"]["status"] == "COMPLETED"
    assert eng2.pending == []
    assert not (eng2.config.working / "TAPE-9").exists()
    assert (eng2.config.tapes / "TAPE-9" / "tape.json").exists()  # archive kept intact


def test_load_jobs_marks_error_when_raw_dv_gone(tmp_path, monkeypatch):
    monkeypatch.setattr(Engine, "_process_worker", lambda self: None)
    eng = _engine(tmp_path)
    (eng.config.state / "jobs.json").write_text(
        '{"TAPE-6": {"id": "y", "tape_id": "TAPE-6", "stage": "capture", "status": "CAPTURING", '
        '"history": [], "logs": "", "updated_at": "2026-01-01T00:00:00"}}')
    eng2 = _engine(tmp_path)
    assert eng2.jobs["TAPE-6"]["status"] == "ERROR"
    assert eng2.pending == []


def test_acquire_dv_relaunches_and_concatenates_segments(tmp_path, monkeypatch):
    """dvgrab exits by itself when the tape goes blank; _acquire_dv must relaunch into
    new segments and stop only after capture_idle_timeout, then concat them."""
    clock = [0.0]
    monkeypatch.setattr(em.time, "sleep", lambda s: clock.__setitem__(0, clock[0] + s))
    monkeypatch.setattr(em.time, "monotonic", lambda: clock[0])
    calls = {"n": 0}

    class FakePopen:
        def __init__(self, cmd, **kw):
            calls["n"] += 1
            self.n, self._p = calls["n"], 0
            if self.n <= 2:  # first two segments carry data; segment 3 = blank tape
                (pathlib.Path(cmd[-1]).parent / f"seg{self.n:03d}.dv").write_bytes(bytes([self.n]) * 100)
            clock[0] += 1

        def poll(self):
            self._p += 1
            return None if self._p == 1 else 0

        def wait(self, timeout=None):
            clock[0] += 1
            return 0

        def send_signal(self, s):
            pass

        def kill(self):
            pass

    monkeypatch.setattr(em.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(Engine, "_index_worker", lambda self: None)  # keep it off the fake clock
    eng = _engine(tmp_path, no_signal_timeout=5, blank_tail_timeout=8, capture_idle_timeout=20,
                  play_wait_timeout=100, max_dvgrab_restarts=10)
    eng.camera = type("C", (), {"info": staticmethod(lambda: {"connected": True}),
                                "command": staticmethod(lambda x: None)})()
    job = eng._new_job("TAPE-7", manual_transport=True)
    eng.jobs["TAPE-7"] = job
    work = eng.config.working / "TAPE-7"
    work.mkdir(parents=True)
    cap = work / "capture001.dv"
    eng._acquire_dv(job, work, cap, None, True)
    assert cap.read_bytes() == bytes([1]) * 100 + bytes([2]) * 100
    assert not list(work.glob("seg[0-9][0-9][0-9].dv"))
    assert calls["n"] >= 3  # relaunched past the two data segments into the blank


def _fake_acquire(dv_bytes):
    def acq(self, job, work, cap, dur, man):
        (work / "capture.log").write_text("Capture Stopped\n")
        if dv_bytes:
            cap.write_bytes(b"x" * dv_bytes)
    return acq


def test_cancel_with_captured_dv_is_archived_not_discarded(tmp_path, monkeypatch):
    monkeypatch.setattr(Engine, "_acquire_dv", _fake_acquire(4 * 3_600_000))
    eng = _engine(tmp_path, keep_partial_min_seconds=2)
    eng.camera = type("C", (), {"info": staticmethod(lambda: {"connected": True}),
                                "command": staticmethod(lambda x: None)})()
    job = eng._new_job("TAPE-8", manual_transport=True)
    eng.jobs["TAPE-8"] = job
    eng.capture_tape = "TAPE-8"
    eng.capture_cancel.set()  # user hit "Przerwij"
    eng._capture_job(job, rewind=False, duration=None, manual_transport=True)
    assert job["status"] == "CAPTURED"          # not CANCELLED
    assert "TAPE-8" in eng.pending              # handed to processing


def test_cancel_with_no_dv_is_cancelled(tmp_path, monkeypatch):
    monkeypatch.setattr(Engine, "_acquire_dv", _fake_acquire(0))
    eng = _engine(tmp_path)
    eng.camera = type("C", (), {"info": staticmethod(lambda: {"connected": True}),
                                "command": staticmethod(lambda x: None)})()
    job = eng._new_job("TAPE-8b", manual_transport=True)
    eng.jobs["TAPE-8b"] = job
    eng.capture_tape = "TAPE-8b"
    eng.capture_cancel.set()
    eng._capture_job(job, rewind=False, duration=None, manual_transport=True)
    assert job["status"] == "CANCELLED"
    assert eng.pending == []


def test_start_compress_registers_job_and_serves_when_ready(tmp_path, monkeypatch):
    import minidv_archiver.media as mm
    monkeypatch.setattr(mm, "compress_share",
                        lambda src, out, **k: pathlib.Path(out).write_bytes(b"small-mp4"))
    eng = _engine(tmp_path)
    td = eng.config.tapes / "TAPE-1"
    td.mkdir(parents=True)
    (td / "tape.json").write_text("{}")
    (td / "0001_x.mp4").write_bytes(b"proxy")
    j = eng.start_compress("TAPE-1", "0001_x")
    assert j["status"] in ("QUEUED", "RUNNING")
    for _ in range(100):
        if eng.compress_status("TAPE-1", "0001_x")["status"] in ("READY", "ERROR"):
            break
        time.sleep(0.05)
    done = eng.compress_status("TAPE-1", "0001_x")
    assert done["status"] == "READY", done
    assert done["size"] == len(b"small-mp4")
    assert pathlib.Path(done["path"]).read_bytes() == b"small-mp4"


def test_start_compress_restore_builds_from_the_dv_masters(tmp_path, monkeypatch):
    import minidv_archiver.media as mm
    seen = {}

    def fake_restore(sources, out, **k):
        seen["sources"] = [pathlib.Path(s).name for s in sources]
        seen["vf"] = k.get("vf")
        pathlib.Path(out).write_bytes(b"restored")

    monkeypatch.setattr(mm, "restore_mp4", fake_restore)
    eng = _engine(tmp_path)
    td = eng.config.tapes / "TAPE-2"
    td.mkdir(parents=True)
    (td / "tape.json").write_text("{}")
    (td / "0001_a.dv.zst").write_bytes(b"m1")
    (td / "0002_b.dv.zst").write_bytes(b"m2")
    (td / "0001_a.mp4").write_bytes(b"proxy")          # must NOT be picked for restore

    j = eng.start_compress("TAPE-2", "0001_a", restore=True)
    assert j["token"] == "0001_a-RES" and j["mode"] == "restore"
    for _ in range(100):
        if eng.compress_status("TAPE-2", "0001_a-RES")["status"] in ("READY", "ERROR"):
            break
        time.sleep(0.05)
    done = eng.compress_status("TAPE-2", "0001_a-RES")
    assert done["status"] == "READY" and pathlib.Path(done["path"]).read_bytes() == b"restored"
    assert seen["sources"] == ["0001_a.dv.zst"]         # the master, not the proxy
    assert "bwdif" in seen["vf"] and "atadenoise" in seen["vf"]

    # whole tape -> all masters, sorted
    jt = eng.start_compress("TAPE-2", None, restore=True)
    assert jt["token"] == "TAPE-RES"
    for _ in range(100):
        if eng.compress_status("TAPE-2", "TAPE-RES")["status"] in ("READY", "ERROR"):
            break
        time.sleep(0.05)
    assert eng.compress_status("TAPE-2", "TAPE-RES")["status"] == "READY"
    assert seen["sources"] == ["0001_a.dv.zst", "0002_b.dv.zst"]


def test_reprobe_tape_writes_fingerprint_and_error_score(tmp_path, monkeypatch):
    import shutil as _sh
    import subprocess as _sp
    import minidv_archiver.media as mm
    if not _sh.which("zstd"):
        import pytest
        pytest.skip("zstd not installed")
    monkeypatch.setattr(mm, "scene_probe_quality", lambda p, fc: (3, ["h0", "h1", "h2"]))
    eng = _engine(tmp_path)
    d = eng.config.tapes / "TAPE-1"
    (d / "thumbnails").mkdir(parents=True)
    (d / "tape.json").write_text(json.dumps({"tape_id": "TAPE-1", "scene_count": 1,
                                             "scenes": [{"scene_index": 1, "scene_id": "0001_x", "frame_count": 100}]}))
    _sp.run(["zstd", "-q", "-o", str(d / "0001_x.dv.zst"), "-"], input=b"\x00" * 4096, check=True)
    (d / "0001_x.json").write_text(json.dumps({
        "scene_index": 1, "scene_id": "0001_x", "tape_id": "TAPE-1", "frame_count": 100,
        "recording": {"datetime": "2004-07-11T10:00:00"}, "timecode": {"start": "00:00:00:00", "end": "00:00:04:00"},
        "capture": {"dropped_frames": 2, "source_discontinuities": ["x"]},
        "files": {"archive": {"filename": "0001_x.dv.zst"}}}))
    (d / "tape.sha256").write_text("h  0001_x.dv.zst\nh  0001_x.json\nh  tape.json\n")

    assert eng._reprobe_tape("TAPE-1")["updated"] == 1
    m = json.loads((d / "0001_x.json").read_text())
    assert m["capture"]["decode_errors"] == 3
    assert m["capture"]["error_score"] == 2 + 1 + 3            # dropped + discontinuities + decode
    assert m["fingerprint"]["frame_hashes"] == ["h0", "h1", "h2"]
    assert m["fingerprint"]["tc_start"] == "00:00:00:00"
    assert "h  0001_x.json" not in (d / "tape.sha256").read_text()   # sha line refreshed
    # second run is a no-op unless forced
    assert eng._reprobe_tape("TAPE-1")["updated"] == 0
    assert eng._reprobe_tape("TAPE-1", force=True)["updated"] == 1


def test_duplicates_groups_same_recording_across_tapes_best_first(tmp_path):
    from minidv_archiver import library_index as li
    eng = _engine(tmp_path)

    def tape(tid, score, hashes):
        d = eng.config.tapes / tid
        (d / "thumbnails").mkdir(parents=True)
        (d / "tape.json").write_text(json.dumps({"tape_id": tid, "scene_count": 1,
            "scenes": [{"scene_index": 1, "scene_id": "0001_s", "frame_count": 500}]}))
        (d / "0001_s.json").write_text(json.dumps({
            "scene_index": 1, "scene_id": "0001_s", "tape_id": tid, "frame_count": 500,
            "timecode": {"start": "00:05:00:00", "end": "00:05:20:00"},
            "recording": {"datetime": "2004-07-11T10:00:00"},
            "capture": {"dropped_frames": score, "source_discontinuities": [], "decode_errors": 0,
                        "error_score": score},
            "fingerprint": {"datetime": "2004-07-11T10:00:00", "tc_start": "00:05:00:00",
                            "tc_end": "00:05:20:00", "frame_count": 500, "frame_hashes": hashes}}))

    hashes = ["1111111111111111", "2222222222222222", "3333333333333333"]
    tape("TAPE-0001", 5, hashes)
    tape("TAPE-0012", 0, hashes)   # every hash matches -> same recording
    li.reindex(eng.config, eng.store, force=True)

    dup = eng.duplicates()
    assert dup["unprobed"] == 0 and len(dup["groups"]) == 1
    members = dup["groups"][0]["members"]
    assert [m["tape_id"] for m in members] == ["TAPE-0012", "TAPE-0001"]   # lower error_score first
    assert members[0]["best"] is True and members[1]["best"] is False
    assert members[0]["error_score"] == 0


def test_start_selection_concatenates_chosen_scenes(tmp_path, monkeypatch):
    import minidv_archiver.media as mm
    monkeypatch.setattr(mm, "concat_mp4",
                        lambda srcs, out, **k: pathlib.Path(out).write_bytes(b"joined:" + str(len(srcs)).encode()))
    monkeypatch.setattr(mm, "compress_share",
                        lambda src, out, **k: pathlib.Path(out).write_bytes(b"fb:" + str(len(src)).encode()))
    eng = _engine(tmp_path)
    td = eng.config.tapes / "TAPE-2"
    td.mkdir(parents=True)
    (td / "tape.json").write_text("{}")
    for s in ("0001_a", "0002_b", "0003_c"):
        (td / f"{s}.mp4").write_bytes(b"x")
    j = eng.start_selection("TAPE-2", ["0003_c", "0001_a", "0002_b", "0001_a"])
    assert j["token"].startswith("SEL-") and j["mode"] == "concat"
    for _ in range(100):
        if eng.compress_status("TAPE-2", j["token"])["status"] in ("READY", "ERROR"):
            break
        time.sleep(0.05)
    done = eng.compress_status("TAPE-2", j["token"])
    assert done["status"] == "READY"
    assert pathlib.Path(done["path"]).read_bytes() == b"joined:3"        # deduped + sorted
    assert eng.start_selection("TAPE-2", ["0001_a", "0002_b", "0003_c"])["token"] == j["token"]  # stable token

    # FB variant of the same selection: distinct token, re-encoded via compress_share
    jf = eng.start_selection("TAPE-2", ["0001_a", "0002_b", "0003_c"], share=True)
    assert jf["token"] == j["token"] + "-FB" and jf["mode"] == "share"
    for _ in range(100):
        if eng.compress_status("TAPE-2", jf["token"])["status"] in ("READY", "ERROR"):
            break
        time.sleep(0.05)
    assert pathlib.Path(eng.compress_status("TAPE-2", jf["token"])["path"]).read_bytes() == b"fb:3"

    try:
        eng.start_selection("TAPE-2", ["nope_x"])
        assert False
    except FileNotFoundError as exc:
        assert "brak scen" in str(exc)


def test_start_playlist_concatenates_scenes_across_tapes(tmp_path, monkeypatch):
    """The timeline's cross-tape build: scenes chosen from different tapes join
    into one MP4, same mechanism as start_selection but not confined to one tape."""
    import minidv_archiver.media as mm
    monkeypatch.setattr(mm, "concat_mp4",
                        lambda srcs, out, **k: pathlib.Path(out).write_bytes(b"joined:" + str(len(srcs)).encode()))
    eng = _engine(tmp_path)
    for tid, scenes in (("TAPE-A", ["0001_a"]), ("TAPE-B", ["0001_x", "0002_y"])):
        td = eng.config.tapes / tid
        td.mkdir(parents=True)
        (td / "tape.json").write_text("{}")
        for s in scenes:
            (td / f"{s}.mp4").write_bytes(b"x")

    items = [{"tape_id": "TAPE-B", "scene_id": "0001_x"}, {"tape_id": "TAPE-A", "scene_id": "0001_a"},
             {"tape_id": "TAPE-B", "scene_id": "0002_y"}, {"tape_id": "TAPE-B", "scene_id": "0001_x"}]  # w/ a dupe
    j = eng.start_playlist(items)
    assert j["token"].startswith("PL-") and j["mode"] == "concat" and j["tape_id"] == "_playlist"
    for _ in range(100):
        if eng.playlist_status(j["token"])["status"] in ("READY", "ERROR"):
            break
        time.sleep(0.05)
    done = eng.playlist_status(j["token"])
    assert done["status"] == "READY"
    assert pathlib.Path(done["path"]).read_bytes() == b"joined:3"    # deduped
    assert eng.start_playlist(items)["token"] == j["token"]           # same order -> stable token (cache hit)
    assert eng.start_playlist(list(reversed(items)))["token"] != j["token"]  # order matters for concat sequence
    assert eng.start_playlist(items[:2])["token"] != j["token"]

    try:
        eng.start_playlist([{"tape_id": "TAPE-A", "scene_id": "nope"}])
        assert False
    except FileNotFoundError as exc:
        assert "nie ma pliku" in str(exc)
    try:
        eng.start_playlist([])
        assert False
    except FileNotFoundError as exc:
        assert "pusta lista scen" in str(exc)


def _make_tape(eng, tape_id, scene_ids):
    d = eng.config.tapes / tape_id
    (d / "thumbnails").mkdir(parents=True)
    for sid in scene_ids:
        for suf in (".dv.zst", ".mp4", ".json"):
            (d / f"{sid}{suf}").write_bytes(b"x")
        (d / "thumbnails" / f"{sid}.jpg").write_bytes(b"x")
    (d / "tape.json").write_text(json.dumps({
        "tape_id": tape_id, "scene_count": len(scene_ids),
        "scenes": [{"scene_index": i + 1, "scene_id": s, "frame_count": 10} for i, s in enumerate(scene_ids)]}))
    (d / "tape.sha256").write_text(
        "".join(f"h  {s}.dv.zst\nh  {s}.mp4\nh  {s}.json\n" for s in scene_ids) + "h  tape.json\n")
    return d


def test_delete_tape_removes_everything(tmp_path):
    eng = _engine(tmp_path)
    _make_tape(eng, "TAPE-1", ["0001_a", "0002_b"])
    eng.jobs["TAPE-1"] = {"id": "x", "tape_id": "TAPE-1", "status": "COMPLETED", "updated_at": "z"}
    assert eng.delete_tape("TAPE-1") == {"deleted": "TAPE-1"}
    assert not (eng.config.tapes / "TAPE-1").exists()
    assert "TAPE-1" not in eng.jobs
    try:
        eng.delete_tape("TAPE-1")
        assert False
    except FileNotFoundError:
        pass


def test_delete_job_clears_empty_cancelled_capture(tmp_path):
    eng = _engine(tmp_path)
    eng.store.put_job({"tape_id": "Chorwacja", "stage": "capture", "status": "CANCELLED",
                       "history": [], "logs": "Error: no DV\n", "updated_at": "z"})
    assert eng.delete_job("Chorwacja") == {"deleted": "Chorwacja"}
    assert eng.store.get_job("Chorwacja") is None
    try:
        eng.delete_job("Chorwacja")
        assert False
    except FileNotFoundError:
        pass


def test_delete_job_refuses_running_and_completed(tmp_path):
    eng = _engine(tmp_path)
    eng.store.put_job({"tape_id": "TAPE-2", "stage": "capture", "status": "CAPTURING",
                       "history": [], "logs": "", "updated_at": "z"})
    try:
        eng.delete_job("TAPE-2")
        assert False
    except RuntimeError as exc:
        assert "przerwane" in str(exc)
    _make_tape(eng, "TAPE-3", ["0001_a"])
    eng.store.put_job({"tape_id": "TAPE-3", "stage": "process", "status": "COMPLETED",
                       "history": [], "logs": "", "updated_at": "z"})
    try:
        eng.delete_job("TAPE-3")
        assert False
    except RuntimeError as exc:
        assert "Usuń kasetę" in str(exc)
    assert (eng.config.tapes / "TAPE-3").exists()   # untouched — archived data stays


def test_delete_job_removes_orphaned_partial_dir_without_tape_json(tmp_path):
    eng = _engine(tmp_path)
    d = eng.config.tapes / "TAPE-4"
    d.mkdir(parents=True)
    (d / "0001_x.dv.zst").write_bytes(b"partial")   # processing died before tape.json was written
    eng.store.put_job({"tape_id": "TAPE-4", "stage": "process", "status": "ERROR",
                       "history": [], "logs": "", "updated_at": "z"})
    eng.delete_job("TAPE-4")
    assert not d.exists()


def test_delete_tape_refuses_while_in_use(tmp_path):
    eng = _engine(tmp_path)
    _make_tape(eng, "TAPE-9", ["0001_a"])
    eng.processing_tape = "TAPE-9"
    try:
        eng.delete_tape("TAPE-9")
        assert False
    except RuntimeError as exc:
        assert "w użyciu" in str(exc)
    assert (eng.config.tapes / "TAPE-9").exists()


def test_rename_tape_moves_dir_and_fixes_metadata(tmp_path):
    eng = _engine(tmp_path)
    d = _make_tape(eng, "TAPE-1", ["0001_a", "0002_b"])
    for sid in ("0001_a", "0002_b"):
        (d / f"{sid}.json").write_text(json.dumps({"scene_id": sid, "tape_id": "TAPE-1"}))
    eng.jobs["TAPE-1"] = {"id": "x", "tape_id": "TAPE-1", "status": "COMPLETED", "updated_at": "z"}
    assert eng.rename_tape("TAPE-1", "WEDDING-2004") == {"tape_id": "WEDDING-2004"}
    nd = eng.config.tapes / "WEDDING-2004"
    assert nd.is_dir() and not (eng.config.tapes / "TAPE-1").exists()
    assert json.loads((nd / "tape.json").read_text())["tape_id"] == "WEDDING-2004"
    assert json.loads((nd / "0001_a.json").read_text())["tape_id"] == "WEDDING-2004"
    assert "WEDDING-2004" in eng.jobs and "TAPE-1" not in eng.jobs
    ( eng.config.tapes / "WEDDING-2004" / "tape.sha256" )  # sha lines refreshed for edited json
    try:
        eng.rename_tape("WEDDING-2004", "bad name!")
        assert False
    except ValueError:
        pass


def test_rename_refuses_existing_name_and_in_use(tmp_path):
    eng = _engine(tmp_path)
    _make_tape(eng, "A", ["0001_a"])
    _make_tape(eng, "B", ["0001_b"])
    try:
        eng.rename_tape("A", "B")
        assert False
    except FileExistsError:
        pass
    eng.processing_tape = "A"
    try:
        eng.rename_tape("A", "C")
        assert False
    except RuntimeError as exc:
        assert "w użyciu" in str(exc)


def test_set_tape_meta_label_and_date(tmp_path):
    eng = _engine(tmp_path)
    d = _make_tape(eng, "TAPE-1", ["0001_a"])
    r = eng.set_tape_meta("TAPE-1", label="Wesele u babci", recording_date="2004-04-10")
    assert r == {"tape_id": "TAPE-1", "label": "Wesele u babci", "recording_date": "2004-04-10"}
    t = json.loads((d / "tape.json").read_text())
    assert t["label"] == "Wesele u babci" and t["recording_date"] == "2004-04-10"
    assert (d / "tape.sha256").read_text().rstrip().endswith("  tape.json")
    # clearing
    r = eng.set_tape_meta("TAPE-1", label="", recording_date="")
    assert r["label"] is None and r["recording_date"] is None
    assert "label" not in json.loads((d / "tape.json").read_text())
    try:
        eng.set_tape_meta("TAPE-1", recording_date="10-04-2004")
        assert False
    except ValueError:
        pass


def test_delete_scenes_updates_manifest_and_rebuilds_proxy(tmp_path, monkeypatch):
    import minidv_archiver.media as mm
    monkeypatch.setattr(mm, "concat_mp4", lambda srcs, out, **k: pathlib.Path(out).write_bytes(b"J" * len(srcs)))
    eng = _engine(tmp_path)
    d = _make_tape(eng, "TAPE-3", ["0001_a", "0002_b", "0003_c"])
    (d / "tape.mp4").write_bytes(b"old")
    res = eng.delete_scenes("TAPE-3", ["0002_b", "nope"])
    assert res == {"tape_id": "TAPE-3", "removed": ["0002_b"], "remaining": 2}
    assert not (d / "0002_b.dv.zst").exists() and not (d / "thumbnails" / "0002_b.jpg").exists()
    tape = json.loads((d / "tape.json").read_text())
    assert tape["scene_count"] == 2 and [s["scene_id"] for s in tape["scenes"]] == ["0001_a", "0003_c"]
    assert (d / "tape.mp4").read_bytes() == b"JJ"                      # rebuilt from the 2 remaining proxies
    sha = (d / "tape.sha256").read_text()
    assert "0002_b" not in sha and "0001_a.mp4" in sha and sha.rstrip().endswith("  tape.json")


def test_start_delete_scenes_runs_in_background_without_self_deadlock(tmp_path, monkeypatch):
    """Regression: delete_scenes() refuses to run on a busy tape (store.tape_busy),
    but the background worker marks a build RUNNING *before* calling it -- without
    ignore_builds, tape_busy() would see that very row and every background delete
    would immediately fail with "tape in use", deleting nothing, ever."""
    import minidv_archiver.media as mm
    monkeypatch.setattr(mm, "concat_mp4", lambda srcs, out, **k: pathlib.Path(out).write_bytes(b"J" * len(srcs)))
    eng = _engine(tmp_path)
    d = _make_tape(eng, "TAPE-3", ["0001_a", "0002_b", "0003_c"])
    j = eng.start_delete_scenes("TAPE-3", ["0002_b"])
    assert j["mode"] == "delete" and j["token"].startswith("DEL-")
    b = {}
    for _ in range(100):
        b = eng.compress_status("TAPE-3", j["token"])
        if b["status"] in ("READY", "ERROR"):
            break
        time.sleep(0.05)
    assert b["status"] == "READY", b.get("error")
    tape = json.loads((d / "tape.json").read_text())
    assert [s["scene_id"] for s in tape["scenes"]] == ["0001_a", "0003_c"]
    assert not (d / "0002_b.dv.zst").exists()

    try:
        eng.start_delete_scenes("TAPE-3", [])
        assert False
    except FileNotFoundError as exc:
        assert "pusta lista scen" in str(exc)


def test_set_derives_scene_index_and_keeps_scene_total(tmp_path):
    eng = _engine(tmp_path)
    job = eng._new_job("TAPE-9", manual_transport=True)
    eng.jobs["TAPE-9"] = job
    eng._set(job, "COMPRESSING", "0001_x", total=300)
    assert job["scene_index"] == 1 and job["scene_total"] == 300
    eng._set(job, "VERIFYING_ARCHIVES", "0002_y")   # total omitted -> stays at 300
    assert job["scene_index"] == 2 and job["scene_total"] == 300
    assert eng.store.get_job("TAPE-9")["scene_total"] == 300


def test_progress_updates_captured_bytes_without_history_entry(tmp_path):
    eng = _engine(tmp_path)
    job = eng._new_job("TAPE-9", manual_transport=True)
    eng.jobs["TAPE-9"] = job
    before = len(job["history"])
    eng._progress(job, 12_345_678)
    assert job["captured_bytes"] == 12_345_678
    assert len(job["history"]) == before          # no history spam every progress tick
    assert eng.store.get_job("TAPE-9")["captured_bytes"] == 12_345_678


def test_missing_camera_fails_and_cleans_working_dir(tmp_path):
    eng = _engine(tmp_path)
    eng.camera = type("FakeCam", (), {"info": staticmethod(lambda: {"connected": False})})()
    job = eng._new_job("TAPE-0002", manual_transport=True)
    eng.jobs["TAPE-0002"] = job
    eng.capture_tape = "TAPE-0002"
    eng._capture_job(job, rewind=False, duration=None, manual_transport=True)
    assert job["status"] == "ERROR"
    assert "reset prądowy" in job["error"]
    assert not (eng.config.working / "TAPE-0002").exists()
    assert eng.capture_tape is None  # slot released
