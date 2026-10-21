"""Face recognition for the "Osoby" (People) view.

Per scene proxy MP4:
  ffmpeg decode -> per second of video, the first frame that isn't blurred
  (variance of the Laplacian >= faces_blur; a blurry frame is skipped and the next
  one of that second tried; a second with no sharp frame is skipped) -> YuNet face
  detector -> SFace 128-d embedding -> faces of one person within the scene fold
  into a single *track* (the seconds they're visible + best thumbnail) -> each
  track joins the nearest *person* (centroid cosine >= faces_match) of the tape's
  own workspace, or starts a new unnamed one the operator can name later.

Persons never cross workspaces: matching, merging and moving are all scoped by
the workspace stored on the tape, so two families' footage can't mix.

The scan is one singleton build row (store.FACES_KEY): visible in Zadania with a
progress bar, cancellable, refused while another is QUEUED/RUNNING. Results are
committed per scene, so a cancel/restart loses at most the scene in flight and
the next run continues where this one stopped. It pauses while a capture or a
tape conversion runs, and runs its ffmpeg + its own thread at idle priority.

cv2/numpy are imported only by the scanner; the api's person edits (rename,
merge, move) use the pure-stdlib vector helpers below.
"""
from __future__ import annotations

import math
import os
import shutil
import subprocess
import sys
import threading
import time
from array import array
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from . import face_models
from .store import FACES_KEY

MODEL_TAG = face_models.TAG
# within one scene, two detections this similar are the same person (stricter
# than the cross-scene match: same lighting/camera, so true matches score high)
TRACK_SIM = 0.45
THUMB_PX = 160
AUTO_WINDOW_H = 6


class ScanCancelled(Exception):
    pass


def available() -> bool:
    try:
        import cv2  # noqa: F401
        import numpy  # noqa: F401
        import onnxruntime  # noqa: F401
    except ImportError:
        return False
    return True


def thumb_path(config, track_id: int) -> Path:
    return config.storage / "faces" / f"{int(track_id)}.jpg"


# ---- pure-stdlib vectors (float32 blobs) ---------------------------------
def _vec(blob: bytes) -> array:
    a = array("f")
    a.frombytes(blob)
    return a


def _normalized(v) -> array:
    s = math.sqrt(sum(x * x for x in v)) or 1.0
    return array("f", (x / s for x in v))


def recompute_person(store, person_id: int) -> None:
    """Centroid = normalized mean of the person's track embeddings."""
    tracks = store.face_tracks(person_id, with_embedding=True)
    if not tracks:
        store.face_person_set_centroid(person_id, None, 0)
        return
    acc = [0.0] * len(_vec(tracks[0]["embedding"]))
    for t in tracks:
        for i, x in enumerate(_vec(t["embedding"])):
            acc[i] += x
    store.face_person_set_centroid(person_id, _normalized(acc).tobytes(), len(tracks))


def drop_thumbs(config, track_ids) -> None:
    for tid in track_ids:
        thumb_path(config, tid).unlink(missing_ok=True)


# ---- models -------------------------------------------------------------
def model_dir(config) -> Path:
    return Path(config.faces_models) if config.faces_models else config.state / "models"


def ensure_models(config, log=lambda m: None) -> dict[str, Path]:
    return face_models.fetch(model_dir(config), log)


