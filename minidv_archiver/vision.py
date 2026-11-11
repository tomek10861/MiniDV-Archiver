"""What's in the pictures ("Szukaj"): an image-text model looks at every
recording and photo, so the archive can be searched by what's in it — "a pond",
"an old CRT monitor", "Italy" — together with who's in it, when and where from.

Runs as its own service (``python -m minidv_archiver.vision``, compose service
``vision``), CPU-capped and at idle priority, so it chews through the archive in
the background without getting in the way of a capture or of anything else:

  * model: SigLIP2 ViT-B/16 (224 px, multilingual text side, ONNX as packaged by
    Immich), downloaded once into <storage>/state/models/vision, sha256-checked;
  * frames: one every ``vision_step`` s of a video, analysed only when the picture
    changed (or every ``vision_max_gap`` s); black / blown-out / flat frames are
    skipped. A photo is one frame (its thumbnail — no NAS access needed);
  * index: <storage>/state/vision.db (one 768-d float16 vector per analysed frame,
    the tag ranges per scene) — kept apart from jobs.db, written only here;
  * tags (vision_vocab.py): a frame gets a tag when it's similar enough to one of
    the tag's descriptions; consecutive frames make a range ("pond 0:42–1:10").
    Changing the vocabulary re-tags from the stored vectors, nothing is decoded again;
  * search: free text (any language; English works best) and/or tags, kind of
    source, dates, people who must / mustn't be there. Results are moments.

A file that changes (a DVD re-encoded, a photo replaced) is looked at again; a
scene that disappears drops out of the index.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import string
import subprocess
import threading
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from .media_library import MEDIA_TAPE
from .vision_vocab import CATEGORIES, GENERIC, GENERIC_OK, RIVALS, TAGS

MODEL_TAG = "siglip2-b16-224/webli@5f3fc29"
_HF = "https://huggingface.co/immich-app/ViT-B-16-SigLIP2__webli/resolve/5f3fc29104ebcf51e9d893af57604badd51a3ea6/"
MODEL_FILES = {
    "visual.onnx": ("visual/model.onnx", "fee10c729875dd203d94f396ac3d664e301d39c977ef6d92e7f467a2a716f0b0"),
    "textual.onnx": ("textual/model.onnx", "fc9991b415d7d0fac7ed540e875fd08a647c8a1abba99f12542531b004c4b68f"),
    "tokenizer.json": ("textual/tokenizer.json", "220c63d496e0c14e63eb656c91e0215e926202e4c74b1f089e09f1920d779b04"),
}
SIZE, CTX, DIM = 224, 64, 768
VISION_KEY = "_vision/INDEX"
KINDS = ("dv", "dvd", "video", "photo")
SEARCH_FLOOR = 0.075            # below this a frame never answers a text query
SEARCH_BAND = 0.035             # ...nor when it's this much below the best frame found


def models_dir(config) -> Path:
    return Path(config.vision_models) if config.vision_models else config.state / "models" / "vision"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch_models(dest: Path, log=lambda m: None, download=None) -> dict[str, Path]:
    """The three model files in ``dest``, downloaded once (~1.5 GB) and checked."""
    dest.mkdir(parents=True, exist_ok=True)
    out = {}
    for name, (rel, digest) in MODEL_FILES.items():
        p = dest / name
        ok = p.with_suffix(p.suffix + ".ok")
        if not (p.exists() and ok.exists() and ok.read_text() == digest):
            log(f"pobieram model {name}")
            tmp = p.with_suffix(p.suffix + ".part")
            if download:
                download(_HF + rel, tmp)
            else:
                with urllib.request.urlopen(_HF + rel, timeout=600) as r, tmp.open("wb") as f:   # noqa: S310
                    shutil.copyfileobj(r, f, 1 << 20)
            if _sha256(tmp) != digest:
                tmp.unlink(missing_ok=True)
                raise RuntimeError(f"model {name}: niezgodna suma sha256")
            tmp.replace(p)
            ok.write_text(digest)
        out[name] = p
    return out


def _canon(text: str) -> str:
    """SigLIP2's text cleaning ("canonicalize"): lower case, no punctuation."""
    return re.sub(r"\s+", " ", text.lower().translate(str.maketrans("", "", string.punctuation))).strip()


