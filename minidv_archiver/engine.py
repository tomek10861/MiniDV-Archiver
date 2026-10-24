from __future__ import annotations

import hashlib
import json
import random
import re
import shutil
import signal
import subprocess
import threading
import time
import uuid
from collections.abc import MutableMapping
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import faces, library_index
from .camera import Camera, CameraError
from .config import Config
from .media import process_capture, sha256
from .store import FACES_KEY, TERMINAL, JobStore, now

MAX_JOBS = 30
# Human label, not a filename (unlike tape_id) -- spaces are fine (e.g. "Kasia").
WORKSPACE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}")

__all__ = ["Engine", "now", "TERMINAL", "MAX_JOBS"]


class _JobsView(MutableMapping):
    """dict-like facade over the job store, so callers (and tests) keep using
    ``engine.jobs[...]`` while the rows actually live in SQLite."""

    def __init__(self, store: JobStore):
        self._store = store

    def __getitem__(self, key):
        job = self._store.get_job(key)
        if job is None:
            raise KeyError(key)
        return job

    def __setitem__(self, key, value):
        self._store.put_job({**value, "tape_id": key})

    def __delitem__(self, key):
        self._store.delete_job(key)

    def __contains__(self, key):
        return self._store.get_job(key) is not None

    def __iter__(self):
        return iter(self._store.all_job_ids())

    def __len__(self):
        return len(self._store.all_job_ids())


def _date_bound(v) -> str | None:
    """'1998', '1998-07' or '1998-07-15' from the browser, else None."""
    v = str(v or "").strip()
    return v if re.fullmatch(r"\d{4}(-\d{2}(-\d{2})?)?", v) else None