# ---- detection + embedding ----------------------------------------------
class FaceAnalyzer:
    def __init__(self, paths: dict[str, Path], *, threads: int = 1, min_size: int = 36, score: float = 0.8):
        import cv2
        import numpy as np
        cv2.setNumThreads(max(1, threads))
        self.cv2, self.np = cv2, np
        self.det = cv2.FaceDetectorYN.create(str(paths["detector"]), "", (320, 320), score, 0.3, 5000)
        self.rec = cv2.FaceRecognizerSF.create(str(paths["recognizer"]), "")
        self.min_size = min_size

    def sharpness(self, frame) -> float:
        cv2 = self.cv2
        small = cv2.resize(frame, (frame.shape[1] // 2, frame.shape[0] // 2), interpolation=cv2.INTER_AREA)
        return float(cv2.Laplacian(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY), cv2.CV_64F).var())

    def faces(self, frame) -> list[dict]:
        np, cv2 = self.np, self.cv2
        h, w = frame.shape[:2]
        self.det.setInputSize((w, h))
        _, rows = self.det.detect(frame)
        out = []
        for r in rows if rows is not None else []:
            x, y, fw, fh = (float(v) for v in r[:4])
            if min(fw, fh) < self.min_size:
                continue
            emb = self.rec.feature(self.rec.alignCrop(frame, r)).flatten().astype(np.float32)
            emb /= (np.linalg.norm(emb) or 1.0)
            # square crop around the box with some margin, for the round avatar
            side = max(fw, fh) * 1.5
            cx, cy = x + fw / 2, y + fh / 2
            x0, y0 = int(max(0, cx - side / 2)), int(max(0, cy - side / 2))
            x1, y1 = int(min(w, cx + side / 2)), int(min(h, cy + side / 2))
            crop = cv2.resize(frame[y0:y1, x0:x1], (THUMB_PX, THUMB_PX), interpolation=cv2.INTER_AREA)
            ok, jpg = cv2.imencode(".jpg", crop, [cv2.IMWRITE_JPEG_QUALITY, 85])
            out.append({"emb": emb, "quality": float(r[-1]) * min(fw, fh), "jpg": jpg.tobytes() if ok else None})
        return out


def _probe(mp4: Path) -> tuple[int, int, float, float]:
    """(display width, height, fps, duration) — width with the pixel aspect
    applied, so faces aren't squashed (DV is 720x576 at a 16:15 / 64:45 SAR)."""
    from .media import ffprobe
    info = ffprobe(mp4)
    v = next(s for s in info.get("streams", []) if s.get("codec_type") == "video")
    w, h = int(v["width"]), int(v["height"])
    num, _, den = (v.get("sample_aspect_ratio") or "1:1").partition(":")
    try:
        sar = int(num) / int(den) if int(num) and int(den) else 1.0
    except ValueError:
        sar = 1.0
    fnum, _, fden = (v.get("r_frame_rate") or "25/1").partition("/")
    fps = (int(fnum) / int(fden or 1)) if int(fnum or 0) else 25.0
    dur = float(v.get("duration") or info.get("format", {}).get("duration") or 0)
    return max(2, int(round(w * sar / 2)) * 2), h, fps, dur


def sharp_frames(mp4: Path, analyzer: FaceAnalyzer, blur: float, nice: bool = True):
    """Yield (second, frame, duration): per second of video the first frame whose
    sharpness reaches ``blur``."""
    np = analyzer.np
    w, h, fps, dur = _probe(mp4)
    prefix = []
    if nice and shutil.which("nice"):
        prefix = ["nice", "-n", "19"] + (["ionice", "-c", "3"] if shutil.which("ionice") else [])
    cmd = [*prefix, "ffmpeg", "-nostdin", "-v", "error", "-threads", "1", "-i", str(mp4), "-an",
           "-vf", f"scale={w}:{h},setsar=1", "-pix_fmt", "bgr24", "-f", "rawvideo", "-"]
    size = w * h * 3
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=size * 2)
    try:
        idx, done_sec = 0, -1
        while True:
            buf = proc.stdout.read(size)
            if len(buf) < size:
                break
            sec = int(idx / fps)
            idx += 1
            if sec == done_sec:
                continue
            frame = np.frombuffer(buf, np.uint8).reshape(h, w, 3)
            if analyzer.sharpness(frame) < blur:
                continue
            done_sec = sec
            yield sec, frame, dur
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()


