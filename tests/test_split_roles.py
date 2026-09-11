"""The point of the split: a capture finished by one process (grabber) is picked
up and archived by another (converter), coordinating only through state/jobs.db."""
import time

import minidv_archiver.media as mm
from minidv_archiver.config import Config
from minidv_archiver.engine import Engine


def _cfg(tmp_path, **kw):
    return Config(storage=tmp_path, min_free_gib=0, camera_wait_timeout=0, **kw)


def _fake_camera(connected=True):
    return type("C", (), {"info": staticmethod(lambda: {"connected": connected}),
                          "command": staticmethod(lambda x: {"command": x})})()


def test_api_restart_does_not_disturb_a_converter_owned_job(tmp_path):
    """Regression: starting an api-role Engine used to run a full reconcile() on
    *every* non-terminal job regardless of who owns it — including a process-stage
    job a sibling converter was actively mid-way through — and rmtree the partial
    tapes/<id> dir out from under it, crashing the converter's in-flight write."""
    storage = tmp_path
    cfg = _cfg(storage)
    cfg.ensure_dirs()
    (cfg.working / "Chorwacja01").mkdir(parents=True)
    (cfg.working / "Chorwacja01" / "capture001.dv").write_bytes(b"x" * 1000)  # raw master, intact
    (cfg.tapes / "Chorwacja01").mkdir(parents=True)
    (cfg.tapes / "Chorwacja01" / "0083_x.mp4").write_bytes(b"mid-write")     # converter is here right now
    eng = Engine(cfg, role="converter")
    eng.store.put_job({"tape_id": "Chorwacja01", "stage": "process", "status": "ENCODING_MP4",
                       "history": [], "logs": "", "updated_at": "t", "scene_index": 83, "scene_total": 300})

    Engine(cfg, role="api")   # <-- this used to be the crash: an api restart mid-processing

    job = eng.store.get_job("Chorwacja01")
    assert job["status"] == "ENCODING_MP4"                       # untouched
    assert (cfg.tapes / "Chorwacja01" / "0083_x.mp4").exists()    # not wiped out from under converter
    assert (cfg.working / "Chorwacja01" / "capture001.dv").exists()


def test_grabber_hands_capture_to_converter(tmp_path, monkeypatch):
    # converter side: stub the heavy pipeline, just drop a tape.json + sha file
    def fake_process(capture, tape_id, storage, level, log, state, *a, **k):
        d = storage / "tapes" / tape_id
        d.mkdir(parents=True)
        (d / "tape.json").write_text('{"tape_id": "%s", "scene_count": 1, "scenes": []}' % tape_id)
        (d / "tape.sha256").write_text("deadbeef  tape.json\n")
        state("COMPLETED")
        return {"tape_id": tape_id, "scene_count": 1, "scenes": []}

    monkeypatch.setattr(mm, "process_capture", fake_process)
    monkeypatch.setattr("minidv_archiver.engine.process_capture", fake_process)

    # grabber side: stub the DV acquisition to just write a raw file
    def fake_acquire(self, job, work, cap, dur, man):
        (work / "capture.log").write_text("Capture Stopped\n")
        cap.write_bytes(b"x" * (5 * 3_600_000))

    monkeypatch.setattr(Engine, "_acquire_dv", fake_acquire)

    storage = tmp_path
    grabber = Engine(_cfg(storage, allow_fcp=False), role="grabber")
    converter = Engine(_cfg(storage, allow_fcp=False), role="converter")
    grabber.camera = converter.camera = _fake_camera()

    # api-equivalent: enqueue a capture job into the shared store
    grabber.start(tape_id="TAPE-0001", manual_transport=True)
    assert grabber.role == "grabber"

    # grabber's dispatch loop equivalent
    job = grabber.store.claim_next_capture()
    assert job["tape_id"] == "TAPE-0001"
    grabber.run_capture_from_store(job)

    # the job is now handed to the converter, whose worker thread is already running
    for _ in range(100):
        if converter.store.get_job("TAPE-0001")["status"] in ("COMPLETED", "ERROR"):
            break
        time.sleep(0.05)
    assert converter.store.get_job("TAPE-0001")["status"] == "COMPLETED"
    assert (storage / "tapes" / "TAPE-0001" / "tape.json").exists()
    assert not (storage / "working" / "TAPE-0001").exists()   # raw DV cleaned up after archival


def test_api_role_runs_no_workers(tmp_path):
    eng = Engine(_cfg(tmp_path), role="api")
    # a queued process job must NOT be drained by an api-role engine
    eng.store.put_job({"tape_id": "T1", "stage": "process", "status": "QUEUED", "history": [], "logs": ""})
    time.sleep(0.3)
    assert eng.store.get_job("T1")["status"] == "QUEUED"


def test_cancel_via_store_is_seen_by_capture_loop(tmp_path, monkeypatch):
    """api (or another process) marks the job cancelled in the store; the grabber's
    _acquire_dv stop check must notice even though its in-process Event is clear."""
    seen = {}

    def fake_acquire(self, job, work, cap, dur, man):
        (work / "capture.log").write_text("x\n")
        seen["store_cancel"] = self._store_cancelled(job)
        cap.write_bytes(b"")

    monkeypatch.setattr(Engine, "_acquire_dv", fake_acquire)
    eng = Engine(_cfg(tmp_path, allow_fcp=False), role="grabber")
    eng.camera = _fake_camera()
    eng.start(tape_id="TAPE-0009", manual_transport=True)
    job = eng.store.claim_next_capture()
    eng.store.mark_cancel("TAPE-0009")            # the "api" cancels it
    eng.run_capture_from_store(job)
    assert seen["store_cancel"] is True
    assert eng.store.get_job("TAPE-0009")["status"] == "CANCELLED"