class Engine:
    """Capture is an exclusive FireWire slot; processing runs in a background queue,
    so a new tape can be captured while the previous one is still being archived.

    ``role`` selects which loops run in this process:
      * ``all``       — single-process mode (default): capture, converter and api together.
      * ``grabber``   — owns FireWire; drives captures only.
      * ``converter`` — no hardware; drains the processing / share-encode queues.
      * ``api``       — no loops; just reads the store and the filesystem.
    All roles share ``state/jobs.db``.
    """

    def __init__(self, config: Config, role: str = "all"):
        self.config = config
        self.role = role
        config.ensure_dirs()
        self.camera = Camera(config.camera_guid)
        self.lock = threading.RLock()
        self.proc_cv = threading.Condition(self.lock)
        self.store = JobStore(config.state / "jobs.db")
        self.capture_tape: str | None = None   # this process's live capture (grabber/all)
        self.capture_proc: subprocess.Popen | None = None
        self.capture_cancel = threading.Event()
        self.processing_tape: str | None = None  # this process's live conversion (converter/all)
        self.store.import_legacy(config.state / "jobs.json")
        # Only reconcile the stages this process actually owns: a restarting api (or
        # any role) must never rmtree a tapes/<id> dir a sibling grabber/converter is
        # actively writing to. See JobStore.reconcile.
        own_stages = tuple(s for role_ok, s in ((role in ("all", "grabber"), "capture"),
                                                (role in ("all", "converter"), "process")) if role_ok)
        self.store.reconcile(config, stages=own_stages)
        if role in ("all", "converter"):
            threading.Thread(target=self._process_worker, daemon=True).start()
            threading.Thread(target=self._compress_worker, daemon=True).start()
            threading.Thread(target=self._index_worker, daemon=True).start()
            threading.Thread(target=self._faces_worker, daemon=True).start()
            threading.Thread(target=self._media_worker, daemon=True).start()

    # ---- storage -----------------------------------------------------------
    def storage(self) -> dict:
        usage = shutil.disk_usage(self.config.storage)
        safe = self.config.min_free_gib * 1024**3
        usable = max(0, usage.free - safe)
        return {"path": str(self.config.storage), "total": usage.total, "used": usage.used, "free": usage.free,
                "min_free": safe, "ready": usage.free >= safe, "estimated_dv_hours": round(usable / 12.96e9, 1)}

    # ---- job registry ----------------------------------------------------
    @property
    def jobs(self) -> _JobsView:
        return _JobsView(self.store)

    @jobs.setter
    def jobs(self, mapping: dict) -> None:
        self.store.replace_all_jobs(dict(mapping))

    @property
    def pending(self) -> list[str]:
        return self.store.pending()

    def _persist(self) -> None:
        self.store.prune(MAX_JOBS)

    def _write_job(self, job: dict) -> None:
        """Persist our working copy without clobbering a ``cancel`` another process
        may have set on the row since we last read it."""
        row = self.store.get_job(job["tape_id"])
        if row and row.get("cancel"):
            job["cancel"] = True
        self.store.put_job(job)

    def _set(self, job: dict, status: str, scene_id=None, message=None, total=None) -> None:
        with self.lock:
            # capture cancellation is handled by _acquire_dv's own stop check (so a
            # partial tape can still be archived); only abort the processing worker here.
            if job.get("cancel") and status not in TERMINAL and job.get("stage") == "process":
                raise RuntimeError("processing cancelled")
            job["status"] = status
            job["updated_at"] = now()
            if scene_id:
                job["current_scene"] = scene_id
                try:  # scene_id = "NNNN_..." -> 1-based index within this tape, for progress/ETA
                    job["scene_index"] = int(scene_id.split("_", 1)[0])
                except (ValueError, IndexError):
                    pass
            if total:
                job["scene_total"] = total
            job["history"].append({"status": status, "at": now(), **({"message": message} if message else {})})
            self._write_job(job)
            self._persist()

    def _log(self, job: dict, message: str) -> None:
        """Every line gets a wall-clock [HH:MM:SS] prefix (UTC, matching created_at/
        updated_at elsewhere) — without it a flood of e.g. ffmpeg decode warnings
        reads as one undated wall of text, and there's no way to tell whether it's
        errors packed into 20 seconds or spread across an hour of tape."""
        if not message:
            return
        with self.lock:
            ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
            stamped = "\n".join(f"[{ts}] {line}" for line in message.splitlines() if line.strip())
            if not stamped:
                return
            job["logs"] = (job["logs"] + stamped + "\n")[-20000:]
            self._write_job(job)

    def _progress(self, job: dict, captured_bytes: int) -> None:
        """Cheap periodic update (no history entry) so the UI can show how far into
        the tape a capture is — elapsed time + bytes so far, from the growing seg*.dv."""
        with self.lock:
            job["captured_bytes"] = captured_bytes
            job["updated_at"] = now()
            self._write_job(job)

    def _new_job(self, tape_id: str, manual_transport: bool, timezone: str | None = None,
                workspace: str = "default") -> dict:
        return {"id": uuid.uuid4().hex, "tape_id": tape_id, "stage": "capture", "status": "CREATED",
                "created_at": now(), "updated_at": now(), "current_scene": None, "dropped_frames": 0,
                "logs": "", "history": [], "manual_transport": manual_transport, "error": None,
                "rewind": True, "duration": None, "timezone": timezone, "workspace": workspace}

    def jobs_list(self) -> list[dict]:
        return self.store.list_jobs()

    def job_by_ref(self, ref: str) -> dict | None:
        job = self.store.get_job(ref)
        if job:
            return job
        return next((j for j in self.store.list_jobs() if j.get("id") == ref), None)

    def current_job(self) -> dict:
        cap = self.store.capture_job()
        if cap:
            return cap
        jobs = self.store.list_jobs()
        return jobs[0] if jobs else {"status": "IDLE"}

    def status(self) -> dict:
        return {"capture": self.store.capture_job(), "processing": self.store.processing_job(),
                "queue": self.store.pending(), "jobs": self.store.list_jobs(),
                "compress": self.store.list_builds()}

    # ---- faces ("Osoby"): scan worker + daily schedule + person edits --------
    def _faces_worker(self) -> None:
        # a RUNNING row left by a crash/restart would block every future scan
        stale = self.store.get_build(FACES_KEY)
        if stale and stale["status"] == "RUNNING":
            stale.update(status="CANCELLED", error="przerwane restartem — następny skan dokończy resztę")
            self.store.put_build(stale)
        while True:
            time.sleep(5)
            try:
                # cheap checks first: cv2 is only imported once there's work to do
                if (self.config.faces_enabled and faces.due_today(self.config, self.store)
                        and faces.available()):
                    faces.mark_auto_run(self.config, self.store)
                    try:
                        self.store.enqueue_face_scan("cron")
                    except RuntimeError:
                        pass                       # one already running: the cron just skips
                build = self.store.claim_face_scan()
                if build:
                    self._run_face_scan(build)
            except Exception:  # noqa: BLE001 - the scheduler must never die
                pass

    def _run_face_scan(self, build: dict) -> None:
        try:
            if not faces.available():
                raise RuntimeError("brak opencv-python-headless/numpy — zainstaluj dodatek [faces]")
            res = faces.run_scan(self.config, self.store, build)
            build.update(status="READY", progress=1.0,
                         detail=f"{res['scenes']} nowych scen · {res['faces']} twarzy · "
                                f"{res['refined']} scen dokładnie · {res['persons']} osób")
        except faces.ScanCancelled:
            build.update(status="CANCELLED", detail="przerwane — następny skan dokończy resztę")
        except Exception as exc:  # noqa: BLE001
            build.update(status="ERROR", error=str(exc))
        build["cancel"] = False
        self.store.put_build(build)

    def start_face_scan(self) -> dict:
        if not self.config.faces_enabled:
            raise RuntimeError("rozpoznawanie twarzy wyłączone (MINIDV_FACES=0)")
        return self.store.enqueue_face_scan("manual")

    def cancel_face_scan(self) -> dict:
        b = self.store.request_build_cancel(FACES_KEY)
        if not b:
            raise RuntimeError("żadne skanowanie twarzy nie trwa")
        return b

    # ---- photos & videos ("Zdjęcia i filmy") -------------------------------
    def _media_reproxy(self, ml, busy) -> None:
        """DVD copies the operator asked to re-encode (media_request_reproxy) — never
        while a face scan runs, since re-encoding moves the face times of the parts."""
        for row in self.store.media_reproxy_pending():
            scan = self.store.get_build(FACES_KEY)
            if busy() or (scan and scan.get("status") in ("QUEUED", "RUNNING")):
                return
            ml.reencode_dvd(self.config, self.store, row, report=self._media_report)

    def media_request_reproxy(self, media_id: int) -> dict:
        """Queue one DVD title for re-encoding with the current DVD settings."""
        from . import media_library as ml
        row = self.store.media_get(media_id)
        if not row or row.get("parent_id") or not ml.is_dvd(row) or row.get("proxy") != 1:
            raise ValueError("to nie jest gotowa płyta DVD")
        self.store.media_update(media_id, reproxy=1)
        self.store.set_meta("media_rescan", "1")
        return {"id": media_id, "queued": True}

    def _media_worker(self) -> None:
        """Index the read-only media folders: find new files, then thumbnails and
        dates, then (slowest) H.264 copies of videos browsers can't play. Yields to
        captures/conversions; a manual "rescan" request (meta flag) wakes it early."""
        from . import media_library as ml
        busy = lambda: bool(self.store.capture_job() or self.store.processing_job())  # noqa: E731
        while True:
            try:
                if ml.roots(self.config):
                    res = ml.scan(self.config, self.store)
                    if res["removed"]:
                        self._drop_faces(ml.MEDIA_TAPE, [str(i) for i in res["removed"]])
                    ml.process_pending(self.config, self.store, busy=busy, report=self._media_report)
                    if self.store.get_meta("media_analog_dates") != "1":      # once: scans / tape transfers
                        ml.redate_analog(self.store)
                        self.store.set_meta("media_analog_dates", "1")
                    ml.date_from_file_times(self.config, self.store)
                    ml.date_from_neighbors(self.config, self.store)
                    ml.build_proxies(self.config, self.store, busy=busy, report=self._media_report)
                    ml.backfill_fps(self.config, self.store)
                    self._media_reproxy(ml, busy)
                    if self.config.media_split_min_s:
                        for parent in ml.split_long_videos(self.config, self.store, busy=busy,
                                                           report=self._media_report):
                            self._drop_faces(ml.MEDIA_TAPE, [str(parent)])   # its clips get scanned instead
                    self.store.set_meta("media_scanned_at", now())
                    self._media_report_idle()
            except Exception as exc:  # noqa: BLE001 - the indexer must never die
                self._media_report_idle(error=str(exc))
            backlog = self.store.media_counts()
            pause = 5 if backlog["new"] or backlog["proxies_pending"] else max(30, self.config.media_interval)
            for _ in range(pause // 5):
                if self.store.get_meta("media_rescan") == "1":
                    self.store.set_meta("media_rescan", "0")
                    break
                time.sleep(5)

    # the indexer as a row in Tasks (not a queued build: it's always on) ------------
    MEDIA_KEY = "_media/INDEX"
    MEDIA_STAGES = {"scan": "przegląd folderów", "thumbs": "miniatury i daty", "convert": "konwersja do MP4",
                    "split": "podział na sceny"}
    _media_last_report = 0.0

    def _media_report(self, stage: str, i: int, total: int, detail: str, frac: float = 0.0) -> None:
        if stage != "scan" and time.monotonic() - self._media_last_report < 2 and frac not in (0.0, 1.0):
            return
        self._media_last_report = time.monotonic()
        prog = (i + frac) / total if total else None
        self.store.put_build({"key": self.MEDIA_KEY, "tape_id": "_media", "token": "INDEX", "mode": "media",
                              "title": "Zdjęcia i filmy", "status": "RUNNING", "stage": stage,
                              "stage_name": self.MEDIA_STAGES.get(stage, stage), "detail": detail,
                              "done": i, "total": total, "progress": round(prog, 4) if prog is not None else None,
                              "path": "", "size": None, "error": None})

    def _media_report_idle(self, error: str | None = None) -> None:
        """RUNNING -> READY (or ERROR) once per busy spell, so an idle indexer doesn't
        keep refreshing its row (finished rows stay visible in Tasks for a while)."""
        row = self.store.get_build(self.MEDIA_KEY)
        if row and row["status"] == "RUNNING":
            c = self.store.media_counts()
            row.update(status="ERROR" if error else "READY", error=error, progress=1.0, stage=None,
                       detail=f"{c['photos']} zdjęć · {c['videos']} filmów")
            self.store.put_build(row)

    def media_status(self) -> dict:
        from . import media_library as ml
        return {"enabled": bool(ml.roots(self.config)), **self.store.media_counts(),
                "scanned_at": self.store.get_meta("media_scanned_at")}

    # ---- films about a person ("Zmontuj film", memories.py) -----------------------
    MEMORY_TAPE = "_memories"

    @staticmethod
    def _memory_public(m: dict) -> dict:
        music = m.get("music") or {}
        return {"id": m["id"], "person_id": m["person_id"], "status": m["status"], "progress": m.get("progress"),
                "title": m.get("title"), "created_at": m.get("created_at"), "updated_at": m.get("updated_at"),
                "duration": m.get("duration"), "size": m.get("size"), "error": m.get("error"),
                "params": {k: v for k, v in (m.get("params") or {}).items() if k != "seed"},
                "pieces": len(m.get("plan") or []),
                "music": {k: music.get(k) for k in ("id", "title", "artist", "credit", "mood")} if music else None,
                "url": f"/api/memories/{m['id']}/file?v={m.get('size') or 0}" if m["status"] == "READY" else None,
                "poster": f"/api/memories/{m['id']}/poster?v={m.get('size') or 0}" if m["status"] == "READY" else None}

    def memories_of(self, person_id: int) -> list[dict]:
        return [self._memory_public(m) for m in self.store.memories_of(person_id)]

    def create_memory(self, person_id: int, opts: dict) -> dict:
        from . import memories as mem
        p = self.store.face_person(person_id)
        if not p:
            raise ValueError("nie ma takiej osoby")
        length = opts.get("length") if opts.get("length") in mem.LENGTHS else "medium"
        sources = [x for x in (opts.get("sources") or ["dv", "video", "photo"]) if x in ("dv", "video", "photo")]
        music = opts.get("music")
        if music is not None and not isinstance(music, str):
            raise ValueError("zły utwór")
        params = {"length": length, "sources": sources or ["dv", "video", "photo"], "music": music or None,
                  "date_from": _date_bound(opts.get("date_from")), "date_to": _date_bound(opts.get("date_to")),
                  "lang": "en" if opts.get("lang") == "en" else "pl", "seed": random.randrange(1 << 30)}
        mid = self.store.memory_add(p["workspace"], person_id, p["name"] or "", params)
        self._enqueue_memory(mid)
        return self._memory_public(self.store.memory_get(mid))

    def memory_music(self, memory_id: int, music: str | None) -> dict:
        """Same moments, other music (the cuts are re-timed to its beat)."""
        m = self.store.memory_get(memory_id)
        if not m:
            raise ValueError("nie ma takiego filmu")
        if m["status"] in ("QUEUED", "RUNNING"):
            raise RuntimeError("film jest właśnie montowany")
        if music is not None and not isinstance(music, str):
            raise ValueError("zły utwór")
        params = {**(m["params"] or {}), "music": music or None}
        self.store.memory_update(memory_id, params=params, status="QUEUED", progress=0, error=None)
        self._enqueue_memory(memory_id)
        return self._memory_public(self.store.memory_get(memory_id))

    def delete_memory(self, memory_id: int) -> dict:
        m = self.store.memory_get(memory_id)
        if not m:
            raise ValueError("nie ma takiego filmu")
        if m["status"] == "RUNNING":
            raise RuntimeError("film jest właśnie montowany — usuń go po skończeniu")
        self.store.memory_delete(memory_id)
        if m.get("path"):
            Path(m["path"]).unlink(missing_ok=True)
            Path(m["path"]).with_suffix(".jpg").unlink(missing_ok=True)
        return {"deleted": memory_id}

    def memory_file(self, memory_id: int, poster: bool = False) -> Path | None:
        m = self.store.memory_get(memory_id)
        p = Path(m["path"]) if m and m.get("path") and m["status"] == "READY" else None
        p = p.with_suffix(".jpg") if p and poster else p
        return p if p and p.exists() else None

    def _enqueue_memory(self, memory_id: int) -> None:
        m = self.store.memory_get(memory_id)
        token = f"{memory_id}-{int(time.time())}"
        with self.proc_cv:
            self.store.put_build({"key": f"{self.MEMORY_TAPE}/{token}", "token": token, "tape_id": self.MEMORY_TAPE,
                                  "mode": "memory", "memory_id": memory_id,
                                  "title": f"{m.get('title') or '?'} — film", "sources": [], "status": "QUEUED",
                                  "path": "", "size": None, "error": None})
            self.proc_cv.notify_all()

    def _render_memory(self, build: dict, on_progress) -> None:
        from . import faces
        from . import memories as mem
        mid = build["memory_id"]
        m = self.store.memory_get(mid)
        if not m:
            return
        self.store.memory_update(mid, status="RUNNING", progress=0, error=None)
        try:
            params = m["params"] or {}
            rnd = random.Random(params.get("seed"))
            music_id = params.get("music")
            if music_id and music_id.startswith("auto"):
                mood = music_id.partition(":")[2] or "sentimental"
                pool = [t["id"] for t in mem.builtin_catalogue() if t["mood"] == mood] or \
                    [t["id"] for t in mem.builtin_catalogue()]
                music_id = rnd.choice(pool)
            music, period, offset, music_s = None, 1.0, 0.0, None
            if music_id:
                track, path = mem.ensure_track(self.config, music_id)
                period, offset = mem.beat_grid(path)
                music_s = (mem._duration(path) or 0) - offset
                music = (track, path)
            plan = m.get("plan")
            if not plan:
                person = faces.person_detail(self.store, m["person_id"]) or {}
                target = mem.LENGTHS[params.get("length", "medium")]
                if music_s:
                    target = min(target, music_s - mem.END_S - 1)
                others = [x["plan"] for x in self.store.memories_of(m["person_id"]) if x["id"] != mid and x["plan"]]
                plan = mem.pick(mem.candidates(self.config, self.store, person), target, seed=params.get("seed", 0),
                                avoid=mem.used_keys(others), date_from=params.get("date_from"),
                                date_to=params.get("date_to"), sources=tuple(params.get("sources") or ()))
            items = mem.fit_to_beats(plan, period, offset, music_s)
            if len(items) < 2:
                raise RuntimeError("za mało nagrań i zdjęć z tą osobą w wybranym okresie")
            years = sorted({c["date"][:4] for c in items if c.get("date")})
            subtitle = (f"{years[0]}–{years[-1]}" if len(years) > 1 else years[0]) if years else ""
            out_dir = self.config.storage / "memories"
            out_dir.mkdir(parents=True, exist_ok=True)
            out = out_dir / f"{mid}-{int(time.time())}.mp4"

            def progress(f: float) -> None:
                on_progress(f)
                self.store.memory_update(mid, progress=round(f, 3))
            dur = mem.render(self.config, items, out, title=m.get("title") or "", subtitle=subtitle, music=music,
                             offset=offset, lang=params.get("lang", "pl"), progress=progress)
            subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-ss", "2.2", "-i", str(out), "-frames:v", "1",
                            "-vf", "scale=640:-2", str(out.with_suffix(".jpg"))])     # cover: the title over the start
            old = m.get("path")
            self.store.memory_update(mid, status="READY", progress=1, plan=plan, path=str(out),
                                     size=out.stat().st_size, duration=dur,
                                     music={k: v for k, v in music[0].items() if not k.startswith("_")}
                                     if music else None)
            if old and old != str(out):
                Path(old).unlink(missing_ok=True)
                Path(old).with_suffix(".jpg").unlink(missing_ok=True)
        except Exception as exc:
            self.store.memory_update(mid, status="ERROR", error=str(exc)[-300:])
            raise

    def media_rescan(self) -> dict:
        self.store.set_meta("media_rescan", "1")
        return {"queued": True}

    def media_albums(self, workspace: str | None) -> list[dict]:
        from . import media_library as ml
        albums: dict[str, dict] = {}
        for r in self.store.media_items(workspace or "default"):
            a = albums.setdefault(r["album"], {"album": r["album"], "count": 0, "photos": 0, "videos": 0,
                                               "date_min": None, "date_max": None, "cover": []})
            a["count"] += 1
            a["photos" if r["kind"] == "photo" else "videos"] += 1
            d, _ = ml.effective_date(r)
            if d:
                a["date_min"] = min(a["date_min"] or d, d)
                a["date_max"] = max(a["date_max"] or d, d)
            if len(a["cover"]) < 4:
                a["cover"].append(r["id"])
        return sorted(albums.values(), key=lambda a: (a["date_max"] or "", a["album"]), reverse=True)

    def media_album(self, workspace: str | None, album: str) -> list[dict]:
        from . import media_library as ml
        items = [ml.public(r) for r in self.store.media_items(workspace or "default", album)]
        return sorted(items, key=lambda m: (m["date"] or "9999", m["time"] or "", m["name"]))

    def media_item(self, media_id: int) -> dict:
        row = self.store.media_get(media_id)
        if not row:
            raise FileNotFoundError("nie ma takiego pliku")
        return row

    def media_set_album_date(self, workspace: str | None, album: str, value) -> dict:
        v = str(value or "").strip()[:10]
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
            raise ValueError("data w formacie RRRR-MM-DD")
        return {"album": album, "dated": self.store.media_set_album_date(workspace or "default", album, v)}

    def media_set_date(self, media_id: int, value, whole: bool = False) -> dict:
        """whole=True on a part of a split film dates the film and all its parts."""
        from . import media_library as ml
        row = self.media_item(media_id)
        v = str(value or "").strip()[:10]
        if v and v != "unknown" and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
            raise ValueError("data w formacie RRRR-MM-DD albo 'unknown'")
        if whole and row.get("parent_id"):
            self.store.media_set_family_date(row["parent_id"], v or None)
        else:
            self.store.media_update(media_id, date_fix=v or None)
        return ml.public(self.store.media_get(media_id))

    def _drop_faces(self, tape_id: str, scene_ids: list[str] | None = None) -> None:
        ids, persons = self.store.face_drop_tracks(tape_id, scene_ids)
        faces.drop_thumbs(self.config, ids)
        for pid in persons:
            if self.store.face_person(pid):
                faces.recompute_person(self.store, pid)

    def faces_status(self, workspace: str | None) -> dict:
        return faces.status(self.config, self.store, workspace or "default")

    def persons(self, workspace: str | None) -> list[dict]:
        return faces.persons(self.store, workspace or "default")

    def person(self, person_id: int) -> dict:
        p = faces.person_detail(self.store, person_id)
        if not p:
            raise FileNotFoundError("nie ma takiej osoby")
        if not p["name"] and not p["hidden"] and faces.available():
            from . import face_refine
            p["suggest"] = face_refine.suggestions(self.config, self.store, p["workspace"]).get(person_id)
        return p

    def update_person(self, person_id: int, *, name=None, hidden=None, group=None) -> dict:
        p = self.store.face_person(person_id)
        if not p:
            raise FileNotFoundError("nie ma takiej osoby")
        if name is not None:
            name = str(name).strip()[:80]
            other = name and self.store.face_person_by_name(p["workspace"], name, exclude=person_id)
            if other:
                # same name as someone already in this workspace = the same person (iPhone-style)
                return self.merge_persons(person_id, other)
        if group is not None:
            group = str(group).strip()[:40]
        self.store.face_person_update(person_id, name=name, hidden=hidden, group=group)
        return self.store.face_person(person_id)

    def face_suggestions(self, workspace: str | None) -> list[dict]:
        """Unnamed people with the named person they most likely are, best first."""
        from . import face_refine
        ws = workspace or "default"
        sug = face_refine.suggestions(self.config, self.store, ws)
        persons = {p["id"]: p for p in self.store.face_persons(ws)}
        out = [{**persons[pid], "suggest": s} for pid, s in sug.items() if pid in persons]
        return sorted(out, key=lambda p: (-p["suggest"]["score"], -p["scenes"]))

    def _drop_memories(self, person_ids: list[int]) -> None:
        """Films of people that no longer exist (dissolved / deleted) go with them."""
        for pid in person_ids:
            for m in self.store.memories_of(int(pid)):
                if m["status"] != "RUNNING":
                    self.delete_memory(m["id"])

    def dissolve_persons(self, person_ids: list[int]) -> dict:
        self._drop_memories(person_ids)
        new = self.store.face_dissolve([int(p) for p in person_ids])
        for pid in new:
            faces.recompute_person(self.store, pid)
        return {"dissolved": len(person_ids), "faces": len(new)}

    def delete_persons(self, person_ids: list[int]) -> dict:
        self._drop_memories(person_ids)
        gone = self.store.face_delete_persons([int(p) for p in person_ids])
        faces.drop_thumbs(self.config, gone)
        return {"deleted": len(person_ids), "faces": len(gone)}

    def reject_suggestion(self, person_id: int, other_id: int) -> dict:
        a, b = self.store.face_person(person_id), self.store.face_person(other_id)
        if not a or not b:
            raise FileNotFoundError("nie ma takiej osoby")
        if a["workspace"] != b["workspace"]:
            raise ValueError("różne workspace")
        self.store.face_reject_pair(person_id, other_id)
        return {"person": person_id, "not": other_id}

    def merge_persons(self, src: int, dst: int) -> dict:
        if src == dst:
            raise ValueError("to ta sama osoba")
        self.store.face_merge(src, dst)
        self.store.memories_move(src, dst)                 # its films now belong to the merged person
        faces.recompute_person(self.store, dst)
        return self.store.face_person(dst)

    def move_face(self, track_id: int, person_id: int | None) -> dict:
        old, new = self.store.face_move_track(track_id, person_id)
        for pid in (old, new):
            if self.store.face_person(pid):
                faces.recompute_person(self.store, pid)
        self.store.face_prune_empty()
        return {"track": track_id, "from": old, "to": new}

    # ---- tape / scene index (fast /api/tapes + year/month timeline) -------
    def _index_worker(self) -> None:
        while True:
            try:
                library_index.reindex(self.config, self.store)
            except Exception:  # noqa: BLE001 - indexing must never take the process down
                pass
            time.sleep(max(30, self.config.index_interval))

    def _reindex(self, tape_id: str) -> None:
        try:
            library_index.reindex_one(self.config, self.store, tape_id)
        except Exception:  # noqa: BLE001
            pass

    def tapes(self, workspace: str | None = None) -> list[dict]:
        return library_index.tapes_list(self.config, self.store, workspace)

    def timeline(self, workspace: str | None = None, source: str = "all") -> dict:
        return library_index.timeline(self.config, self.store, workspace, source)

    def timeline_month(self, year: int, month: int, workspace: str | None = None, source: str = "all") -> list[dict]:
        return library_index.month_scenes(self.config, self.store, year, month, workspace, source)

    def duplicates(self, workspace: str | None = None) -> dict:
        return library_index.duplicates(self.config, self.store, workspace)

    # ---- workspaces: operator-created labels, "default" always implicit ----
    def list_workspaces(self) -> list[str]:
        return ["default"] + [n for n in self.store.list_workspaces() if n != "default"]

    def create_workspace(self, name: str) -> dict:
        name = (name or "").strip()
        if not name or not WORKSPACE_RE.fullmatch(name):
            raise ValueError("nieprawidłowa nazwa workspace")
        self.store.ensure_workspace(name)
        return {"name": name}

    def dismiss_duplicate_group(self, members: list[dict]) -> dict:
        """Operator says this group is a false positive — persist every pairwise
        combination so future duplicate scans (new fingerprints included) don't
        re-merge these specific scenes, without suppressing a genuinely new match
        involving just one of them."""
        keys = [f"{m['tape_id']}/{m['scene_id']}" for m in members
               if isinstance(m, dict) and m.get("tape_id") and m.get("scene_id")]
        if len(keys) < 2:
            raise ValueError("potrzeba co najmniej 2 scen do odrzucenia grupy")
        pairs = 0
        for i in range(len(keys)):
            for j in range(i + 1, len(keys)):
                self.store.dismiss_duplicate_pair(keys[i], keys[j])
                pairs += 1
        return {"dismissed_pairs": pairs}

    # ---- quality re-probe: decode-error count + content fingerprint per scene ----
    def start_reprobe(self, tape_id: str, force: bool = False) -> dict:
        self._tape_dir(tape_id)
        return self._enqueue_build(tape_id, "REPROBE-F" if force else "REPROBE",
                                   [tape_id], "reprobe", f"{tape_id} · ponowna sonda jakości")

    def _reprobe_tape(self, tape_id: str, force: bool = False) -> dict:
        from .media import scene_probe_quality
        d = self.config.tapes / tape_id
        if d.parent != self.config.tapes or not (d / "tape.json").exists():
            return {"tape_id": tape_id, "updated": 0}
        tmp_root = self.config.storage / "tmp"
        tmp_root.mkdir(parents=True, exist_ok=True)
        hashes, updated = {}, 0
        for sj in sorted(d.glob("[0-9]*.json")):
            try:
                meta = json.loads(sj.read_text())
            except (OSError, ValueError):
                continue
            cap = meta.get("capture") or {}
            if not force and meta.get("fingerprint") and cap.get("decode_errors") is not None:
                continue
            arch = (meta.get("files") or {}).get("archive") or {}
            zst = d / (arch.get("filename") or f"{sj.stem}.dv.zst")
            if not zst.exists():
                continue
            raw = tmp_root / f"reprobe-{tape_id}-{sj.stem}.dv"
            try:
                with raw.open("wb") as out:
                    if subprocess.run(["zstd", "-q", "-dc", str(zst)], stdout=out).returncode:
                        continue
                fc = meta.get("frame_count") or (raw.stat().st_size // 144000)
                decode_errors, frame_hashes = scene_probe_quality(raw, fc)
            finally:
                raw.unlink(missing_ok=True)
            cap = meta.setdefault("capture", {})
            cap["decode_errors"] = decode_errors
            cap["error_score"] = ((cap.get("dropped_frames") or 0)
                                  + len(cap.get("source_discontinuities") or []) + decode_errors)
            meta["fingerprint"] = {"datetime": (meta.get("recording") or {}).get("datetime"),
                                   "tc_start": (meta.get("timecode") or {}).get("start"),
                                   "tc_end": (meta.get("timecode") or {}).get("end"),
                                   "frame_count": meta.get("frame_count"), "frame_hashes": frame_hashes}
            sj.write_text(json.dumps(meta, indent=2, ensure_ascii=False) + "\n")
            hashes[sj.name] = sha256(sj)
            updated += 1
        if hashes:
            self._refresh_sha_lines(d, hashes)
            self._reindex(tape_id)
        return {"tape_id": tape_id, "updated": updated}

    # ---- backfill creation_time/date onto MP4s that never got it ----------
    def start_metadata_backfill(self, tape_id: str, force: bool = False) -> dict:
        self._tape_dir(tape_id)
        return self._enqueue_build(tape_id, "METABACKFILL-F" if force else "METABACKFILL", [tape_id],
                                   "backfill_meta", f"{tape_id} · uzupełnienie daty nagrania w MP4")

    def _backfill_metadata_tape(self, tape_id: str, force: bool = False) -> dict:
        """Writes creation_time/date onto scene MP4s (and tape.mp4) that are missing
        it — from the recording datetime already sitting in each scene's .json — via
        a stream-copy remux (media.remux_metadata): no re-encode, master untouched.
        Keeps tape.sha256 truthful by recomputing the checksum of anything it edits.
        force=True also *recomputes* an existing tag instead of skipping it — for
        tapes processed before local_to_utc_iso existed, whose creation_time was
        wrongly stamped as if the camera's local time were already UTC."""
        from .media import ffprobe, local_to_utc_iso, max_plausible_year, remux_metadata, scene_metadata, sha256 as file_sha256
        d = self.config.tapes / tape_id
        if d.parent != self.config.tapes or not (d / "tape.json").exists():
            return {"tape_id": tape_id, "updated": 0}
        tz_name = self.config.default_recording_tz
        updated, first_dt, hashes = 0, None, {}
        for sj in sorted(d.glob("[0-9]*.json")):
            try:
                meta = json.loads(sj.read_text())
            except (OSError, ValueError):
                continue
            dt = (meta.get("recording") or {}).get("datetime")
            dt = dt and library_index.fix_century(dt)          # "1926" from a two-digit VAUX year
            valid = dt and dt[:4].isdigit() and 1990 <= int(dt[:4]) <= max_plausible_year()
            if valid and first_dt is None:
                first_dt = dt
            proxy_name = ((meta.get("files") or {}).get("proxy") or {}).get("filename") or f"{sj.stem}.mp4"
            proxy = d / proxy_name
            if not valid or not proxy.exists():
                continue
            if ffprobe(proxy).get("format", {}).get("tags", {}).get("creation_time") and not force:
                continue
            tc = meta.get("timecode") or {}
            tags = scene_metadata(tape_id, meta.get("scene_index", 1), dt,
                                  (meta.get("recording") or {}).get("datetime_source", "DV_VAUX"),
                                  tc.get("start"), tc.get("end"), tz_name)
            remux_metadata(proxy, tags)
            meta.setdefault("files", {}).setdefault("proxy", {})["sha256"] = file_sha256(proxy)
            meta["files"]["proxy"]["size"] = proxy.stat().st_size
            sj.write_text(json.dumps(meta, indent=2, ensure_ascii=False) + "\n")
            hashes[proxy_name] = meta["files"]["proxy"]["sha256"]
            hashes[sj.name] = file_sha256(sj)
            updated += 1
        tape_mp4 = d / "tape.mp4"
        if (tape_mp4.exists() and first_dt
                and (force or not ffprobe(tape_mp4).get("format", {}).get("tags", {}).get("creation_time"))):
            tape_meta = json.loads((d / "tape.json").read_text())
            remux_metadata(tape_mp4, {"title": f"{tape_id} — cała taśma", "date": first_dt[:10],
                                      "creation_time": local_to_utc_iso(first_dt, tz_name),
                                      "comment": f"MiniDV {tape_id} · {tape_meta.get('scene_count', 0)} scen · sklejony podgląd"})
            updated += 1
        if hashes:
            self._refresh_sha_lines(d, hashes)
        if updated:
            self._reindex(tape_id)
        return {"tape_id": tape_id, "updated": updated}

    # ---- on-demand re-encode / download builds ----------------------------
    def _prune_share(self) -> None:
        share = self.config.storage / "tmp" / "share"
        cutoff = time.time() - 2 * 3600
        globs = ("*.mp4", "*.src.dv", "*.join.txt", "*.concat.txt")
        for p in (q for g in globs for q in share.glob(g)) if share.exists() else []:
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink()
            except OSError:
                pass

    def _enqueue_build(self, tape_id: str, token: str, sources: list[Path], mode: str, title: str,
                       stabilize: bool = False, upscale: bool = False, kinds: list[str] | None = None) -> dict:
        key = f"{tape_id}/{token}"
        with self.proc_cv:
            existing = self.store.get_build(key)
            if existing and (existing["status"] in ("QUEUED", "RUNNING")
                             or (existing["status"] == "READY" and Path(existing["path"]).exists())):
                return dict(existing)
            ext = ".zip" if mode == "archive" else ".mp4"
            out = self.config.storage / "tmp" / "share" / f"{tape_id}_{token}{ext}"
            build = {"key": key, "token": token, "tape_id": tape_id, "mode": mode, "title": title,
                     "sources": [str(p) for p in sources], "status": "QUEUED", "path": str(out),
                     "size": None, "error": None, "stabilize": stabilize, "upscale": upscale,
                     "kinds": kinds}
            self.store.put_build(build)
            self.proc_cv.notify_all()
        return dict(self.store.get_build(key))

    def _tape_dir(self, tape_id: str) -> Path:
        d = self.config.tapes / tape_id
        if d.parent != self.config.tapes or not (d / "tape.json").exists():
            raise FileNotFoundError(f"nie ma kasety {tape_id}")
        return d

    @staticmethod
    def _restore_tag(stabilize: bool, upscale: bool) -> str:
        """Token/filename suffix for a restore build — distinct per option combo, so
        toggling stabilize/upscale never serves a stale cached build made without
        them (see _enqueue_build's READY-reuse check) and each combo downloads as
        its own file."""
        return "-RES" + ("-STAB" if stabilize else "") + ("-UP" if upscale else "")

    def start_compress(self, tape_id: str, scene_id: str | None, restore: bool = False,
                       stabilize: bool = False, upscale: bool = False) -> dict:
        """FB-size re-encode of one scene / the whole tape (scene_id=None), or —
        with restore=True — a denoise/repair MP4 built from the DV master(s).
        stabilize/upscale only apply together with restore=True."""
        d = self._tape_dir(tape_id)
        label = (f" · scena {scene_id}" if scene_id else " — cała taśma")
        if restore:
            if scene_id is None:
                srcs = sorted(d.glob("[0-9]*.dv.zst"))
                if not srcs:
                    raise FileNotFoundError("brak masterów .dv.zst")
            else:
                src = d / f"{scene_id}.dv.zst"
                if not src.exists():
                    raise FileNotFoundError(f"nie ma pliku {src.name}")
                srcs = [src]
            return self._enqueue_build(tape_id, (scene_id or "TAPE") + self._restore_tag(stabilize, upscale),
                                       srcs, "restore", f"{tape_id}{label} · naprawiony",
                                       stabilize=stabilize, upscale=upscale)
        src = d / ("tape.mp4" if scene_id is None else f"{scene_id}.mp4")
        if not src.exists():
            raise FileNotFoundError(f"nie ma pliku {src.name}")
        return self._enqueue_build(tape_id, scene_id or "TAPE", [src], "share", f"{tape_id}{label}")

    def start_selection(self, tape_id: str, scene_ids: list[str], share: bool = False,
                        restore: bool = False, stabilize: bool = False, upscale: bool = False,
                        archive: bool = False) -> dict:
        """Join several scenes into one MP4 — stream copy, ~FB-size re-encode, or a
        repair pass from those scenes' DV masters (restore=True) — or, with
        archive=True, skip merging entirely and zip each scene's proxy as its own
        file (restore/stabilize/upscale don't apply to that mode)."""
        d = self._tape_dir(tape_id)
        ids = sorted({s for s in scene_ids if re.fullmatch(r"[0-9A-Za-z._-]{1,80}", s or "")})
        if not ids:
            raise FileNotFoundError("pusta lista scen")
        if archive:
            srcs = [d / f"{s}.mp4" for s in ids]
            missing = [p.name for p in srcs if not p.exists()]
            if missing:
                raise FileNotFoundError("brak scen: " + ", ".join(missing))
            token = "SEL-" + hashlib.sha1("\n".join(ids).encode()).hexdigest()[:12] + "-ZIP"
            return self._enqueue_build(tape_id, token, srcs, "archive", f"{tape_id} · {len(ids)} scen (ZIP)")
        suffix = ".dv.zst" if restore else ".mp4"
        srcs = [d / f"{s}{suffix}" for s in ids]
        missing = [p.name for p in srcs if not p.exists()]
        if missing:
            raise FileNotFoundError("brak scen: " + ", ".join(missing))
        tag = self._restore_tag(stabilize, upscale) if restore else "-FB" if share else ""
        token = "SEL-" + hashlib.sha1("\n".join(ids).encode()).hexdigest()[:12] + tag
        mode = "restore" if restore else "share" if share else "concat"
        return self._enqueue_build(tape_id, token, srcs, mode, f"{tape_id} · {len(ids)} scen",
                                   stabilize=stabilize if restore else False, upscale=upscale if restore else False)

    PLAYLIST_BUCKET = "_playlist"

    def start_playlist(self, items: list[dict], title: str | None = None, restore: bool = False,
                       stabilize: bool = False, upscale: bool = False, archive: bool = False) -> dict:
        """Join scenes picked from possibly *different* tapes (the timeline's
        cross-tape multi-select) into one MP4 — same stream-copy concat as
        start_selection, just not confined to one tape's directory — or repair
        from their DV masters (restore=True), or skip merging and zip each
        scene's proxy as its own file (archive=True). Unlike start_selection this
        does NOT sort scenes by id: order is the caller's (the timeline sends its
        already time-sorted scene order), since here scene_index carries no
        cross-tape meaning to sort by."""
        from . import media_library as ml
        pairs, seen = [], set()
        for it in items or []:
            tid, sid = (it or {}).get("tape_id"), (it or {}).get("scene_id")
            if not (isinstance(tid, str) and isinstance(sid, str)):
                continue
            if not ((tid == ml.MEDIA_TAPE or re.fullmatch(r"[0-9A-Za-z._-]{1,80}", tid))
                    and re.fullmatch(r"[0-9A-Za-z._-]{1,80}", sid)):
                continue
            if (tid, sid) in seen:
                continue
            seen.add((tid, sid))
            pairs.append((tid, sid))
        if not pairs:
            raise FileNotFoundError("pusta lista scen")
        # camera/phone photos & videos mixed in -> a re-encoded montage (MiniDV alone
        # stays a lossless stream-copy join; "restore" works from DV masters only)
        mixed = any(t == ml.MEDIA_TAPE for t, _ in pairs)
        if mixed and restore and not archive:
            raise ValueError("naprawa z masterów działa tylko dla samych scen MiniDV — odznacz zdjęcia i filmy "
                             "albo zbuduj zwykły film")
        suffix = ".dv.zst" if (restore and not archive) else ".mp4"
        srcs, kinds = [], []
        for tid, sid in pairs:
            if tid == ml.MEDIA_TAPE:
                row = self.store.media_get(int(sid)) if sid.isdigit() else None
                if not row:
                    raise FileNotFoundError(f"nie ma pliku {sid}")
                p = Path(row["path"]) if archive or row["kind"] == "photo" else ml.video_path(self.config, row)
                if p is None:
                    raise FileNotFoundError(f"{row['name']}: film jeszcze się konwertuje — spróbuj za chwilę")
                srcs.append(p)
                kinds.append(row["kind"])
                continue
            d = self._tape_dir(tid)
            p = d / f"{sid}{suffix}"
            if not p.exists():
                raise FileNotFoundError(f"nie ma pliku {tid}/{sid}{suffix}")
            srcs.append(p)
            kinds.append("dv")
        tag = "-ZIP" if archive else (self._restore_tag(stabilize, upscale) if restore else "-MIX" if mixed else "")
        token = "PL-" + hashlib.sha1("\n".join(f"{t}/{s}" for t, s in pairs).encode()).hexdigest()[:12] + tag
        ttl = title or f"Wybrane sceny ({len(pairs)})"
        mode = "archive" if archive else "restore" if restore else "montage" if mixed else "concat"
        return self._enqueue_build(self.PLAYLIST_BUCKET, token, srcs, mode, ttl,
                                   stabilize=stabilize if (restore and not archive) else False,
                                   upscale=upscale if (restore and not archive) else False, kinds=kinds)

    def playlist_status(self, token: str) -> dict:
        return self.compress_status(self.PLAYLIST_BUCKET, token)

    def _compress_worker(self) -> None:
        poll = self.role != "all"
        while True:
            if poll:
                build = self.store.claim_next_build()
                if not build:
                    time.sleep(1)
                    continue
            else:
                with self.proc_cv:
                    while not any(b["status"] == "QUEUED" and b["key"] != FACES_KEY
                                  for b in self.store.list_builds()):
                        self.proc_cv.wait()
                build = self.store.claim_next_build()
                if not build:
                    continue
            out = Path(build["path"])
            try:
                from .media import build_archive, compress_share, concat_mp4, restore_mp4
                out.parent.mkdir(parents=True, exist_ok=True)
                self._prune_share()
                self.store.prune_builds()
                srcs = [Path(s) for s in build["sources"]]

                def on_progress(frac: float, _build=build) -> None:
                    _build["progress"] = round(min(1.0, max(0.0, frac)), 4)
                    self.store.put_build(_build)

                if build["mode"] == "reprobe":
                    self._reprobe_tape(build["sources"][0], force=build["token"].endswith("-F"))
                elif build["mode"] == "backfill_meta":
                    self._backfill_metadata_tape(build["sources"][0], force=build["token"].endswith("-F"))
                elif build["mode"] == "delete":
                    self.delete_scenes(build["tape_id"], build["sources"], _from_build=True)
                elif build["mode"] == "memory":
                    self._render_memory(build, on_progress)
                elif build["mode"] == "montage":
                    from .media import montage_mp4
                    montage_mp4([{"path": p, "kind": k} for p, k in zip(srcs, build.get("kinds") or [])], out,
                                meta={"title": build["title"]}, log=lambda m: None, progress=on_progress)
                elif build["mode"] == "concat":
                    concat_mp4(srcs, out, meta={"title": build["title"]}, log=lambda m: None, progress=on_progress)
                elif build["mode"] == "archive":
                    build_archive(srcs, out)
                elif build["mode"] == "restore":
                    restore_mp4(srcs, out, vf=self.config.restore_filters, crf=self.config.restore_crf,
                                preset=self.config.restore_preset, decimate=self.config.restore_decimate,
                                stabilize=bool(build.get("stabilize")),
                                stabilize_shakiness=self.config.restore_stabilize_shakiness,
                                stabilize_smoothing=self.config.restore_stabilize_smoothing,
                                upscale=bool(build.get("upscale")),
                                upscale_factor=self.config.restore_upscale_factor,
                                upscale_scaler=self.config.restore_upscale_scaler,
                                meta={"title": build["title"]}, log=lambda m: None, progress=on_progress)
                else:
                    compress_share(srcs if len(srcs) > 1 else srcs[0], out, max_mb=self.config.share_max_mb,
                                   crf=self.config.share_crf, preset=self.config.share_preset,
                                   meta={"title": build["title"]}, log=lambda m: None, progress=on_progress)
                build.update(status="READY",
                             size=None if build["mode"] in ("reprobe", "backfill_meta", "delete", "memory")
                             else out.stat().st_size)
                self.store.put_build(build)
            except Exception as exc:
                build.update(status="ERROR", error=str(exc))
                self.store.put_build(build)

    def compress_status(self, tape_id: str, token: str | None) -> dict:
        build = self.store.get_build(f"{tape_id}/{token or 'TAPE'}")
        return dict(build) if build else {"status": "NONE"}

    # ---- deletion / rename / metadata (irreversible — the caller must have confirmed) ----
    def _busy(self, tape_id: str, ignore_builds: bool = False) -> bool:
        return self.store.tape_busy(tape_id, ignore_builds=ignore_builds) or tape_id in (self.capture_tape, self.processing_tape)

    def _drop_share(self, tape_id: str) -> None:
        for build in self.store.builds_for_tape(tape_id):
            Path(build["path"]).unlink(missing_ok=True)
            self.store.delete_build(build["key"])

    def delete_job(self, tape_id: str) -> dict:
        """Clear a CANCELLED/ERROR job record that holds a tape_id but has no
        archived data — e.g. a capture that never got a PLAY signal. Refuses if the
        tape actually archived (use delete_tape for that) or is still in use."""
        with self.lock:
            job = self.store.get_job(tape_id)
            if not job:
                raise FileNotFoundError(f"brak zadania {tape_id}")
            if job.get("status") not in TERMINAL or job.get("status") == "COMPLETED":
                raise RuntimeError('można usunąć tylko przerwane/błędne zadania — '
                                   'ukończoną kasetę usuń przez "Usuń kasetę"')
            if self._busy(tape_id):
                raise RuntimeError("zadanie jest w użyciu — poczekaj, aż się skończy")
            d = self.config.tapes / tape_id
            if d.is_dir():
                if (d / "tape.json").exists():
                    raise RuntimeError('kaseta ma zarchiwizowane dane — usuń ją przez "Usuń kasetę"')
                shutil.rmtree(d, ignore_errors=True)   # partial/orphaned archive dir, no tape.json
            shutil.rmtree(self.config.working / tape_id, ignore_errors=True)
            self.store.delete_job(tape_id)
            self._drop_share(tape_id)
            self.store.delete_index(tape_id)
        return {"deleted": tape_id}

    def delete_tape(self, tape_id: str) -> dict:
        with self.lock:
            if self._busy(tape_id):
                raise RuntimeError("kaseta jest w użyciu — poczekaj, aż zadanie się skończy")
            d = self.config.tapes / tape_id
            if d.parent != self.config.tapes or not d.is_dir():
                raise FileNotFoundError(f"nie ma kasety {tape_id}")
            shutil.rmtree(d, ignore_errors=True)
            shutil.rmtree(self.config.working / tape_id, ignore_errors=True)
            self.store.delete_job(tape_id)
            self._drop_share(tape_id)
            self.store.delete_index(tape_id)
            self._drop_faces(tape_id)
        return {"deleted": tape_id}

    def start_delete_scenes(self, tape_id: str, scene_ids: list[str]) -> dict:
        """Deleting scenes rebuilds the whole-tape proxy (re-concats every remaining
        scene's MP4) — for a tape with hundreds of scenes that's slow enough to time
        out the HTTP request, so this runs in the background like the other on-demand
        builds, visible (and individually retryable) in Zadania instead of silently
        stalling partway through a browser-side loop over many tapes."""
        self._tape_dir(tape_id)
        ids = sorted({s for s in scene_ids if re.fullmatch(r"[0-9A-Za-z._-]{1,80}", s or "")})
        if not ids:
            raise FileNotFoundError("pusta lista scen")
        token = "DEL-" + hashlib.sha1("\n".join(ids).encode()).hexdigest()[:12]
        return self._enqueue_build(tape_id, token, ids, "delete", f"{tape_id} · usuń {len(ids)} scen")

    def delete_scenes(self, tape_id: str, scene_ids: list[str], _from_build: bool = False) -> dict:
        with self.lock:
            if self._busy(tape_id, ignore_builds=_from_build):
                raise RuntimeError("kaseta jest w użyciu — spróbuj później")
        d = self._tape_dir(tape_id)
        tape = json.loads((d / "tape.json").read_text())
        ids = {s for s in scene_ids if re.fullmatch(r"[0-9A-Za-z._-]{1,80}", s or "")}
        removed = [s["scene_id"] for s in tape.get("scenes", []) if s["scene_id"] in ids]
        if not removed:
            raise FileNotFoundError("brak takich scen w kasecie")
        for sid in removed:
            for suffix in (".dv.zst", ".mp4", ".json"):
                (d / f"{sid}{suffix}").unlink(missing_ok=True)
            (d / "thumbnails" / f"{sid}.jpg").unlink(missing_ok=True)
        tape["scenes"] = [s for s in tape.get("scenes", []) if s["scene_id"] not in ids]
        tape["scene_count"] = len(tape["scenes"])
        proxies = sorted(d.glob("[0-9]*.mp4"))
        if proxies:
            try:
                from .media import concat_mp4
                concat_mp4(proxies, d / "tape.mp4", meta={"title": f"{tape_id} — cała taśma"})
                tape["proxy_full"] = {"filename": "tape.mp4", "size": (d / "tape.mp4").stat().st_size}
            except Exception:
                (d / "tape.mp4").unlink(missing_ok=True)
                tape.pop("proxy_full", None)
        else:
            (d / "tape.mp4").unlink(missing_ok=True)
            tape.pop("proxy_full", None)
        (d / "tape.json").write_text(json.dumps(tape, indent=2) + "\n")
        sha = d / "tape.sha256"
        if sha.exists():
            lines = [ln for ln in sha.read_text().splitlines()
                     if ln and not ln.endswith("  tape.json") and not any(r in ln for r in removed)]
            lines.append(f"{sha256(d / 'tape.json')}  tape.json")
            sha.write_text("\n".join(lines) + "\n")
        with self.lock:
            self._drop_share(tape_id)
        self._drop_faces(tape_id, removed)
        self._reindex(tape_id)
        return {"tape_id": tape_id, "removed": removed, "remaining": tape["scene_count"]}

    def _refresh_sha_lines(self, tape_dir: Path, new_hashes: dict[str, str]) -> None:
        sha = tape_dir / "tape.sha256"
        if not sha.exists():
            return
        out = []
        for ln in sha.read_text().splitlines():
            if not ln:
                continue
            name = ln.split("  ", 1)[-1]
            out.append(f"{new_hashes[name]}  {name}" if name in new_hashes else ln)
        sha.write_text("\n".join(out) + "\n")

    def rename_tape(self, tape_id: str, new_id: str) -> dict:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", new_id or ""):
            raise ValueError("niepoprawna nazwa kasety (dozwolone: litery, cyfry, . _ -)")
        with self.lock:
            if self._busy(tape_id):
                raise RuntimeError("kaseta jest w użyciu — poczekaj, aż zadanie się skończy")
            src = self.config.tapes / tape_id
            if src.parent != self.config.tapes or not src.is_dir():
                raise FileNotFoundError(f"nie ma kasety {tape_id}")
            if new_id == tape_id:
                return {"tape_id": new_id}
            if ((self.config.tapes / new_id).exists() or (self.config.working / new_id).exists()
                    or new_id in self.jobs):
                raise FileExistsError(f"nazwa zajęta: {new_id}")
            dst = self.config.tapes / new_id
            shutil.move(str(src), str(dst))
            hashes = {}
            tp = dst / "tape.json"
            tape = json.loads(tp.read_text())
            tape["tape_id"] = new_id
            tp.write_text(json.dumps(tape, indent=2) + "\n")
            for sj in dst.glob("[0-9]*.json"):
                m = json.loads(sj.read_text())
                m["tape_id"] = new_id
                sj.write_text(json.dumps(m, indent=2, ensure_ascii=False) + "\n")
                hashes[sj.name] = sha256(sj)
            hashes["tape.json"] = sha256(tp)
            self._refresh_sha_lines(dst, hashes)
            job = self.store.get_job(tape_id)
            if job:
                self.store.delete_job(tape_id)
                job["tape_id"] = new_id
                self.store.put_job(job)
            self._drop_share(tape_id)
            self.store.delete_index(tape_id)
            self.store.face_rename_tape(tape_id, new_id)
            self._persist()
        self._reindex(new_id)
        return {"tape_id": new_id}

    def set_tape_meta(self, tape_id: str, *, label=None, recording_date=None, undated_date=None) -> dict:
        """recording_date overrides every scene's date; undated_date only fills in
        scenes whose own date can't be trusted (dead camera clock), keeping the rest."""
        d = self._tape_dir(tape_id)
        tp = d / "tape.json"
        tape = json.loads(tp.read_text())
        if label is not None:
            lbl = str(label).strip()[:120]
            if lbl:
                tape["label"] = lbl
            else:
                tape.pop("label", None)
        for field, value in (("recording_date", recording_date), ("undated_date", undated_date)):
            if value is None:
                continue
            rd = str(value).strip()[:10]
            if rd and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", rd):
                raise ValueError("data w formacie RRRR-MM-DD")
            if rd:
                tape[field] = rd
            else:
                tape.pop(field, None)
        tp.write_text(json.dumps(tape, indent=2) + "\n")
        self._refresh_sha_lines(d, {"tape.json": sha256(tp)})
        self._reindex(tape_id)
        return {"tape_id": tape_id, "label": tape.get("label"), "recording_date": tape.get("recording_date"),
                "undated_date": tape.get("undated_date")}

    def set_scene_dates(self, tape_id: str, dates: dict) -> dict:
        """Per-scene date fixes from the operator: {scene_id: "YYYY-MM-DD" | "unknown"
        | ""} — "unknown" keeps a scene off every timeline (e.g. a dead camera clock
        whose date is plainly wrong), "" goes back to the automatic date. Stored in
        tape.json; the scenes' own files are never touched."""
        d = self._tape_dir(tape_id)
        tp = d / "tape.json"
        tape = json.loads(tp.read_text())
        known = {p.stem for p in d.glob("[0-9]*.json")}
        own = dict(tape.get("scene_dates") or {})
        for sid, value in (dates or {}).items():
            if sid not in known:
                raise FileNotFoundError(f"nie ma sceny {sid}")
            v = str(value or "").strip()[:10]
            if v and v != "unknown" and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
                raise ValueError("data w formacie RRRR-MM-DD albo 'unknown'")
            if v:
                own[sid] = v
            else:
                own.pop(sid, None)
        if own:
            tape["scene_dates"] = own
        else:
            tape.pop("scene_dates", None)
        tp.write_text(json.dumps(tape, indent=2) + "\n")
        self._refresh_sha_lines(d, {"tape.json": sha256(tp)})
        self._reindex(tape_id)
        return {"tape_id": tape_id, "scene_dates": own}

    # ---- id helpers ----------------------------------------------------
    def _next_tape_id(self) -> str:
        ids = [0]
        for root in (self.config.tapes, self.config.working):
            for path in root.glob("TAPE-[0-9][0-9][0-9][0-9]"):
                match = re.fullmatch(r"TAPE-(\d{4})", path.name)
                if match:
                    ids.append(int(match.group(1)))
        return f"TAPE-{max(ids) + 1:04d}"

    @staticmethod
    def _compat_override(allow_fcp: bool, manual_transport: bool, rewind: bool) -> tuple[bool, bool]:
        """With AV/C disabled (DCR-PC2E default) force manual transport and no auto-rewind."""
        if allow_fcp:
            return manual_transport, rewind
        return True, False

    # ---- capture --------------------------------------------------------
    def start(self, tape_id: str | None = None, rewind: bool = True, duration: int | None = None,
              manual_transport: bool = False, timezone: str | None = None, workspace: str | None = None) -> dict:
        manual_transport, rewind = self._compat_override(self.config.allow_fcp, manual_transport, rewind)
        with self.lock:
            if self.capture_tape or self.store.capture_job():
                raise RuntimeError("a capture is already in progress")
            tape_id = tape_id or self._next_tape_id()
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", tape_id):
                raise ValueError("invalid tape_id")
            if (self.config.tapes / tape_id).exists() or (self.config.working / tape_id).exists() or tape_id in self.jobs:
                raise FileExistsError(f"tape_id already exists: {tape_id}")
            timezone = timezone or None
            if timezone:
                try:
                    ZoneInfo(timezone)
                except ZoneInfoNotFoundError:
                    raise ValueError(f"nieznana strefa czasowa: {timezone}") from None
            workspace = (workspace or "").strip() or "default"
            if not WORKSPACE_RE.fullmatch(workspace):
                raise ValueError("nieprawidłowa nazwa workspace")
            if workspace != "default":
                self.store.ensure_workspace(workspace)
            job = self._new_job(tape_id, manual_transport, timezone, workspace)
            job["rewind"], job["duration"] = rewind, duration
            self.store.put_job(job)
            # role "all": run the capture in this process now.
            # role "api": the row is enough — the grabber process claims it from the store.
            if self.role == "all":
                self.capture_tape = tape_id
                self.capture_proc = None
                self.capture_cancel.clear()
                threading.Thread(target=self._capture_job, args=(job, rewind, duration, manual_transport),
                                 daemon=True).start()
            return dict(job)

    def stop(self, tape_id: str | None = None) -> dict:
        with self.lock:
            target = tape_id or self.capture_tape or (self.store.capture_job() or {}).get("tape_id")
            job = self.store.get_job(target) if target else None
            if not job:
                return {"status": "IDLE"}
            is_capture = job.get("stage") == "capture" and job.get("status") not in TERMINAL
            if is_capture:
                self.capture_cancel.set()
                self.store.mark_cancel(target)                       # cross-process signal for the grabber
                if self.capture_proc and self.capture_proc.poll() is None:
                    self.capture_proc.send_signal(signal.SIGINT)
                if not job.get("manual_transport"):
                    try:
                        self.camera.command("stop")
                    except Exception:
                        pass
            elif target in self.store.pending():
                self._set(job, "CANCELLED", message="removed from queue")
            elif job.get("status") not in TERMINAL:
                self.store.mark_cancel(target)
            return dict(self.store.get_job(target) or job)

    # ---- live preview (read-only tap on the growing raw DV, never touches FireWire) ----
    @staticmethod
    def _preview_source(work: Path) -> Path | None:
        """Whichever seg*.dv dvgrab is currently writing (highest index), or the
        merged capture001.dv once the capture has finished; None if neither exists yet."""
        segs = sorted(work.glob("seg[0-9][0-9][0-9].dv"))
        src = segs[-1] if segs else work / "capture001.dv"
        return src if src.exists() else None

    def preview_procs(self) -> tuple[subprocess.Popen, subprocess.Popen] | None:
        """A ``tail -f`` piped into ffmpeg -> low-res/low-fps MJPEG. Read-only and
        independent of the capture process — killing/losing this stream can never
        affect an in-progress capture."""
        job = self.store.capture_job()
        if not job:
            return None
        src = self._preview_source(self.config.working / job["tape_id"])
        if not src:
            return None
        tail = subprocess.Popen(["tail", "-c", "+0", "-f", str(src)], stdout=subprocess.PIPE)
        ff = subprocess.Popen(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "dv", "-i", "pipe:0",
             "-an", "-vf", "scale=480:-2,fps=8", "-q:v", "6",
             "-f", "mpjpeg", "-boundary_tag", "frame", "pipe:1"],
            stdin=tail.stdout, stdout=subprocess.PIPE)
        tail.stdout.close()  # tail gets SIGPIPE (not a silent hang) once ffmpeg exits
        return tail, ff

    def _acquire_dv(self, job: dict, work: Path, capture: Path, duration: int | None,
                    manual_transport: bool) -> None:
        """Run dvgrab into seg*.dv, relaunching if it quits early (blank tape makes dvgrab
        exit on its own). Stop when: the DV signal stops entirely (no_signal_timeout),
        or only blank frames have arrived for blank_tail_timeout (recording ended),
        or capture_idle_timeout as a backstop. Then concatenate segments -> capture001.dv."""
        cfg = self.config
        seg_base = str(work / "seg")
        log_file = (work / "capture.log").open("w")
        log_path = work / "capture.log"
        seg_index, started = 0, time.monotonic()
        last_total, last_growth, last_content, seen_data = 0, time.monotonic(), time.monotonic(), False
        last_progress = 0.0
        this_year = datetime.now(timezone.utc).year
        tc_re = re.compile(r"timecode (\d\d):(\d\d):(\d\d)\.\d\d(?: date (\d{4}))?")
        total = lambda: sum(p.stat().st_size for p in work.glob("seg[0-9][0-9][0-9].dv"))

        def tail_blank() -> bool:
            try:
                line = log_path.read_text(errors="replace").replace("\r", "\n").rstrip().rsplit("\n", 1)[-1]
            except OSError:
                return False
            m = tc_re.search(line)
            if not m:
                return False
            zero = m[1] == "00" and m[2] == "00" and m[3] == "00"
            future = bool(m[4]) and int(m[4]) >= this_year - 1  # camera outputs its RTC over blank tape
            return zero or future

        def stop_reason():
            if self.capture_cancel.is_set() or (self.role != "all" and self._store_cancelled(job)):
                return "cancel"
            if not seen_data:
                if time.monotonic() - started > cfg.play_wait_timeout:
                    raise RuntimeError("brak strumienia DV — nie naciśnięto PLAY / brak sygnału")
                return None
            if time.monotonic() - started > cfg.capture_max_seconds:
                self._log(job, "osiągnięto maksymalny czas zapisu — domykam to, co zebrano")
                return "done"
            idle, blanktail = time.monotonic() - last_growth, time.monotonic() - last_content
            if idle > cfg.no_signal_timeout:
                self._log(job, f"Koniec — brak sygnału DV przez {int(idle)} s (kamera stanęła / koniec taśmy)")
                return "done"
            if blanktail > cfg.blank_tail_timeout:
                self._log(job, f"Koniec nagrania — {int(blanktail)} s samych pustych klatek")
                return "done"
            if idle > cfg.capture_idle_timeout:
                self._log(job, f"Koniec — {int(idle)} s bez nowych danych")
                return "done"
            return None

        try:
            while not stop_reason():
                if seg_index >= cfg.max_dvgrab_restarts:
                    self._log(job, "osiągnięto limit restartów dvgrab — kończę zapis")
                    break
                seg_index += 1
                cmd = ["dvgrab", "-noavc", "-card", "0", "-format", "raw", "-size", "0", "-buffers", "500"]
                if duration:
                    cmd += ["-duration", f"{duration}s"]
                cmd += [seg_base]
                self.capture_proc = subprocess.Popen(cmd, stdout=log_file, stderr=subprocess.STDOUT, text=True)
                if seg_index == 1:
                    time.sleep(2)
                    if manual_transport:
                        self._log(job, "TRYB RĘCZNY DCR-PC2E: odbiornik DV gotowy — naciśnij PLAY na kamerze. "
                                  "Zapis domknie się automatycznie po wykryciu końca strumienia (lub naciśnij STOP).")
                    else:
                        self.camera.command("play")
                else:
                    self._log(job, f"dvgrab zakończył się sam — wznawiam, segment {seg_index}")
                while self.capture_proc.poll() is None:
                    tot = total()
                    if tot != last_total:
                        last_total, last_growth = tot, time.monotonic()
                        if not tail_blank():
                            last_content = time.monotonic()
                        if tot > 0 and not seen_data:
                            seen_data = True
                            self._set(job, "CAPTURING")
                    if tot > 0 and time.monotonic() - last_progress >= 3:
                        self._progress(job, tot)
                        last_progress = time.monotonic()
                    if stop_reason():
                        self.capture_proc.send_signal(signal.SIGINT)
                        break
                    time.sleep(1)
                try:
                    self.capture_proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self.capture_proc.kill()
                    self.capture_proc.wait()
                if not stop_reason():
                    time.sleep(2)  # brief pause before relaunch
        finally:
            log_file.close()
        segs = sorted(work.glob("seg[0-9][0-9][0-9].dv"))
        if len(segs) == 1:
            segs[0].rename(capture)
        elif segs:
            with capture.open("wb") as out:  # raw DV frames are self-contained -> plain concat
                for p in segs:
                    with p.open("rb") as src:
                        shutil.copyfileobj(src, out)
                    p.unlink()

    def _store_cancelled(self, job: dict) -> bool:
        row = self.store.get_job(job["tape_id"])
        return bool(row and row.get("cancel"))

    def _capture_job(self, job: dict, rewind: bool, duration: int | None, manual_transport: bool) -> None:
        tape_id = job["tape_id"]
        work = self.config.working / tape_id
        capture = work / "capture001.dv"
        try:
            self._set(job, "CHECKING_STORAGE")
            if not self.storage()["ready"]:
                raise RuntimeError("insufficient free space for safe capture")
            work.mkdir(parents=True)
            self._set(job, "CHECKING_CAMERA")
            deadline = time.monotonic() + self.config.camera_wait_timeout
            while not self.camera.info()["connected"]:
                if self.capture_cancel.is_set() or time.monotonic() > deadline:
                    raise CameraError("nie widać kamery na magistrali — wykonaj reset prądowy kamery i spróbuj ponownie")
                time.sleep(2)
            if rewind and not manual_transport:
                self._set(job, "REWINDING")
                self.camera.rewind_to_start(self.config.rewind_timeout, self.capture_cancel.is_set)
            self._set(job, "WAITING_FOR_PLAY" if manual_transport else "CAPTURING")
            self._acquire_dv(job, work, capture, duration, manual_transport)
            if not manual_transport:
                self.camera.command("stop")
            text = (work / "capture.log").read_text(errors="replace")
            self._log(job, text)
            drop_lines = [line.replace("\x07", "").strip() for line in text.splitlines()
                          if re.search(r"frame dropped", line, re.I)]
            job["dropped_frames"] = len(drop_lines)
            dv_bytes = capture.stat().st_size if capture.exists() else 0
            has_dv = dv_bytes >= self.config.keep_partial_min_seconds * 3_600_000  # ~DV25 byte rate
            if (self.capture_cancel.is_set() or self._store_cancelled(job)) and not has_dv:
                self._set(job, "CANCELLED")
                return
            if dv_bytes == 0:
                raise RuntimeError("capture produced no DV data")
            if self.capture_cancel.is_set() or self._store_cancelled(job):
                self._log(job, "Przerwano ręcznie — archiwizuję zebrany materiał")
                # a capture-stage cancel has now been honored (partial tape archived);
                # clear it before the processing stage, whose _set() guard would
                # otherwise read this same stale flag and abort before doing anything
                self.store.clear_cancel(tape_id)
                job["cancel"] = False
            job["capture_meta"] = {"started_at": job["created_at"], "drops": len(drop_lines),
                                   "drop_lines": drop_lines, "camera": self.camera.info()}
            self._set(job, "CAPTURED")
            self.store.enqueue_process(tape_id)
            with self.proc_cv:
                self.proc_cv.notify_all()
        except Exception as exc:
            self._log(job, f"ERROR: {type(exc).__name__}: {exc}")
            job["error"] = str(exc)
            if not manual_transport:
                try:
                    self.camera.command("stop")
                except Exception:
                    pass
            self._set(job, "ERROR")
        finally:
            if self.capture_proc and self.capture_proc.poll() is None:
                self.capture_proc.send_signal(signal.SIGINT)
                try:
                    self.capture_proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.capture_proc.kill()
            with self.lock:
                self.capture_proc = None
                if self.capture_tape == tape_id:
                    self.capture_tape = None
            if job["status"] in {"ERROR", "CANCELLED"} and not (capture.exists() and capture.stat().st_size > 0):
                shutil.rmtree(work, ignore_errors=True)

    def run_capture_from_store(self, job: dict) -> None:
        """Grabber entrypoint: a capture job already claimed from the store."""
        with self.lock:
            self.capture_tape = job["tape_id"]
            self.capture_proc = None
            self.capture_cancel.clear()
        self._capture_job(job, job.get("rewind", True), job.get("duration"),
                          job.get("manual_transport", False))

    # ---- processing worker -------------------------------------------------
    def _process_worker(self) -> None:
        poll = self.role != "all"
        while True:
            if poll:
                job = self.store.claim_next_process()
                if not job:
                    time.sleep(1)
                    continue
            else:
                with self.proc_cv:
                    while not self.store.pending():
                        self.proc_cv.wait()
                job = self.store.claim_next_process()
                if not job:
                    continue
            with self.lock:
                self.processing_tape = job["tape_id"]
            try:
                self._run_processing(job)
            finally:
                with self.lock:
                    self.processing_tape = None

    def _run_processing(self, job: dict) -> None:
        tape_id = job["tape_id"]
        work = self.config.working / tape_id
        capture = work / "capture001.dv"
        meta = job.get("capture_meta") or {"started_at": job["created_at"], "drops": 0, "drop_lines": [],
                                           "camera": self.camera.info()}
        job["stage"] = "process"
        job["processing_started_at"] = now()
        try:
            if not capture.exists() or capture.stat().st_size == 0:
                raise RuntimeError("raw DV missing for processing")
            self._set(job, "ANALYZING_DV")
            tape = process_capture(capture, tape_id, self.config.storage, self.config.zstd_level,
                                   lambda m: self._log(job, m),
                                   lambda s, sid=None, total=None: self._set(job, s, sid, total=total),
                                   meta["drop_lines"], nice=self.config.nice_processing,
                                   mp4_preset=self.config.mp4_preset,
                                   recording_tz=job.get("timezone") or self.config.default_recording_tz)
            tape_path = self.config.tapes / tape_id / "tape.json"
            tape["capture_started_at"] = meta["started_at"]
            tape["capture_completed_at"] = now()
            tape["camera"] = meta["camera"]
            tape["capture_dropped_frames"] = meta["drops"]
            tape["workspace"] = job.get("workspace") or "default"
            tape_path.write_text(json.dumps(tape, indent=2) + "\n")
            checksum_path = tape_path.parent / "tape.sha256"
            lines = [line for line in checksum_path.read_text().splitlines() if not line.endswith("  tape.json")]
            lines.append(f"{sha256(tape_path)}  tape.json")
            checksum_path.write_text("\n".join(lines) + "\n")
            capture.unlink()
            shutil.rmtree(work, ignore_errors=True)
            self._reindex(tape_id)
            self._set(job, "COMPLETED")
        except Exception as exc:
            self._log(job, f"ERROR: {type(exc).__name__}: {exc}")
            job["error"] = str(exc)
            with self.lock:
                job["status"] = "ERROR"
                job["updated_at"] = now()
                job["history"].append({"status": "ERROR", "at": now()})
                self.store.put_job(job)   # terminal — a stale cancel no longer matters
                self._persist()

    def process_existing(self, source: Path, tape_id: str) -> dict:
        work = self.config.working / tape_id
        if work.exists() or (self.config.tapes / tape_id).exists():
            raise FileExistsError(tape_id)
        work.mkdir(parents=True)
        capture = work / "capture001.dv"
        shutil.copy2(source, capture)
        (work / "capture.log").write_text(f"Offline validation source: {source}\n")
        job = self._new_job(tape_id, manual_transport=True)
        job["stage"] = "process"
        job["status"] = "ANALYZING_DV"
        self.store.put_job(job)
        result = process_capture(capture, tape_id, self.config.storage, self.config.zstd_level,
                                 lambda m: self._log(job, m),
                                 lambda s, sid=None, total=None: self._set(job, s, sid, total=total),
                                 nice=self.config.nice_processing, mp4_preset=self.config.mp4_preset,
                                 recording_tz=self.config.default_recording_tz)
        capture.unlink()
        shutil.rmtree(work, ignore_errors=True)
        self._reindex(tape_id)
        self._set(job, "COMPLETED")
        return result