def scan_scene(mp4: Path, analyzer: FaceAnalyzer, blur: float, on_frac=lambda f: None,
               check=lambda: None) -> list[dict]:
    """Tracks for one scene: [{emb, times, quality, jpg}]."""
    np = analyzer.np
    tracks: list[dict] = []
    for sec, frame, dur in sharp_frames(mp4, analyzer, blur):
        check()
        if dur:
            on_frac(min(1.0, sec / dur))
        for f in analyzer.faces(frame):
            best, best_sim = None, TRACK_SIM
            for t in tracks:
                s = float(np.dot(t["emb"], f["emb"]))
                if s >= best_sim:
                    best, best_sim = t, s
            if best is None:
                tracks.append({"sum": f["emb"].copy(), "emb": f["emb"], "times": [sec],
                               "quality": f["quality"], "jpg": f["jpg"]})
                continue
            best["sum"] += f["emb"]
            best["emb"] = best["sum"] / (np.linalg.norm(best["sum"]) or 1.0)
            if best["times"][-1] != sec:
                best["times"].append(sec)
            if f["quality"] > best["quality"] and f["jpg"]:
                best["quality"], best["jpg"] = f["quality"], f["jpg"]
    return tracks


class Matcher:
    """Assigns tracks to persons of one workspace via an in-memory centroid
    matrix; reloads when the operator edits persons (store.faces_gen)."""

    def __init__(self, store, threshold: float):
        import numpy as np
        self.np, self.store, self.threshold = np, store, threshold
        self.cache: dict[str, tuple[list[int], object, list[int]]] = {}
        self.gen = None

    def invalidate(self) -> None:
        self.cache.clear()

    def assign(self, workspace: str, emb) -> int:
        np = self.np
        gen = self.store.faces_gen()
        if gen != self.gen:
            self.cache.clear()
            self.gen = gen
        if workspace not in self.cache:
            rows = self.store.face_person_centroids(workspace)
            mat = (np.stack([np.frombuffer(r[1], np.float32) for r in rows]) if rows
                   else np.zeros((0, len(emb)), np.float32))
            self.cache[workspace] = ([r[0] for r in rows], mat, [r[2] for r in rows])
        ids, mat, ns = self.cache[workspace]
        if ids:
            sims = mat @ emb
            i = int(np.argmax(sims))
            if float(sims[i]) >= self.threshold:
                c = mat[i] * ns[i] + emb
                mat[i] = c / (np.linalg.norm(c) or 1.0)
                ns[i] += 1
                self.store.face_person_set_centroid(ids[i], mat[i].astype(np.float32).tobytes(), ns[i])
                return ids[i]
        pid = self.store.face_person_create(workspace, emb.astype(np.float32).tobytes(), 1)
        self.cache[workspace] = (ids + [pid], np.vstack([mat, emb[None, :]]), ns + [1])
        return pid


def pending_scenes(config, store) -> list[dict]:
    """Scenes with a proxy MP4 not yet scanned with the current model, oldest tape first."""
    done = store.face_scanned_keys(MODEL_TAG)
    out = []
    for doc in store.list_index():
        tid = doc.get("tape_id")
        if not tid:
            continue
        ws = doc.get("workspace") or "default"
        for s in (doc.get("_index") or {}).get("scenes", []):
            sid = s.get("scene_id")
            if not sid or (tid, sid) in done:
                continue
            mp4 = config.tapes / tid / f"{sid}.mp4"
            if mp4.exists():
                out.append({"tape_id": tid, "scene_id": sid, "workspace": ws, "mp4": mp4})
    return out


def _lower_thread_priority() -> None:
    """Idle priority for this thread only (Linux nice is per-thread), so the
    api/converter threads in the same process stay responsive."""
    if sys.platform.startswith("linux"):
        try:
            os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), 19)
        except OSError:
            pass


STAGES = {1: "szybki skan", 2: "dokładna analiza", 3: "grupowanie"}


