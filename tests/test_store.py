import json
import threading

from minidv_archiver.config import Config
from minidv_archiver.store import JobStore


def _store(tmp_path):
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    return JobStore(tmp_path / "state" / "jobs.db")


def test_put_get_roundtrip_is_faithful(tmp_path):
    s = _store(tmp_path)
    job = {"id": "abc", "tape_id": "TAPE-1", "stage": "capture", "status": "CREATED",
           "history": [{"status": "CREATED", "at": "t"}], "logs": "line\n", "manual_transport": True,
           "capture_meta": {"drops": 2}}
    s.put_job(job)
    back = s.get_job("TAPE-1")
    assert back["id"] == "abc" and back["capture_meta"] == {"drops": 2}
    assert back["history"] == [{"status": "CREATED", "at": "t"}]
    assert s.all_job_ids() == ["TAPE-1"]
    assert s.get_job("nope") is None


def test_list_jobs_newest_first(tmp_path):
    s = _store(tmp_path)
    s.put_job({"tape_id": "A", "status": "COMPLETED", "updated_at": "2026-01-01T00:00:00"})
    s.put_job({"tape_id": "B", "status": "ENCODING_MP4", "updated_at": "2026-01-02T00:00:00"})
    assert [j["tape_id"] for j in s.list_jobs()] == ["B", "A"]