class Encoder:
    """Frames and text into the same 768-d space (unit vectors)."""

    def __init__(self, files: dict[str, Path], threads: int = 1):
        import numpy as np
        import onnxruntime as ort
        from tokenizers import Tokenizer
        self.np = np
        so = ort.SessionOptions()
        so.intra_op_num_threads = max(1, threads)
        so.inter_op_num_threads = 1
        self._so, self._ort, self._files = so, ort, files
        self._vis = ort.InferenceSession(str(files["visual.onnx"]), so, providers=["CPUExecutionProvider"])
        self._txt = None                         # 1.1 GB: loaded on the first text
        self._tok = Tokenizer.from_file(str(files["tokenizer.json"]))
        self._lock = threading.Lock()

    def image(self, rgb):
        """rgb: SIZE x SIZE x 3 uint8 (squashed, as the model was trained)."""
        np = self.np
        x = (rgb.astype(np.float32) / 127.5 - 1.0).transpose(2, 0, 1)[None]
        e = self._vis.run(None, {"image": x})[0][0]
        return e / (np.linalg.norm(e) + 1e-9)

    def text(self, q: str):
        np = self.np
        with self._lock:
            if self._txt is None:
                self._txt = self._ort.InferenceSession(str(self._files["textual.onnx"]), self._so,
                                                       providers=["CPUExecutionProvider"])
        ids = (self._tok.encode(_canon(q)).ids + [0] * CTX)[:CTX]
        e = self._txt.run(None, {"text": np.array([ids], np.int32)})[0][0]
        return e / (np.linalg.norm(e) + 1e-9)


# ---- the index -----------------------------------------------------------------
_SCHEMA = """
CREATE TABLE IF NOT EXISTS frames (tape_id TEXT NOT NULL, scene_id TEXT NOT NULL, t REAL NOT NULL,
                                   t_end REAL NOT NULL, emb BLOB NOT NULL);
CREATE INDEX IF NOT EXISTS frames_scene ON frames(tape_id, scene_id);
CREATE TABLE IF NOT EXISTS scanned (tape_id TEXT NOT NULL, scene_id TEXT NOT NULL, sig TEXT, model TEXT,
                                     frames INTEGER, at TEXT, PRIMARY KEY (tape_id, scene_id));
CREATE TABLE IF NOT EXISTS tags (tape_id TEXT NOT NULL, scene_id TEXT NOT NULL, tag TEXT NOT NULL,
                                 t0 REAL, t1 REAL, score REAL);
CREATE INDEX IF NOT EXISTS tags_scene ON tags(tape_id, scene_id);
CREATE INDEX IF NOT EXISTS tags_tag ON tags(tag);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
"""