def run_scan(config, store, build: dict, log=lambda m: None) -> dict:
    """Body of the face-scan build, three stages: 1 fast scan of new scenes,
    2 refinement of tracks not refined yet (face_refine), 3 re-clustering of every
    workspace. Raises ScanCancelled when asked to stop; each stage keeps what it
    finished, so the next run continues."""
    from . import face_refine
    _lower_thread_priority()
    started, last_save, last_check = time.monotonic(), 0.0, 0.0
    stats = {"scenes": 0, "faces": 0, "refined": 0, "persons": 0}

    def save(stage: int, i: int, total: int, detail: str, frac: float = 0.0, force: bool = False) -> None:
        nonlocal last_save, started
        if build.get("stage") != stage:
            started = time.monotonic()           # ETA per stage
        if not force and build.get("stage") == stage and time.monotonic() - last_save < 2:
            return
        last_save = time.monotonic()
        prog = (i + frac) / total if total else 1.0
        elapsed = time.monotonic() - started
        build.update(stage=stage, stage_name=STAGES[stage], progress=round(min(1.0, prog), 4), done=i,
                     total=total, faces=stats["faces"], detail=detail,
                     eta_s=round(elapsed / prog * (1 - prog)) if prog > 0.002 else None)
        store.put_build(build)

    def check() -> None:
        nonlocal last_check
        if time.monotonic() - last_check < 2:
            return
        last_check = time.monotonic()
        if store.build_cancelled(FACES_KEY):
            raise ScanCancelled()

    def wait_if_busy(stage: int, i: int, total: int) -> None:
        # captures/conversions win: capture must never drop frames to us
        nonlocal last_check
        while store.capture_job() or store.processing_job():
            save(stage, i, total, "pauza — trwa zgrywanie/przetwarzanie", force=True)
            for _ in range(10):
                last_check = 0.0
                check()
                time.sleep(1)
        last_check = 0.0

    todo = pending_scenes(config, store)
    needs_models = todo or store.face_scenes_to_refine(face_models.REFINE_TAG)
    analyzer = None
    if needs_models:
        paths = ensure_models(config, log)
        analyzer = FaceAnalyzer(paths, threads=config.faces_threads, min_size=config.faces_min_size,
                                score=config.faces_score)

    # ---- 1: fast scan ----
    matcher = Matcher(store, config.faces_match) if todo else None
    total = len(todo)
    save(1, 0, total, "start", force=True)
    for i, sc in enumerate(todo):
        wait_if_busy(1, i, total)
        check()
        detail = f"{sc['tape_id']} · {sc['scene_id']}"
        save(1, i, total, detail, force=True)
        try:
            tracks = scan_scene(sc["mp4"], analyzer, config.faces_blur,
                                on_frac=lambda f, i=i, d=detail: save(1, i, total, d, f), check=check)
        except ScanCancelled:
            raise
        except Exception as exc:  # noqa: BLE001 - one bad file must not stop a days-long scan
            log(f"{detail}: {exc}")
            store.face_save_scene(sc["tape_id"], sc["scene_id"], MODEL_TAG, [])
            continue
        for _attempt in range(3):
            rows = [{"person_id": matcher.assign(sc["workspace"], t["emb"]), "workspace": sc["workspace"],
                     "embedding": t["emb"].astype(analyzer.np.float32).tobytes(), "quality": t["quality"],
                     "times": t["times"]} for t in tracks]
            ids = store.face_save_scene(sc["tape_id"], sc["scene_id"], MODEL_TAG, rows)
            if ids is not None:
                break
            matcher.invalidate()   # a person was merged away under us; re-match
        else:
            continue
        for tid, t in zip(ids, tracks):
            if t["jpg"]:
                thumb_path(config, tid).write_bytes(t["jpg"])
        stats["faces"] += len(tracks)
    stats["scenes"] = total

    # ---- 2: refinement (ArcFace on the best views of every new track) ----
    if analyzer is not None:
        stats["refined"] = face_refine.run_refine(
            config, store, analyzer,
            save=lambda i, n, d: save(2, i, n, d, force=True), check=check,
            wait_if_busy=lambda i, n: wait_if_busy(2, i, n))

    # ---- 3: re-cluster every workspace from scratch (seconds, not minutes) ----
    face_refine.import_rules(config, store)
    workspaces = store.face_workspaces()
    for i, ws in enumerate(workspaces):
        check()
        save(3, i, len(workspaces), ws, force=True)
        res = face_refine.recluster(config, store, ws)
        stats["persons"] += res["persons"] or 0
    save(3, len(workspaces), len(workspaces), "gotowe", force=True)
    return stats


