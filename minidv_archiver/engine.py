from __future__ import annotations

import hashlib
import json
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

from . import library_index
from .camera import Camera, CameraError
from .config import Config
from .media import process_capture, sha256
from .store import TERMINAL, JobStore, now

MAX_JOBS = 30

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
        if not message:
            return
        with self.lock:
            job["logs"] = (job["logs"] + message + "\n")[-20000:]
            self._write_job(job)

    def _progress(self, job: dict, captured_bytes: int) -> None:
        """Cheap periodic update (no history entry) so the UI can show how far into
        the tape a capture is — elapsed time + bytes so far, from the growing seg*.dv."""
        with self.lock:
            job["captured_bytes"] = captured_bytes
            job["updated_at"] = now()
            self._write_job(job)

    def _new_job(self, tape_id: str, manual_transport: bool) -> dict:
        return {"id": uuid.uuid4().hex, "tape_id": tape_id, "stage": "capture", "status": "CREATED",
                "created_at": now(), "updated_at": now(), "current_scene": None, "dropped_frames": 0,
                "logs": "", "history": [], "manual_transport": manual_transport, "error": None,
                "rewind": True, "duration": None}

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

    def tapes(self) -> list[dict]:
        return library_index.tapes_list(self.config, self.store)

    def timeline(self) -> dict:
        return library_index.timeline(self.config, self.store)

    def timeline_month(self, year: int, month: int) -> list[dict]:
        return library_index.month_scenes(self.config, self.store, year, month)

    def duplicates(self) -> dict:
        return library_index.duplicates(self.config, self.store)

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

    def _enqueue_build(self, tape_id: str, token: str, sources: list[Path], mode: str, title: str) -> dict:
        key = f"{tape_id}/{token}"
        with self.proc_cv:
            existing = self.store.get_build(key)
            if existing and (existing["status"] in ("QUEUED", "RUNNING")
                             or (existing["status"] == "READY" and Path(existing["path"]).exists())):
                return dict(existing)
            out = self.config.storage / "tmp" / "share" / f"{tape_id}_{token}.mp4"
            build = {"key": key, "token": token, "tape_id": tape_id, "mode": mode, "title": title,
                     "sources": [str(p) for p in sources], "status": "QUEUED", "path": str(out),
                     "size": None, "error": None}
            self.store.put_build(build)
            self.proc_cv.notify_all()
        return dict(self.store.get_build(key))

    def _tape_dir(self, tape_id: str) -> Path:
        d = self.config.tapes / tape_id
        if d.parent != self.config.tapes or not (d / "tape.json").exists():
            raise FileNotFoundError(f"nie ma kasety {tape_id}")
        return d

    def start_compress(self, tape_id: str, scene_id: str | None, restore: bool = False) -> dict:
        """FB-size re-encode of one scene / the whole tape (scene_id=None), or —
        with restore=True — a denoise/repair MP4 built from the DV master(s)."""
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
            return self._enqueue_build(tape_id, (scene_id or "TAPE") + "-RES", srcs, "restore",
                                       f"{tape_id}{label} · naprawiony")
        src = d / ("tape.mp4" if scene_id is None else f"{scene_id}.mp4")
        if not src.exists():
            raise FileNotFoundError(f"nie ma pliku {src.name}")
        return self._enqueue_build(tape_id, scene_id or "TAPE", [src], "share", f"{tape_id}{label}")

    def start_selection(self, tape_id: str, scene_ids: list[str], share: bool = False,
                        restore: bool = False) -> dict:
        """Join several scenes into one MP4 — stream copy, ~FB-size re-encode, or a
        repair pass from those scenes' DV masters (restore=True)."""
        d = self._tape_dir(tape_id)
        ids = sorted({s for s in scene_ids if re.fullmatch(r"[0-9A-Za-z._-]{1,80}", s or "")})
        suffix = ".dv.zst" if restore else ".mp4"
        srcs = [d / f"{s}{suffix}" for s in ids]
        missing = [p.name for p in srcs if not p.exists()]
        if not srcs or missing:
            raise FileNotFoundError("brak scen: " + ", ".join(missing) if missing else "pusta lista scen")
        tag = "-RES" if restore else "-FB" if share else ""
        token = "SEL-" + hashlib.sha1("\n".join(ids).encode()).hexdigest()[:12] + tag
        mode = "restore" if restore else "share" if share else "concat"
        return self._enqueue_build(tape_id, token, srcs, mode, f"{tape_id} · {len(ids)} scen")

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
                    while not any(b["status"] == "QUEUED" for b in self.store.list_builds()):
                        self.proc_cv.wait()
                build = self.store.claim_next_build()
                if not build:
                    continue
            out = Path(build["path"])
            try:
                from .media import compress_share, concat_mp4, restore_mp4
                out.parent.mkdir(parents=True, exist_ok=True)
                self._prune_share()
                self.store.prune_builds()
                srcs = [Path(s) for s in build["sources"]]
                if build["mode"] == "reprobe":
                    self._reprobe_tape(build["sources"][0], force=build["token"].endswith("-F"))
                elif build["mode"] == "concat":
                    concat_mp4(srcs, out, meta={"title": build["title"]}, log=lambda m: None)
                elif build["mode"] == "restore":
                    restore_mp4(srcs, out, vf=self.config.restore_filters, crf=self.config.restore_crf,
                                preset=self.config.restore_preset, decimate=self.config.restore_decimate,
                                meta={"title": build["title"]}, log=lambda m: None)
                else:
                    compress_share(srcs if len(srcs) > 1 else srcs[0], out, max_mb=self.config.share_max_mb,
                                   crf=self.config.share_crf, preset=self.config.share_preset,
                                   meta={"title": build["title"]}, log=lambda m: None)
                build.update(status="READY", size=None if build["mode"] == "reprobe" else out.stat().st_size)
                self.store.put_build(build)
            except Exception as exc:
                build.update(status="ERROR", error=str(exc))
                self.store.put_build(build)

    def compress_status(self, tape_id: str, token: str | None) -> dict:
        build = self.store.get_build(f"{tape_id}/{token or 'TAPE'}")
        return dict(build) if build else {"status": "NONE"}

    # ---- deletion / rename / metadata (irreversible — the caller must have confirmed) ----
    def _busy(self, tape_id: str) -> bool:
        return self.store.tape_busy(tape_id) or tape_id in (self.capture_tape, self.processing_tape)

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
        return {"deleted": tape_id}

    def delete_scenes(self, tape_id: str, scene_ids: list[str]) -> dict:
        with self.lock:
            if self._busy(tape_id):
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
            self._persist()
        self._reindex(new_id)
        return {"tape_id": new_id}

    def set_tape_meta(self, tape_id: str, *, label=None, recording_date=None) -> dict:
        d = self._tape_dir(tape_id)
        tp = d / "tape.json"
        tape = json.loads(tp.read_text())
        if label is not None:
            lbl = str(label).strip()[:120]
            if lbl:
                tape["label"] = lbl
            else:
                tape.pop("label", None)
        if recording_date is not None:
            rd = str(recording_date).strip()[:10]
            if rd and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", rd):
                raise ValueError("data w formacie RRRR-MM-DD")
            if rd:
                tape["recording_date"] = rd
            else:
                tape.pop("recording_date", None)
        tp.write_text(json.dumps(tape, indent=2) + "\n")
        self._refresh_sha_lines(d, {"tape.json": sha256(tp)})
        self._reindex(tape_id)
        return {"tape_id": tape_id, "label": tape.get("label"), "recording_date": tape.get("recording_date")}

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
              manual_transport: bool = False) -> dict:
        manual_transport, rewind = self._compat_override(self.config.allow_fcp, manual_transport, rewind)
        with self.lock:
            if self.capture_tape or self.store.capture_job():
                raise RuntimeError("a capture is already in progress")
            tape_id = tape_id or self._next_tape_id()
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", tape_id):
                raise ValueError("invalid tape_id")
            if (self.config.tapes / tape_id).exists() or (self.config.working / tape_id).exists() or tape_id in self.jobs:
                raise FileExistsError(f"tape_id already exists: {tape_id}")
            job = self._new_job(tape_id, manual_transport)
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
                                   mp4_preset=self.config.mp4_preset)
            tape_path = self.config.tapes / tape_id / "tape.json"
            tape["capture_started_at"] = meta["started_at"]
            tape["capture_completed_at"] = now()
            tape["camera"] = meta["camera"]
            tape["capture_dropped_frames"] = meta["drops"]
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
                                 nice=self.config.nice_processing, mp4_preset=self.config.mp4_preset)
        capture.unlink()
        shutil.rmtree(work, ignore_errors=True)
        self._reindex(tape_id)
        self._set(job, "COMPLETED")
        return result
