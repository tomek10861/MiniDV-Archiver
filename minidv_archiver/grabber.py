"""Grabber service entrypoint — the only process that touches FireWire.

Holds an exclusive lock (``state/grabber.lock``) so at most one grabber runs. Its
dispatch loop claims freshly-created capture jobs from the store and runs them
(``Engine.run_capture_from_store`` -> the unchanged ``_capture_job`` /
``_acquire_dv`` logic). A cancel written to the job row by the api is picked up
by ``_acquire_dv``'s stop check. A second thread serves AV/C transport requests
(play / stop / rewind) the api enqueues for cameras that support FCP.

On shutdown it brings the camera to a safe STOP.
"""
from __future__ import annotations

import fcntl
import signal
import sys
import threading
import time

from .config import Config
from .engine import Engine


def _acquire_singleton(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        sys.exit(f"another grabber already holds {path}")
    return handle


def _cam_loop(engine: Engine, stop: threading.Event) -> None:
    while not stop.is_set():
        cmd = engine.store.claim_cam_command()
        if not cmd:
            time.sleep(0.5)
            continue
        try:
            engine.store.finish_cam_command(cmd["id"], True, engine.camera.command(cmd["name"]))
        except Exception as exc:  # noqa: BLE001 - report any failure back to the api
            engine.store.finish_cam_command(cmd["id"], False, {"error": str(exc)})


def main():
    cfg = Config()
    engine = Engine(cfg, role="grabber")
    _lock = _acquire_singleton(cfg.state / "grabber.lock")  # noqa: F841 - held for process lifetime
    print(f"MiniDV grabber ready (camera guid={cfg.camera_guid or 'auto'})")

    stop = threading.Event()

    def _shutdown(*_):
        stop.set()
        engine.capture_cancel.set()
        try:
            if engine.capture_proc and engine.capture_proc.poll() is None:
                engine.capture_proc.send_signal(signal.SIGINT)
            if not cfg.allow_fcp:
                pass
            elif engine.camera.info().get("connected"):
                engine.camera.command("stop")
        except Exception:
            pass

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, _shutdown)

    threading.Thread(target=_cam_loop, args=(engine, stop), daemon=True).start()

    while not stop.is_set():
        job = engine.store.claim_next_capture()
        if not job:
            time.sleep(1)
            continue
        try:
            engine.run_capture_from_store(job)
        except Exception as exc:  # noqa: BLE001 - never let one bad job kill the loop
            print(f"capture {job.get('tape_id')} failed: {exc}", file=sys.stderr)


if __name__ == "__main__":
    main()