# ---- schedule -------------------------------------------------------------
def due_today(config, store, at: datetime | None = None) -> bool:
    """True once a day, the first time the local clock is past faces_scan_at —
    but only within AUTO_WINDOW_H hours of it, so restarting the server at 14:00
    doesn't kick off the "nightly" scan mid-afternoon. (A scan that started in
    the window runs on until it's done.)"""
    hhmm = (config.faces_scan_at or "").strip()
    if not hhmm:
        return False
    try:
        h, m = (int(x) for x in hhmm.split(":"))
        tz = ZoneInfo(config.default_recording_tz)
    except Exception:  # noqa: BLE001 - bad time / zone in config = no schedule
        return False
    local = (at or datetime.now(tz)).astimezone(tz)
    if not 0 <= (local.hour * 60 + local.minute) - (h * 60 + m) < AUTO_WINDOW_H * 60:
        return False
    return store.get_meta("faces_auto_date") != local.date().isoformat()


def mark_auto_run(config, store, at: datetime | None = None) -> None:
    tz = ZoneInfo(config.default_recording_tz)
    store.set_meta("faces_auto_date", (at or datetime.now(tz)).astimezone(tz).date().isoformat())


# ---- read side for the api -------------------------------------------------
def persons(store, workspace: str) -> list[dict]:
    return sorted(store.face_persons(workspace),
                  key=lambda p: (p["name"] is None, (p["name"] or "").lower(), -p["scenes"], -p["tracks"]))


def segments(times: list[int]) -> list[list[int]]:
    """[3,4,5,9] -> [[3,5],[9,9]] — consecutive seconds as one on-screen stretch."""
    out: list[list[int]] = []
    for t in sorted(set(times)):
        if out and t <= out[-1][1] + 2:
            out[-1][1] = t
        else:
            out.append([t, t])
    return out


def person_detail(store, person_id: int) -> dict | None:
    p = store.face_person(person_id)
    if not p:
        return None
    tracks = store.face_tracks(person_id)
    docs = {}
    scenes: dict[tuple[str, str], dict] = {}
    for t in tracks:
        key = (t["tape_id"], t["scene_id"])
        if t["tape_id"] not in docs:
            docs[t["tape_id"]] = store.get_index(t["tape_id"]) or {}
        doc = docs[t["tape_id"]]
        if key not in scenes:
            meta = next((s for s in (doc.get("_index") or {}).get("scenes", []) if s.get("scene_id") == t["scene_id"]), {})
            scenes[key] = {"tape_id": t["tape_id"], "scene_id": t["scene_id"], "label": doc.get("label"),
                           "scene_index": meta.get("scene_index"), "date": meta.get("date"),
                           "time": meta.get("time"), "times": [], "tracks": []}
        scenes[key]["times"] += t["times"]
        scenes[key]["tracks"].append(t["id"])
    out = []
    for s in scenes.values():
        s["segments"] = segments(s.pop("times"))
        out.append(s)
    out.sort(key=lambda s: ((s["date"] or "9999"), s["time"] or "", s["tape_id"], s["scene_id"]))
    # each face carries its scene's recording date/time, for the person's timeline
    return {**p, "tracks": [{"id": t["id"], "tape_id": t["tape_id"], "scene_id": t["scene_id"],
                             "quality": t["quality"], "first": min(t["times"]) if t["times"] else 0,
                             "date": scenes[(t["tape_id"], t["scene_id"])]["date"],
                             "time": scenes[(t["tape_id"], t["scene_id"])]["time"]}
                            for t in sorted(tracks, key=lambda t: -t["quality"])],
            "scenes": out}


def status(config, store, workspace: str) -> dict:
    tape_ids = {d.get("tape_id") for d in store.list_index()
                if (d.get("workspace") or "default") == workspace}
    total = sum(len((d.get("_index") or {}).get("scenes", [])) for d in store.list_index()
                if d.get("tape_id") in tape_ids)
    return {"available": available(), "enabled": config.faces_enabled, "scan_at": config.faces_scan_at,
            "scanned": store.face_scanned_count(tape_ids), "scenes": total,
            "scan": store.get_build(FACES_KEY)}