class VisionDB:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False, timeout=30)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._lock = threading.RLock()
        self._cache = None                       # (version, keys, ts, matrix)

    def meta(self, k: str) -> str | None:
        with self._lock:
            r = self._conn.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return r[0] if r else None

    def set_meta(self, k: str, v: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("INSERT INTO meta (k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))

    def _bump(self) -> None:
        self._conn.execute("INSERT INTO meta (k, v) VALUES ('version', '1') "
                           "ON CONFLICT(k) DO UPDATE SET v=CAST(v AS INTEGER) + 1")

    def scanned(self) -> dict[tuple[str, str], str]:
        with self._lock:
            return {(a, b): s for a, b, s in self._conn.execute("SELECT tape_id, scene_id, sig FROM scanned")}

    def counts(self) -> dict:
        with self._lock:
            scenes, frames = self._conn.execute("SELECT COUNT(*), COALESCE(SUM(frames), 0) FROM scanned").fetchone()
        return {"scenes": scenes, "frames": frames}

    def save_scene(self, key: tuple[str, str], sig: str, frames: list[tuple[float, float, bytes]],
                   tags: list[tuple[str, float, float, float]]) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM frames WHERE tape_id=? AND scene_id=?", key)
            self._conn.execute("DELETE FROM tags WHERE tape_id=? AND scene_id=?", key)
            self._conn.executemany("INSERT INTO frames (tape_id, scene_id, t, t_end, emb) VALUES (?,?,?,?,?)",
                                   [(*key, t, t1, e) for t, t1, e in frames])
            self._conn.executemany("INSERT INTO tags (tape_id, scene_id, tag, t0, t1, score) VALUES (?,?,?,?,?,?)",
                                   [(*key, *x) for x in tags])
            self._conn.execute("INSERT OR REPLACE INTO scanned (tape_id, scene_id, sig, model, frames, at) "
                               "VALUES (?,?,?,?,?,?)", (*key, sig, MODEL_TAG, len(frames), now))
            self._bump()

    def drop(self, keys: list[tuple[str, str]]) -> None:
        with self._lock, self._conn:
            for k in keys:
                for t in ("frames", "tags", "scanned"):
                    self._conn.execute(f"DELETE FROM {t} WHERE tape_id=? AND scene_id=?", k)
            self._bump()

    def replace_all_tags(self, tags: dict[tuple[str, str], list[tuple[str, float, float, float]]]) -> None:
        """Every scene's tags at once (stage 2), in one transaction."""
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM tags")
            self._conn.executemany("INSERT INTO tags (tape_id, scene_id, tag, t0, t1, score) VALUES (?,?,?,?,?,?)",
                                   [(*k, *x) for k, rows in tags.items() for x in rows])

    def tags_by_scene(self) -> dict[tuple[str, str], list[tuple[str, float, float, float]]]:
        out: dict = {}
        with self._lock:
            for a, b, tag, t0, t1, sc in self._conn.execute("SELECT tape_id, scene_id, tag, t0, t1, score FROM tags"):
                out.setdefault((a, b), []).append((tag, t0, t1, sc))
        return out

    def matrix(self):
        """(keys per row, (start, end) times, float32 matrix) of every analysed
        frame, cached until the index changes."""
        import numpy as np
        version = self.meta("version") or "0"
        if self._cache and self._cache[0] == version:
            return self._cache[1:]
        with self._lock:
            rows = self._conn.execute("SELECT tape_id, scene_id, t, t_end, emb FROM frames "
                                      "ORDER BY tape_id, scene_id, t").fetchall()
        keys = [(r[0], r[1]) for r in rows]
        ts = np.array([(r[2], r[3]) for r in rows], np.float32).reshape(-1, 2)
        m = (np.frombuffer(b"".join(r[4] for r in rows), np.float16).reshape(len(rows), DIM).astype(np.float32)
             if rows else np.zeros((0, DIM), np.float32))
        self._cache = (version, keys, ts, m)
        return keys, ts, m


# ---- what there is to look at ------------------------------------------------------
_CATALOG: dict[int, tuple[float, dict]] = {}
CATALOG_TTL = 60.0


def catalog(config, store, max_age: float = CATALOG_TTL) -> dict[tuple[str, str], dict]:
    """Every scene / photo / video the archive shows, with what search filters on —
    rebuilt at most once a minute (it walks ~50k rows and checks files on the NAS)."""
    hit = _CATALOG.get(id(store))
    if hit and time.monotonic() - hit[0] < max_age:
        return hit[1]
    out = _build_catalog(config, store)
    _CATALOG[id(store)] = (time.monotonic(), out)
    return out


def _build_catalog(config, store) -> dict[tuple[str, str], dict]:
    from . import media_library as ml
    out: dict[tuple[str, str], dict] = {}
    for doc in store.list_index():
        tape = doc.get("tape_id")
        if not tape:
            continue
        ws = doc.get("workspace") or "default"
        for s in (doc.get("_index") or {}).get("scenes", []):
            rate = 30000 / 1001 if s.get("standard") == "NTSC" else 25
            out[(tape, s["scene_id"])] = {
                "kind": "dv", "workspace": ws, "date": s.get("date"), "time": s.get("time"),
                "label": doc.get("label") or tape, "scene_index": s.get("scene_index"),
                "path": str(config.tapes / tape / f"{s['scene_id']}.mp4"),
                "duration": (s.get("frame_count") or 0) / rate}
    parents: dict[int, dict | None] = {}
    for row in store.media_ready():
        if row["kind"] == "photo":
            thumb = ml.derived(config, "thumbs", row["id"], ".jpg")
            path, kind = (str(thumb) if thumb.exists() else row["path"]), "photo"
        else:
            p = ml.video_path(config, row)
            if not p:
                continue
            pid = row.get("parent_id")
            if pid and pid not in parents:
                parents[pid] = store.media_get(pid)
            dvd = ml.is_dvd(parents.get(pid) or {} if pid else row)
            path, kind = str(p), ("dvd" if dvd else "video")
        date, tm = ml.effective_date(row)
        out[(ml.MEDIA_TAPE, str(row["id"]))] = {
            "kind": kind, "workspace": row.get("workspace") or "default", "date": date, "time": tm,
            "label": row.get("album") or "", "name": row.get("name"), "media_id": row["id"],
            "media_kind": row["kind"], "path": path, "duration": row.get("duration") or 0}
    return out


def _sig(path: str) -> str | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return f"{st.st_size}:{int(st.st_mtime)}:{MODEL_TAG}"


def _usable(rgb, np) -> tuple[bool, object]:
    """(worth analysing, a 16x16 colour thumbnail to compare the next frame with —
    colour, not grey: a red and a blue shot can be equally bright)."""
    small = rgb[::14, ::14].astype(np.float32)
    g = small.mean(axis=2)
    m, sd = float(g.mean()), float(g.std())
    return (sd >= 6 and 18 <= m <= 240), small


def sample_frames(path: str, *, photo: bool, step: float, max_gap: float, nice: bool = True):
    """Yield (t, t_end, rgb SIZE x SIZE) of the frames worth analysing: the first
    usable one, then each that differs from the last analysed or comes ``max_gap``
    s later; t_end = until when that picture lasted (the next change, a blank
    frame, or the end), so a tag or a hit covers the whole shot."""
    import numpy as np
    if photo:
        from PIL import Image
        with Image.open(path) as im:
            rgb = np.asarray(im.convert("RGB").resize((SIZE, SIZE), Image.BICUBIC), np.uint8)
        if _usable(rgb, np)[0]:
            yield 0.0, 0.0, rgb
        return
    pre = (["nice", "-n", "19"] if nice and shutil.which("nice") else [])
    if pre and shutil.which("ionice"):
        pre += ["ionice", "-c", "3"]
    cmd = pre + ["ffmpeg", "-nostdin", "-v", "error", "-threads", "1", "-i", path, "-an", "-sn", "-vf",
                 f"fps=1/{step},scale={SIZE}:{SIZE}:flags=bicubic", "-pix_fmt", "rgb24", "-f", "rawvideo", "-"]
    size = SIZE * SIZE * 3
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=size * 4)
    held = None                                  # [t, t_end, rgb, grey] of the picture being followed
    i = 0
    try:
        while True:
            buf = proc.stdout.read(size)
            if len(buf) < size:
                break
            t, i = i * step, i + 1
            rgb = np.frombuffer(buf, np.uint8).reshape(SIZE, SIZE, 3)
            ok, g = _usable(rgb, np)
            if held and ok and float(np.abs(g - held[3]).mean()) <= 10 and t - held[0] < max_gap:
                held[1] = t + step                # same picture goes on
                continue
            if held:
                yield held[0], held[1], held[2]
                held = None
            if ok:
                held = [t, t + step, rgb, g]
        if held:
            yield held[0], held[1], held[2]
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()


def tag_ranges(spans, sims, ids: list[str], threshold: float, gap: float):
    """Per tag, runs of frames at or above ``threshold`` as (tag, t0, t1, best);
    ``spans`` = (start, end) of each frame's picture, runs closer than ``gap`` s join."""
    out = []
    for j, tag in enumerate(ids):
        run = None
        for (t0, t1), s in zip(spans, sims[:, j]):
            if s < threshold:
                continue
            if run and t0 - run[1] <= gap:
                run[1], run[2] = max(run[1], float(t1)), max(run[2], float(s))
            else:
                if run:
                    out.append((tag, *run))
                run = [float(t0), float(t1), float(s)]
        if run:
            out.append((tag, *run))
    return out


def distill_keep(T, rival, scene_of, event_of, *, thr: float, strong: float, margin: float, support: int):
    """Stage 2: which (frame, tag) hits stay. A hit must beat its rivals by
    ``margin``; then it stays when it's strong, or when another frame of its scene
    has it too, or when ``support`` other frames of its event (same tape and day /
    same album and day) do. A lone weak frame no one around agrees with goes.
    T, rival: frames x tags; scene_of / event_of: a group id per frame."""
    import numpy as np
    ok = (T >= thr) & (T - rival >= margin)
    keep = ok & (T >= strong)
    for groups, need in ((scene_of, 1), (event_of, support)):
        _, inv = np.unique(np.asarray(groups), return_inverse=True)
        counts = np.zeros((inv.max() + 1 if len(inv) else 0, T.shape[1]), np.int32)
        np.add.at(counts, inv, ok.astype(np.int32))
        keep |= ok & (counts[inv] - ok >= need)
    return keep


# ---- the service -----------------------------------------------------------------
class VisionService:
    def __init__(self, config, store, encoder: Encoder | None = None):
        self.config, self.store = config, store
        self.db = VisionDB(config.state / "vision.db")
        self._enc = encoder
        self._enc_lock = threading.Lock()
        self._tagmat = None                      # (tag ids per column, matrix k x DIM, owners)

    # model
    def encoder(self) -> Encoder:
        with self._enc_lock:
            if self._enc is None:
                files = fetch_models(models_dir(self.config), log=lambda m: self._report("download", 0, 1, m))
                self._enc = Encoder(files, self.config.vision_threads)
            return self._enc

    def _texts(self, texts: list[str]):
        """Vectors of these descriptions, cached on disk by the exact list."""
        import numpy as np
        h = hashlib.sha1((json.dumps(texts) + MODEL_TAG).encode()).hexdigest()[:12]
        cache = models_dir(self.config) / f"text-{h}.npy"
        if cache.exists():
            return h, np.load(cache)
        enc = self.encoder()
        m = np.array([enc.text(p) for p in texts], np.float32).reshape(-1, DIM)
        cache.parent.mkdir(parents=True, exist_ok=True)
        np.save(cache, m)
        return h, m

    def _tags_matrix(self):
        """One row per tag description; the tag of each row."""
        owners = [t["id"] for t in TAGS for _ in t["prompts"]]
        h, m = self._texts([p for t in TAGS for p in t["prompts"]])
        self._tagmat = (h, owners, m)
        return owners, m

    def _scene_tags(self, spans, embs):
        """Tag ranges of one scene from its frame vectors."""
        import numpy as np
        owners, m = self._tags_matrix()
        sims = embs @ m.T                         # frames x descriptions
        ids = list(dict.fromkeys(owners))
        per_tag = np.stack([sims[:, [i for i, o in enumerate(owners) if o == tag]].max(axis=1) for tag in ids], 1)
        return tag_ranges(spans, per_tag, ids, self.config.vision_tag_t, self.config.vision_step)

    # indexing
    def _report(self, stage: str, i: int, total: int, detail: str = "", status: str = "RUNNING") -> None:
        b = self.store.get_build(VISION_KEY) or {}
        b.update(key=VISION_KEY, tape_id="_vision", token="INDEX", mode="vision", title="Analiza obrazu (Szukaj)",
                 status=status, stage=stage, progress=round(i / total, 4) if total else 0, done=i, total=total,
                 detail=detail[:120], path="", sources=[], **self.db.counts())
        self.store.put_build(b)

    def paused(self) -> bool:
        return self.store.get_meta("vision_paused") == "1"

    def distill(self, force: bool = False) -> int:
        """Stage 2 ("destylat"): re-decide every tag of the archive from the stored
        frame vectors, with what's around each frame — its rivals (what the tag gets
        mistaken for), the rest of its scene and the rest of its event (same tape
        and day / same album and day). Nothing is decoded again; a minute or two.
        Runs whenever the index, the vocabulary or the thresholds changed."""
        import numpy as np
        c = self.config
        ids = [t["id"] for t in TAGS]
        texts = list(dict.fromkeys([p for t in TAGS for p in t["prompts"]] + GENERIC
                                   + [p for ps in RIVALS.values() for p in ps]))
        h, V = self._texts(texts)
        mark = f"{h}:{c.vision_tag_t}:{c.vision_tag_strong}:{c.vision_tag_margin}:{c.vision_event_support}:" \
               f"{self.db.meta('version') or 0}"
        if not force and self.db.meta("distilled") == mark:
            return 0
        n_scenes = self.db.counts()["scenes"]
        self._report("distill", 0, n_scenes, "etap 2: wczytuję wektory klatek")
        keys, ts, m = self.db.matrix()
        if not len(keys):
            self.db.set_meta("distilled", mark)
            return 0
        self._report("distill", 0, n_scenes, "etap 2: porównuję klatki z tagami i rywalami")
        col = {p: i for i, p in enumerate(texts)}
        S = m @ V.T                               # frames x descriptions
        T = np.stack([S[:, [col[p] for p in t["prompts"]]].max(1) for t in TAGS], 1)
        rival = np.stack([S[:, [col[p] for p in GENERIC if p not in GENERIC_OK.get(t["id"], set())]
                                + [col[p] for p in RIVALS.get(t["id"], [])]].max(1) for t in TAGS], 1)
        cat = catalog(c, self.store, max_age=0)

        def event(k):
            it = cat.get(k) or {}
            where = ("@" + (it.get("label") or "")) if k[0] == MEDIA_TAPE else k[0]
            return f"{where}|{(it.get('date') or '')[:10]}"
        scene_ids, event_ids = {}, {}
        scene_of = np.array([scene_ids.setdefault(k, len(scene_ids)) for k in keys])
        event_of = np.array([event_ids.setdefault(event(k), len(event_ids)) for k in keys])
        keep = distill_keep(T, rival, scene_of, event_of, thr=c.vision_tag_t, strong=c.vision_tag_strong,
                            margin=c.vision_tag_margin, support=c.vision_event_support)
        kept = np.where(keep, T, -1.0)
        tags, start = {}, 0
        for i in range(1, len(keys) + 1):         # rows are ordered by scene: one block per scene
            if i == len(keys) or keys[i] != keys[start]:
                tags[keys[start]] = tag_ranges([tuple(x) for x in ts[start:i]], kept[start:i], ids,
                                               c.vision_tag_t, c.vision_step)
                start = i
                if len(tags) % 2000 == 0:
                    self._report("distill", len(tags), n_scenes, "etap 2: ustalam tagi scen")
        self._report("distill", len(tags), n_scenes, "etap 2: zapisuję tagi")
        self.db.replace_all_tags(tags)
        self.db.set_meta("distilled", mark)
        return len(tags)

    def index_once(self, busy=lambda: False, budget_s: float = 3600) -> int:
        """Analyse what isn't in the index yet (or changed); forget what's gone."""
        import numpy as np
        cat = catalog(self.config, self.store, max_age=0)
        known = self.db.scanned()
        gone = [k for k in known if k not in cat]
        if gone:
            self.db.drop(gone)
        order = {"dv": 0, "dvd": 1, "video": 2, "photo": 3}
        todo = []
        for key, it in sorted(cat.items(), key=lambda kv: (order[kv[1]["kind"]], kv[1]["date"] or "9999", kv[0])):
            sig = _sig(it["path"])
            if sig and known.get(key) != sig:
                todo.append((key, it, sig))
        until, done = time.monotonic() + budget_s, 0
        enc = self.encoder() if todo else None
        for i, (key, it, sig) in enumerate(todo):
            if time.monotonic() > until or self.paused():
                break
            while busy():
                self._report("waiting", i, len(todo), "czeka na zgrywanie / przetwarzanie")
                time.sleep(30)
            self._report("scan", i, len(todo), f"{it['label']} · {it.get('name') or key[1]}")
            spans, embs = [], []
            try:
                for t0, t1, rgb in sample_frames(it["path"], photo=it["kind"] == "photo",
                                                 step=self.config.vision_step, max_gap=self.config.vision_max_gap):
                    spans.append((t0, t1))
                    embs.append(enc.image(rgb))
            except Exception:  # noqa: BLE001 - one unreadable file must not stop the rest
                spans, embs = [], []
            m = np.array(embs, np.float32).reshape(-1, DIM)
            self.db.save_scene(key, sig, [(a, b, e.astype(np.float16).tobytes()) for (a, b), e in zip(spans, m)],
                               self._scene_tags(spans, m) if spans else [])
            done += 1
        left = len(todo) - done
        if not left:
            self.distill()                        # stage 2 once a round has seen everything
        self._report("idle" if not left else ("paused" if self.paused() else "scan"), len(todo) - left,
                     len(todo), "", status="PAUSED" if self.paused() and left else ("RUNNING" if left else "READY"))
        return done

    def run_forever(self, busy=lambda: False, interval: float = 600) -> None:
        while True:
            try:
                if self.paused():
                    self._report("paused", 0, 0, "", status="PAUSED")
                    time.sleep(30)
                    continue
                if self.index_once(busy=busy):          # did something: look again at once
                    continue
            except Exception as exc:  # noqa: BLE001 - the indexer must never die
                self._report("error", 0, 0, str(exc), status="ERROR")
            time.sleep(interval)

    # searching
    def tags(self, workspace: str = "default") -> list[dict]:
        cat = catalog(self.config, self.store)
        counts: dict[str, int] = {}
        for key, rows in self.db.tags_by_scene().items():
            it = cat.get(key)
            if it and it["workspace"] == workspace:
                for tag in {r[0] for r in rows}:
                    counts[tag] = counts.get(tag, 0) + 1
        return [{"id": t["id"], "pl": t["pl"], "en": t["en"], "cat": t["cat"], "count": counts.get(t["id"], 0)}
                for t in TAGS]

    def status(self) -> dict:
        b = self.store.get_build(VISION_KEY) or {}
        return {"model": MODEL_TAG, **self.db.counts(), "paused": self.paused(),
                "progress": {k: b.get(k) for k in ("status", "stage", "done", "total", "detail", "updated_at")},
                "categories": {k: {"pl": v[0], "en": v[1]} for k, v in CATEGORIES.items()}}

    def search(self, p: dict) -> dict:
        """p: {workspace, q?, tags?, not_tags?, kinds?, date_from?, date_to?,
        with?: [person ids, all of them], without?: [person ids], sort?: 'score' | 'date', limit?}"""
        import numpy as np
        ws = p.get("workspace") or "default"
        kinds = set(p.get("kinds") or KINDS)
        d0, d1 = p.get("date_from") or "", p.get("date_to") or ""
        want, avoid = set(p.get("tags") or []), set(p.get("not_tags") or [])
        with_p, without_p = {int(x) for x in p.get("with") or []}, {int(x) for x in p.get("without") or []}
        q = (p.get("q") or "").strip()
        cat = catalog(self.config, self.store)
        people = self.store.face_scene_people()
        tags = self.db.tags_by_scene()

        def allowed(key, it) -> bool:
            if it["workspace"] != ws or it["kind"] not in kinds:
                return False
            d = it["date"] or ""
            if (d0 or d1) and (not d or d < d0 or (d1 and d[: len(d1)] > d1)):
                return False
            here = people.get(key, {})
            if not with_p <= set(here) or without_p & set(here):
                return False
            have = {r[0] for r in tags.get(key, [])}
            return want <= have and not (avoid & have)

        hits: dict[tuple[str, str], dict] = {}
        if q:
            keys, ts, m = self.db.matrix()
            if len(keys):
                sims = m @ self.encoder().text(q).astype(np.float32)
                ok = np.array([allowed(k, cat[k]) if k in cat else False for k in keys]) if keys else np.zeros(0, bool)
                best = float(sims[ok].max()) if ok.any() else 0.0
                thr = max(SEARCH_FLOOR, best - SEARCH_BAND)
                for i in np.nonzero(ok & (sims >= thr))[0]:
                    k = keys[i]
                    h = hits.setdefault(k, {"score": 0.0, "times": []})
                    h["score"] = max(h["score"], float(sims[i]))
                    h["times"].append((float(ts[i, 0]), float(ts[i, 1]), float(sims[i])))
        else:
            for k, it in cat.items():
                if (want or with_p or kinds != set(KINDS) or d0 or d1) and allowed(k, it):
                    hits[k] = {"score": 0.0, "times": []}
        step = self.config.vision_step
        out = []
        for k, h in hits.items():
            it = cat[k]
            if h["times"]:
                moments = _merge(sorted(h["times"]), step)
            elif want:
                moments = _merge(sorted((t0, t1, sc) for tag, t0, t1, sc in tags.get(k, []) if tag in want), step)
            elif with_p:
                secs = sorted({t for pid in with_p for t in people.get(k, {}).get(pid, [])})
                moments = _merge([(t, t + 1, 0.0) for t in secs], 3)
            else:
                moments = [(0.0, min(it["duration"] or 0, 10.0), 0.0)]
            here = people.get(k, {})
            out.append({"tape_id": k[0], "scene_id": k[1], "kind": it["kind"], "date": it["date"], "time": it["time"],
                        "label": it["label"], "name": it.get("name"), "scene_index": it.get("scene_index"),
                        "media_id": it.get("media_id"), "media_kind": it.get("media_kind"),
                        "duration": round(it["duration"] or 0, 2), "score": round(h["score"], 4),
                        "moments": [[round(a, 1), round(b, 1)] for a, b, _ in moments][:12],
                        "people": sorted(here, key=lambda pid: -len(here[pid]))[:8],
                        "tags": sorted({r[0] for r in tags.get(k, [])})})
        if q and p.get("sort") != "date":
            out.sort(key=lambda r: -r["score"])
        else:
            out.sort(key=lambda r: (r["date"] or "9999", r["time"] or "", r["tape_id"], r["scene_id"]))
        limit = int(p.get("limit") or 300)
        return {"total": len(out), "results": out[:limit], "indexed": self.db.counts()}


def _merge(spans: list[tuple[float, float, float]], gap: float) -> list[tuple[float, float, float]]:
    out: list[list[float]] = []
    for a, b, s in spans:
        if out and a - out[-1][1] <= gap:
            out[-1][1] = max(out[-1][1], b)
            out[-1][2] = max(out[-1][2], s)
        else:
            out.append([a, b, s])
    return [tuple(x) for x in out]


# ---- frames for the result cards -----------------------------------------------------
def frame_jpg(config, store, tape_id: str, scene_id: str, t: float) -> Path | None:
    """A small JPEG of the frame at ``t`` s (cached under <storage>/vision/frames)."""
    from . import media_library as ml
    if tape_id == ml.MEDIA_TAPE:
        row = store.media_get(int(scene_id)) if scene_id.isdigit() else None
        if not row:
            return None
        if row["kind"] == "photo":
            thumb = ml.derived(config, "thumbs", row["id"], ".jpg")
            return thumb if thumb.exists() else None
        src = ml.video_path(config, row)
    else:
        if "/" in tape_id or "/" in scene_id or tape_id.startswith("."):
            return None
        src = config.tapes / tape_id / f"{scene_id}.mp4"
    if not src or not Path(src).exists():
        return None
    out = config.storage / "vision" / "frames" / f"{hashlib.sha1(f'{tape_id}/{scene_id}'.encode()).hexdigest()[:16]}_{t:.1f}.jpg"
    if not out.exists():
        out.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-ss", f"{max(0.0, t):.2f}", "-i", str(src),
                        "-frames:v", "1", "-vf", "scale='trunc(iw*sar/2)*2':ih,setsar=1,scale=360:-2", "-q:v", "4",
                        str(out)], timeout=60)
    return out if out.exists() else None


