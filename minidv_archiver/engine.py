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
from datetime import datetime, timezone
from pathlib import Path

from .camera import Camera, CameraError
from .config import Config
from .media import process_capture, sha256

TERMINAL = {"COMPLETED", "ERROR", "CANCELLED"}
MAX_JOBS = 30


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Engine:
    """Capture is an exclusive FireWire slot; processing runs in a background queue,
    so a new tape can be captured while the previous one is still being archived."""

    def __init__(self, config: Config):
        self.config = config
        config.ensure_dirs()
        self.camera = Camera(config.camera_guid)
        self.lock = threading.RLock()
        self.proc_cv = threading.Condition(self.lock)
        self.jobs: dict[str, dict] = {}
        self.pending: list[str] = []          # tape_ids waiting for the processing worker
        self.capture_tape: str | None = None  # tape_id holding the FireWire slot
        self.capture_proc: subprocess.Popen | None = None
        self.capture_cancel = threading.Event()
        self.processing_tape: str | None = None
        self.compress: dict[str, dict] = {}   # on-demand "share to FB" re-encodes, key "<tape>/<scene|TAPE>"
        self.compress_pending: list[str] = []
        self._load_jobs()
        threading.Thread(target=self._process_worker, daemon=True).start()
        threading.Thread(target=self._compress_worker, daemon=True).start()

    # ---- storage -----------------------------------------------------------
    def storage(self) -> dict:
        usage = shutil.disk_usage(self.config.storage)
        safe = self.config.min_free_gib * 1024**3
        usable = max(0, usage.free - safe)
        return {"path": str(self.config.storage), "total": usage.total, "used": usage.used, "free": usage.free,
                "min_free": safe, "ready": usage.free >= safe, "estimated_dv_hours": round(usable / 12.96e9, 1)}

    # ---- job registry ----------------------------------------------------
    def _load_jobs(self) -> None:
        path = self.config.state / "jobs.json"
        try:
            self.jobs = json.loads(path.read_text())
        except (OSError, ValueError):
            self.jobs = {}
        # No capture survives a restart; resume processing where the raw DV is still on disk.
        for tape_id, job in list(self.jobs.items()):
            if job.get("status") in TERMINAL:
                continue
            tape_dir = self.config.tapes / tape_id
            dv = self.config.working / tape_id / "capture001.dv"
            if (tape_dir / "tape.json").exists():
                job["stage"], job["status"] = "process", "COMPLETED"  # finished right before the restart
                shutil.rmtree(self.config.working / tape_id, ignore_errors=True)
            elif dv.exists() and dv.stat().st_size > 0:
                job["stage"], job["status"] = "process", "QUEUED"
                shutil.rmtree(tape_dir, ignore_errors=True)  # drop the half-written archive; redo from raw DV
                self.pending.append(tape_id)
            else:
                job["status"] = "ERROR"
                job["error"] = "interrupted by a restart"
                shutil.rmtree(self.config.working / tape_id, ignore_errors=True)
        self._persist()

    def _persist(self) -> None:
        keep = sorted(self.jobs.values(), key=lambda j: j.get("updated_at", ""), reverse=True)
        drop = [j["tape_id"] for j in keep[MAX_JOBS:] if j.get("status") in TERMINAL]
        for tid in drop:
            self.jobs.pop(tid, None)
        (self.config.state / "jobs.json").write_text(json.dumps(self.jobs, indent=2) + "\n")

    def _set(self, job: dict, status: str, scene_id=None, message=None) -> None:
        with self.lock:
            if job.get("cancel") and status not in TERMINAL:
                raise RuntimeError("processing cancelled")
            job["status"] = status
            job["updated_at"] = now()
            if scene_id:
                job["current_scene"] = scene_id
            job["history"].append({"status": status, "at": now(), **({"message": message} if message else {})})
            self._persist()

    def _log(self, job: dict, message: str) -> None:
        if not message:
            return
        with self.lock:
            job["logs"] = (job["logs"] + message + "\n")[-20000:]
            self._persist()

    def _new_job(self, tape_id: str, manual_transport: bool) -> dict:
        return {"id": uuid.uuid4().hex, "tape_id": tape_id, "stage": "capture", "status": "CREATED",
                "created_at": now(), "updated_at": now(), "current_scene": None, "dropped_frames": 0,
                "logs": "", "history": [], "manual_transport": manual_transport, "error": None}

    def jobs_list(self) -> list[dict]:
        with self.lock:
            return [dict(j) for j in sorted(self.jobs.values(), key=lambda j: j.get("updated_at", ""), reverse=True)]

    def job_by_ref(self, ref: str) -> dict | None:
        with self.lock:
            if ref in self.jobs:
                return dict(self.jobs[ref])
            return next((dict(j) for j in self.jobs.values() if j.get("id") == ref), None)

    def current_job(self) -> dict:
        with self.lock:
            if self.capture_tape:
                return dict(self.jobs[self.capture_tape])
            jobs = self.jobs_list()
            return jobs[0] if jobs else {"status": "IDLE"}

    def status(self) -> dict:
        with self.lock:
            cap = dict(self.jobs[self.capture_tape]) if self.capture_tape else None
            proc = dict(self.jobs[self.processing_tape]) if self.processing_tape else None
            return {"capture": cap, "processing": proc, "queue": list(self.pending),
                    "jobs": self.jobs_list(), "compress": [dict(c) for c in self.compress.values()]}

    # ---- on-demand re-encode / download builds ----------------------------
    def _prune_share(self) -> None:
        share = self.config.storage / "tmp" / "share"
        cutoff = time.time() - 2 * 3600
        for p in share.glob("*.mp4") if share.exists() else []:
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink()
            except OSError:
                pass

    def _enqueue_build(self, tape_id: str, token: str, sources: list[Path], mode: str, title: str) -> dict:
        key = f"{tape_id}/{token}"
        with self.proc_cv:
            job = self.compress.get(key)
            if job and (job["status"] in ("QUEUED", "RUNNING")
                        or (job["status"] == "READY" and Path(job["path"]).exists())):
                return dict(job)
            out = self.config.storage / "tmp" / "share" / f"{tape_id}_{token}.mp4"
            job = {"key": key, "token": token, "tape_id": tape_id, "mode": mode, "title": title,
                   "sources": [str(p) for p in sources], "status": "QUEUED", "path": str(out),
                   "size": None, "error": None, "updated_at": now()}
            self.compress[key] = job
            self.compress_pending.append(key)
            self.proc_cv.notify_all()
        return dict(job)

    def _tape_dir(self, tape_id: str) -> Path:
        d = self.config.tapes / tape_id
        if d.parent != self.config.tapes or not (d / "tape.json").exists():
            raise FileNotFoundError(f"nie ma kasety {tape_id}")
        return d

    def start_compress(self, tape_id: str, scene_id: str | None) -> dict:
        """FB-size re-encode of one scene, or the whole tape (scene_id=None)."""
        d = self._tape_dir(tape_id)
        src = d / ("tape.mp4" if scene_id is None else f"{scene_id}.mp4")
        if not src.exists():
            raise FileNotFoundError(f"nie ma pliku {src.name}")
        title = f"{tape_id}" + (f" · scena {scene_id}" if scene_id else " — cała taśma")
        return self._enqueue_build(tape_id, scene_id or "TAPE", [src], "share", title)

    def start_selection(self, tape_id: str, scene_ids: list[str], share: bool = False) -> dict:
        """Join several scenes into one MP4 — lossless stream copy, or a ~FB-size re-encode."""
        d = self._tape_dir(tape_id)
        ids = sorted({s for s in scene_ids if re.fullmatch(r"[0-9A-Za-z._-]{1,80}", s or "")})
        srcs = [d / f"{s}.mp4" for s in ids]
        missing = [p.name for p in srcs if not p.exists()]
        if not srcs or missing:
            raise FileNotFoundError("brak scen: " + ", ".join(missing) if missing else "pusta lista scen")
        token = "SEL-" + hashlib.sha1("\n".join(ids).encode()).hexdigest()[:12] + ("-FB" if share else "")
        return self._enqueue_build(tape_id, token, srcs, "share" if share else "concat",
                                   f"{tape_id} · {len(ids)} scen")

    def _compress_worker(self) -> None:
        while True:
            with self.proc_cv:
                while not self.compress_pending:
                    self.proc_cv.wait()
                key = self.compress_pending.pop(0)
                job = self.compress.get(key)
                if not job:
                    continue
                job["status"], job["updated_at"] = "RUNNING", now()
            out = Path(job["path"])
            try:
                from .media import compress_share, concat_mp4
                out.parent.mkdir(parents=True, exist_ok=True)
                self._prune_share()
                srcs = [Path(s) for s in job["sources"]]
                if job["mode"] == "concat":
                    concat_mp4(srcs, out, meta={"title": job["title"]}, log=lambda m: None)
                else:
                    compress_share(srcs if len(srcs) > 1 else srcs[0], out, max_mb=self.config.share_max_mb,
                                   crf=self.config.share_crf, preset=self.config.share_preset,
                                   meta={"title": job["title"]}, log=lambda m: None)
                with self.lock:
                    job.update(status="READY", size=out.stat().st_size, updated_at=now())
            except Exception as exc:
                with self.lock:
                    job.update(status="ERROR", error=str(exc), updated_at=now())

    def compress_status(self, tape_id: str, token: str | None) -> dict:
        with self.lock:
            return dict(self.compress.get(f"{tape_id}/{token or 'TAPE'}", {"status": "NONE"}))

    # ---- deletion (irreversible — the caller must have confirmed) ----------
    def _drop_share(self, tape_id: str) -> None:
        for k in [k for k in self.compress if k.startswith(f"{tape_id}/")]:
            job = self.compress.pop(k)
            Path(job["path"]).unlink(missing_ok=True)

    def delete_tape(self, tape_id: str) -> dict:
        with self.lock:
            if tape_id in (self.capture_tape, self.processing_tape) or tape_id in self.pending:
                raise RuntimeError("kaseta jest w użyciu — poczekaj, aż zadanie się skończy")
            d = self.config.tapes / tape_id
            if d.parent != self.config.tapes or not d.is_dir():
                raise FileNotFoundError(f"nie ma kasety {tape_id}")
            shutil.rmtree(d, ignore_errors=True)
            shutil.rmtree(self.config.working / tape_id, ignore_errors=True)
            self.jobs.pop(tape_id, None)
            self._drop_share(tape_id)
            self._persist()
        return {"deleted": tape_id}

    def delete_scenes(self, tape_id: str, scene_ids: list[str]) -> dict:
        with self.lock:
            if tape_id in (self.capture_tape, self.processing_tape) or tape_id in self.pending:
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
        return {"tape_id": tape_id, "removed": removed, "remaining": tape["scene_count"]}

    # ---- rename / editable metadata --------------------------------------
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
            if tape_id in (self.capture_tape, self.processing_tape) or tape_id in self.pending:
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
            if tape_id in self.jobs:
                j = self.jobs.pop(tape_id)
                j["tape_id"] = new_id
                self.jobs[new_id] = j
            self._drop_share(tape_id)
            self._persist()
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
            if self.capture_tape:
                raise RuntimeError("a capture is already in progress")
            tape_id = tape_id or self._next_tape_id()
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", tape_id):
                raise ValueError("invalid tape_id")
            if (self.config.tapes / tape_id).exists() or (self.config.working / tape_id).exists() or tape_id in self.jobs:
                raise FileExistsError(f"tape_id already exists: {tape_id}")
            job = self._new_job(tape_id, manual_transport)
            self.jobs[tape_id] = job
            self.capture_tape = tape_id
            self.capture_proc = None
            self.capture_cancel.clear()
            self._persist()
            threading.Thread(target=self._capture_job, args=(job, rewind, duration, manual_transport),
                             daemon=True).start()
            return dict(job)

    def stop(self, tape_id: str | None = None) -> dict:
        with self.lock:
            target = tape_id or self.capture_tape
            job = self.jobs.get(target) if target else None
            if not job:
                return {"status": "IDLE"}
            if target == self.capture_tape:
                self.capture_cancel.set()
                if self.capture_proc and self.capture_proc.poll() is None:
                    self.capture_proc.send_signal(signal.SIGINT)
                if not job.get("manual_transport"):
                    try:
                        self.camera.command("stop")
                    except Exception:
                        pass
            elif target in self.pending:
                self.pending.remove(target)
                self._set(job, "CANCELLED", message="removed from queue")
            elif job.get("status") not in TERMINAL:
                job["cancel"] = True  # picked up between scenes by the processing worker
            return dict(job)

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
            if self.capture_cancel.is_set():
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
            if self.capture_cancel.is_set() and not has_dv:
                self._set(job, "CANCELLED")
                return
            if dv_bytes == 0:
                raise RuntimeError("capture produced no DV data")
            if self.capture_cancel.is_set():
                self._log(job, "Przerwano ręcznie — archiwizuję zebrany materiał")
            job["capture_meta"] = {"started_at": job["created_at"], "drops": len(drop_lines),
                                   "drop_lines": drop_lines, "camera": self.camera.info()}
            self._set(job, "CAPTURED")
            with self.proc_cv:
                self.pending.append(tape_id)
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

    # ---- processing worker -------------------------------------------------
    def _process_worker(self) -> None:
        while True:
            with self.proc_cv:
                while not self.pending:
                    self.proc_cv.wait()
                tape_id = self.pending.pop(0)
                self.processing_tape = tape_id
                job = self.jobs.get(tape_id)
            try:
                if job:
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
        try:
            if not capture.exists() or capture.stat().st_size == 0:
                raise RuntimeError("raw DV missing for processing")
            self._set(job, "ANALYZING_DV")
            tape = process_capture(capture, tape_id, self.config.storage, self.config.zstd_level,
                                   lambda m: self._log(job, m),
                                   lambda s, sid=None: self._set(job, s, sid),
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
            self._set(job, "COMPLETED")
        except Exception as exc:
            self._log(job, f"ERROR: {type(exc).__name__}: {exc}")
            job["error"] = str(exc)
            with self.lock:
                job["status"] = "ERROR"
                job["updated_at"] = now()
                job["history"].append({"status": "ERROR", "at": now()})
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
        self.jobs[tape_id] = job
        result = process_capture(capture, tape_id, self.config.storage, self.config.zstd_level,
                                 lambda m: self._log(job, m), lambda s, sid=None: self._set(job, s, sid),
                                 nice=self.config.nice_processing, mp4_preset=self.config.mp4_preset)
        capture.unlink()
        shutil.rmtree(work, ignore_errors=True)
        self._set(job, "COMPLETED")
        return result