def test_pending_and_claim_are_atomic(tmp_path):
    s = _store(tmp_path)
    for i in range(6):
        s.put_job({"tape_id": f"T{i}", "stage": "process", "status": "QUEUED",
                   "updated_at": f"2026-01-01T00:00:0{i}"})
    assert s.pending() == [f"T{i}" for i in range(6)]

    claimed, lock = [], threading.Lock()

    def worker():
        while True:
            job = s.claim_next_process()
            if not job:
                return
            with lock:
                claimed.append(job["tape_id"])

    threads = [threading.Thread(target=worker) for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(claimed) == [f"T{i}" for i in range(6)]   # each job taken exactly once
    assert s.pending() == []
    assert s.get_job("T0")["status"] == "ANALYZING_DV"


def test_capture_and_processing_job_views(tmp_path):
    s = _store(tmp_path)
    assert s.capture_job() is None
    s.put_job({"tape_id": "C1", "stage": "capture", "status": "CAPTURING", "updated_at": "t1"})
    s.put_job({"tape_id": "P1", "stage": "process", "status": "QUEUED", "updated_at": "t2"})
    s.put_job({"tape_id": "P2", "stage": "process", "status": "ENCODING_MP4", "updated_at": "t3"})
    assert s.capture_job()["tape_id"] == "C1"
    assert s.processing_job()["tape_id"] == "P2"        # QUEUED P1 is still "pending", not "processing"
    assert "P1" in s.pending() and "P2" not in s.pending()


def test_tape_busy(tmp_path):
    s = _store(tmp_path)
    assert not s.tape_busy("TAPE-9")
    s.put_job({"tape_id": "TAPE-9", "stage": "process", "status": "COMPRESSING", "updated_at": "t"})
    assert s.tape_busy("TAPE-9")
    s.put_job({"tape_id": "TAPE-9", "stage": "process", "status": "COMPLETED", "updated_at": "t"})
    assert not s.tape_busy("TAPE-9")
    s.put_build({"key": "TAPE-9/TAPE", "tape_id": "TAPE-9", "status": "RUNNING"})
    assert s.tape_busy("TAPE-9")


def test_builds_claim_and_drop(tmp_path):
    s = _store(tmp_path)
    s.put_build({"key": "T/one", "tape_id": "T", "status": "QUEUED", "token": "one"})
    s.put_build({"key": "T/two", "tape_id": "T", "status": "QUEUED", "token": "two"})
    b = s.claim_next_build()
    assert b["status"] == "RUNNING"
    assert s.get_build(b["key"])["status"] == "RUNNING"
    assert {x["key"] for x in s.builds_for_tape("T")} == {"T/one", "T/two"}
    s.delete_build("T/one")
    s.delete_build("T/two")
    assert s.builds_for_tape("T") == []


def test_prune_builds_drops_old_finished_only(tmp_path):
    s = _store(tmp_path)
    old = "2000-01-01T00:00:00+00:00"
    s.put_build({"key": "T/a", "tape_id": "T", "status": "READY"})
    s._conn.execute("UPDATE builds SET updated_at=? WHERE key='T/a'", (old,))
    s.put_build({"key": "T/b", "tape_id": "T", "status": "RUNNING"})
    s._conn.execute("UPDATE builds SET updated_at=? WHERE key='T/b'", (old,))   # old but not finished
    s.put_build({"key": "T/c", "tape_id": "T", "status": "READY"})              # finished but fresh
    s.prune_builds(max_age_hours=6)
    keys = {b["key"] for b in s.list_builds()}
    assert keys == {"T/b", "T/c"}


def test_cam_command_flow(tmp_path):
    s = _store(tmp_path)
    cid = s.enqueue_cam_command("play")
    got = s.claim_cam_command()
    assert got == {"id": cid, "name": "play"}
    assert s.claim_cam_command() is None
    s.finish_cam_command(cid, True, {"command": "play", "response": ["09"]})
    res = s.wait_cam_command(cid, timeout=1)
    assert res["status"] == "DONE" and res["result"]["command"] == "play"


def test_prune_keeps_recent_and_drops_old_terminal(tmp_path):
    s = _store(tmp_path)
    for i in range(40):
        s.put_job({"tape_id": f"T{i:02d}", "status": "COMPLETED", "updated_at": f"2026-01-01T00:{i:02d}:00"})
    s.put_job({"tape_id": "LIVE", "status": "CAPTURING", "stage": "capture",
               "updated_at": "2026-01-01T00:00:00"})   # old but non-terminal -> kept
    s.prune(30)
    ids = set(s.all_job_ids())
    assert "LIVE" in ids and "T39" in ids and "T00" not in ids
    assert len(ids) <= 31


def test_reconcile_only_touches_stages_it_owns(tmp_path):
    """A restarting api (stages=()) — or any role that doesn't own a stage — must
    never rmtree a tapes/<id> dir a sibling grabber/converter is still writing to."""
    cfg = Config(storage=tmp_path)
    cfg.ensure_dirs()
    s = JobStore(cfg.state / "jobs.db")
    s.put_job({"tape_id": "CAP-1", "stage": "capture", "status": "CAPTURING",
              "history": [], "logs": "", "updated_at": "t"})
    s.put_job({"tape_id": "PROC-1", "stage": "process", "status": "ENCODING_MP4",
              "history": [], "logs": "", "updated_at": "t"})
    (cfg.tapes / "PROC-1").mkdir(parents=True)
    (cfg.tapes / "PROC-1" / "0083_x.mp4").write_bytes(b"mid-write")   # converter mid-scene

    s.reconcile(cfg, stages=())                          # api role: touch nothing
    assert s.get_job("CAP-1")["status"] == "CAPTURING"
    assert s.get_job("PROC-1")["status"] == "ENCODING_MP4"
    assert (cfg.tapes / "PROC-1" / "0083_x.mp4").exists()  # NOT wiped out from under converter

    s.reconcile(cfg, stages=("capture",))                 # grabber role: only capture-stage
    assert s.get_job("CAP-1")["status"] == "ERROR"        # no raw DV -> orphaned capture
    assert s.get_job("PROC-1")["status"] == "ENCODING_MP4"  # still untouched
    assert (cfg.tapes / "PROC-1" / "0083_x.mp4").exists()


def test_import_legacy_then_reconcile(tmp_path):
    (tmp_path / "state").mkdir(parents=True)
    legacy = tmp_path / "state" / "jobs.json"
    legacy.write_text(json.dumps({
        "TAPE-5": {"id": "x", "tape_id": "TAPE-5", "stage": "process", "status": "COMPRESSING",
                   "history": [], "logs": "", "updated_at": "2026-01-01T00:00:00"},
        "TAPE-6": {"id": "y", "tape_id": "TAPE-6", "stage": "capture", "status": "CAPTURING",
                   "history": [], "logs": "", "updated_at": "2026-01-01T00:00:00"},
    }))
    cfg = Config(storage=tmp_path)
    cfg.ensure_dirs()
    dv = cfg.working / "TAPE-5" / "capture001.dv"
    dv.parent.mkdir(parents=True)
    dv.write_bytes(b"x" * 10)

    s = JobStore(cfg.state / "jobs.db")
    assert s.import_legacy(cfg.state / "jobs.json") is True
    assert not (cfg.state / "jobs.json").exists()
    assert (cfg.state / "jobs.json.imported").exists()
    s.reconcile(cfg)
    assert s.get_job("TAPE-5")["status"] == "QUEUED"      # raw DV present -> requeue
    assert "TAPE-5" in s.pending()
    assert s.get_job("TAPE-6")["status"] == "ERROR"       # capture cannot resume

    s2 = JobStore(cfg.state / "jobs.db")                  # already imported -> no-op
    assert s2.import_legacy(cfg.state / "jobs.json") is False