# ---- the service process -----------------------------------------------------------
def serve(service: VisionService, port: int) -> None:
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import parse_qs, urlparse

    class H(BaseHTTPRequestHandler):
        def _json(self, obj, code=200):
            data = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            u = urlparse(self.path)
            ws = (parse_qs(u.query).get("workspace") or ["default"])[0]
            try:
                if u.path == "/status":
                    return self._json(service.status())
                if u.path == "/tags":
                    return self._json(service.tags(ws))
                return self._json({"error": "not found"}, 404)
            except Exception as exc:  # noqa: BLE001
                return self._json({"error": str(exc)}, 500)

        def do_POST(self):
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                if urlparse(self.path).path == "/search":
                    return self._json(service.search(body))
                return self._json({"error": "not found"}, 404)
            except Exception as exc:  # noqa: BLE001
                return self._json({"error": str(exc)}, 500)

        def log_message(self, *_):
            pass

    ThreadingHTTPServer(("0.0.0.0", port), H).serve_forever()


def main() -> None:
    from .config import Config
    from .store import JobStore
    try:
        os.nice(19)
    except OSError:
        pass
    config = Config()
    config.ensure_dirs()
    store = JobStore(config.state / "jobs.db")
    service = VisionService(config, store)
    busy = lambda: bool(store.capture_job() or store.processing_job())  # noqa: E731
    threading.Thread(target=service.run_forever, kwargs={"busy": busy}, daemon=True).start()
    print(f"MiniDV vision on :{config.vision_port} ({MODEL_TAG}, {config.vision_threads} threads)")
    serve(service, config.vision_port)


if __name__ == "__main__":
    main()
