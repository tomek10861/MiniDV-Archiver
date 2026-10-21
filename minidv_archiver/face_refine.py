"""Refinement pass ("dokładna analiza") + re-clustering for face recognition.

The fast scan (faces.py) looks at one frame per second with SFace and assigns
each track greedily, in scan order — cheap, but on DV footage SFace barely
separates people (same person ~0.2-0.35 cosine, different people up to ~0.38)
and greedy assignment compounds its mistakes, so people both merge and split.

This pass, per track (one person in one scene):
  * goes back to the seconds the face was visible and decodes every 4th frame of
    up to REFINE_SECONDS of them (blurred frames skipped, as in the fast scan),
  * re-detects faces there and scores every view: detector confidence x size x
    sharpness of the face itself x how frontal it is (from the 5 landmarks),
  * embeds the best few views with ArcFace (a much stronger model; too slow for
    every frame, fine for a handful per track) into one quality-weighted template,
    and makes the best view the track's thumbnail.

Then every track of a workspace is re-clustered from scratch:
  * only frontal-enough tracks build groups (average-linkage agglomerative
    clustering — a group joins another only if they're similar *on average*,
    which doesn't chain unrelated faces together like nearest-match does),
  * two faces in the same frame are never the same person,
  * profile / blurry tracks only join an existing group when clearly closer to it
    than to any other; otherwise they stay on their own instead of polluting one,
  * operator corrections are hard rules: a merge / "move here" is "same", a
    "not this person" is "diff" (store.face_feedback), and a named or hidden
    person keeps its name/flag via its anchor track.
Thresholds were tuned on real footage from this archive (see config.py).
"""
from __future__ import annotations

import heapq
import shutil
import subprocess
from pathlib import Path

from . import face_models
from .faces import THUMB_PX, ScanCancelled, _probe, recompute_person, thumb_path

REFINE_SECONDS = 8      # per track, spread over the seconds it was visible
FRAME_STEP = 4          # every 4th frame of those seconds (~6 per second at 25 fps)
TOP_VIEWS = 3           # best views per track that get an ArcFace embedding
MATCH_SFACE = 0.25      # re-detected face -> which track (SFace, same scene, same second)

# ArcFace's canonical 112x112 landmark positions (eyes, nose, mouth corners)
_ARC_TEMPLATE = [[38.2946, 51.6963], [73.5318, 51.5014], [56.0252, 71.7366],
                 [41.5493, 92.3655], [70.7299, 92.2041]]


class ArcFace:
    def __init__(self, path: Path, threads: int = 1):
        import cv2
        import numpy as np
        import onnxruntime as ort
        so = ort.SessionOptions()
        so.intra_op_num_threads = max(1, threads)
        so.inter_op_num_threads = 1
        self.sess = ort.InferenceSession(str(path), so, providers=["CPUExecutionProvider"])
        self.inp = self.sess.get_inputs()[0].name
        self.cv2, self.np = cv2, np
        self.tpl = np.array(_ARC_TEMPLATE, np.float32)

    def align(self, frame, landmarks):
        m, _ = self.cv2.estimateAffinePartial2D(landmarks.astype(self.np.float32), self.tpl,
                                                method=self.cv2.LMEDS)
        if m is None:
            return None
        return self.cv2.warpAffine(frame, m, (112, 112), borderValue=0)

    def embed(self, aligned):
        np = self.np
        x = (self.cv2.cvtColor(aligned, self.cv2.COLOR_BGR2RGB).astype(np.float32) - 127.5) / 127.5
        e = self.sess.run(None, {self.inp: x.transpose(2, 0, 1)[None]})[0][0]
        return (e / (np.linalg.norm(e) or 1.0)).astype(np.float32)


def frontal_score(lm, box_w: float) -> float:
    """0 = profile, 1 = facing the camera: wide eye spacing for the box and the
    nose centred between the eyes."""
    eye_l, eye_r, nose = lm[0], lm[1], lm[2]
    ed = float(((eye_r[0] - eye_l[0]) ** 2 + (eye_r[1] - eye_l[1]) ** 2) ** 0.5)
    if ed < 1:
        return 0.0
    yaw = abs(float(nose[0]) - (float(eye_l[0]) + float(eye_r[0])) / 2) / ed
    return max(0.0, min(1.0, ed / (0.42 * box_w))) * max(0.0, 1 - 2 * yaw)


def _view_quality(an, frame, row) -> tuple[float, float]:
    """(quality, frontal) of one detected face."""
    cv2 = an.cv2
    x, y, w, h = (int(v) for v in row[:4])
    lm = row[4:14].reshape(5, 2)
    fr = frontal_score(lm, float(row[2]))
    crop = frame[max(0, y):max(0, y) + max(1, h), max(0, x):max(0, x) + max(1, w)]
    if crop.size == 0:
        return 0.0, fr
    sharp = float(cv2.Laplacian(cv2.cvtColor(cv2.resize(crop, (96, 96)), cv2.COLOR_BGR2GRAY), cv2.CV_64F).var())
    size_f = max(0.0, min(1.0, (min(w, h) - 20) / 60))
    return float(row[-1]) * size_f * min(1.0, sharp / 80) * (0.2 + 0.8 * fr), fr


def _thumb(an, frame, row) -> bytes | None:
    cv2 = an.cv2
    h, w = frame.shape[:2]
    x, y, fw, fh = (float(v) for v in row[:4])
    side = max(fw, fh) * 1.5
    cx, cy = x + fw / 2, y + fh / 2
    x0, y0 = int(max(0, cx - side / 2)), int(max(0, cy - side / 2))
    x1, y1 = int(min(w, cx + side / 2)), int(min(h, cy + side / 2))
    if x1 <= x0 or y1 <= y0:
        return None
    ok, jpg = cv2.imencode(".jpg", cv2.resize(frame[y0:y1, x0:x1], (THUMB_PX, THUMB_PX)),
                           [cv2.IMWRITE_JPEG_QUALITY, 88])
    return jpg.tobytes() if ok else None


def _sample(times: list[int], k: int) -> list[int]:
    ts = sorted(set(times))
    if len(ts) <= k:
        return ts
    return sorted({ts[round(i * (len(ts) - 1) / (k - 1))] for i in range(k)})


def _frames(mp4: Path, seconds: set[int], fps: float, w: int, h: int, np):
    """Yield (second, frame) for every FRAME_STEP-th frame inside the given
    seconds. Frame numbers (not timestamps) drive ffmpeg's select, so which frame
    is which second is exact; decoding still runs over the file but nothing else does."""
    ranges = []
    for s in sorted(seconds):
        a, b = int(s * fps), int((s + 1) * fps) - 1
        if ranges and a <= ranges[-1][1] + 1:
            ranges[-1][1] = b
        else:
            ranges.append([a, b])
    if not ranges:
        return
    expr = "+".join(f"between(n,{a},{b})" for a, b in ranges)
    prefix = ["nice", "-n", "19"] if shutil.which("nice") else []
    if prefix and shutil.which("ionice"):
        prefix += ["ionice", "-c", "3"]
    cmd = [*prefix, "ffmpeg", "-nostdin", "-v", "error", "-threads", "1", "-i", str(mp4), "-an",
           "-vf", f"select='not(mod(n,{FRAME_STEP}))*({expr})',scale={w}:{h},setsar=1",
           "-vsync", "0", "-pix_fmt", "bgr24", "-f", "rawvideo", "-"]
    picked = [n for a, b in ranges for n in range(a, b + 1) if n % FRAME_STEP == 0]
    size = w * h * 3
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=size * 2)
    try:
        for n in picked:
            buf = proc.stdout.read(size)
            if len(buf) < size:
                break
            yield int(n / fps), np.frombuffer(buf, np.uint8).reshape(h, w, 3)
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()


def refine_scene(mp4: Path, tracks: list[dict], an, arc, check=lambda: None, blur: float = 0.0) -> dict[int, dict]:
    """{track_id: {arc, rq, frontal, jpg}} for the tracks of one scene."""
    np = an.np
    w, h, fps, _ = _probe(mp4)
    want: dict[int, set[int]] = {t["id"]: set(_sample(t["times"], REFINE_SECONDS)) for t in tracks}
    sface = {t["id"]: np.frombuffer(t["embedding"], np.float32) for t in tracks}
    visible = {t["id"]: set(t["times"]) for t in tracks}
    views: dict[int, list] = {t["id"]: [] for t in tracks}      # min-heap of (q, seq, frontal, aligned, jpg)
    seq = 0
    for sec, frame in _frames(mp4, set().union(*want.values()), fps, w, h, np):
        check()
        if blur and an.sharpness(frame) < blur:
            continue
        an.det.setInputSize((w, h))
        _, rows = an.det.detect(frame)
        if rows is None:
            continue
        cands = [tid for tid in want if any(abs(sec - s) <= 1 for s in visible[tid])]
        if not cands:
            continue
        for row in rows:
            if min(row[2], row[3]) < 24:
                continue
            e = an.rec.feature(an.rec.alignCrop(frame, row)).flatten().astype(np.float32)
            e /= (np.linalg.norm(e) or 1.0)
            sims = [(float(e @ sface[tid]), tid) for tid in cands]
            best, tid = max(sims)
            if best < MATCH_SFACE:
                continue
            q, fr = _view_quality(an, frame, row)
            heap = views[tid]
            if len(heap) >= TOP_VIEWS and q <= heap[0][0]:
                continue
            aligned = arc.align(frame, row[4:14].reshape(5, 2))
            if aligned is None:
                continue
            seq += 1
            item = (q, seq, fr, aligned, _thumb(an, frame, row))
            heapq.heappush(heap, item) if len(heap) < TOP_VIEWS else heapq.heapreplace(heap, item)
    out = {}
    for tid, heap in views.items():
        if not heap:
            continue
        best = max(heap)
        acc = np.zeros(512, np.float32)
        for q, _, _, aligned, _ in heap:
            acc += max(q, 1e-3) * arc.embed(aligned)
        out[tid] = {"arc": acc / (np.linalg.norm(acc) or 1.0), "rq": best[0] * 100, "frontal": best[2], "jpg": best[4]}
    return out


def refine_from_thumb(config, track_id: int, an, arc) -> dict | None:
    """Fallback when the face wasn't found again in the video: use the stored
    thumbnail (one view, so its quality says how much to trust it)."""
    cv2, np = an.cv2, an.np
    img = cv2.imread(str(thumb_path(config, track_id)))
    if img is None:
        return None
    img = cv2.resize(img, (320, 320))
    an.det.setInputSize((320, 320))
    _, rows = an.det.detect(img)
    if rows is None:
        return None
    row = max(rows, key=lambda r: r[2] * r[3])
    aligned = arc.align(img, row[4:14].reshape(5, 2))
    if aligned is None:
        return None
    q, fr = _view_quality(an, img, row)
    return {"arc": arc.embed(aligned), "rq": q * 50, "frontal": fr, "jpg": None}


def run_refine(config, store, an, save, check, wait_if_busy) -> int:
    """Stage 2: refine every track not yet refined with the current model."""
    paths = face_models.fetch(Path(config.faces_models) if config.faces_models else config.state / "models")
    arc = ArcFace(paths["arcface"], threads=config.faces_threads)
    scenes = store.face_scenes_to_refine(face_models.REFINE_TAG)
    for i, (tape_id, scene_id) in enumerate(scenes):
        wait_if_busy(i, len(scenes))
        check()
        save(i, len(scenes), f"{tape_id} · {scene_id}")
        tracks = store.face_scene_tracks(tape_id, scene_id)
        mp4 = config.tapes / tape_id / f"{scene_id}.mp4"
        try:
            res = refine_scene(mp4, tracks, an, arc, check, config.faces_blur) if mp4.exists() else {}
        except ScanCancelled:
            raise
        except Exception:  # noqa: BLE001 - one unreadable file must not stop the pass
            res = {}
        for t in tracks:
            r = res.get(t["id"]) or refine_from_thumb(config, t["id"], an, arc)
            if r and r.get("jpg"):
                thumb_path(config, t["id"]).write_bytes(r["jpg"])
            store.face_save_refined(t["id"], arc=r["arc"].tobytes() if r else None, rq=r["rq"] if r else 0.0,
                                    frontal=r["frontal"] if r else 0.0, tag=face_models.REFINE_TAG)
    return len(scenes)


# ---- clustering -------------------------------------------------------------
def avg_linkage(sim, threshold: float, sizes=None, cannot=None):
    """Average-linkage agglomerative clustering (nearest-neighbour chain, O(n^2)).
    sim: (n,n) similarity between units of ``sizes`` members each (already
    averaged per member pair); stops when no pair averages >= threshold.
    cannot: (n,n) bool, pairs that may never end up together. Returns labels."""
    import numpy as np
    n = len(sim)
    W = np.array(sim, np.float64)
    np.fill_diagonal(W, -np.inf)
    if cannot is not None:
        W[cannot] = -np.inf
    size = np.ones(n) if sizes is None else np.asarray(sizes, np.float64).copy()
    alive = np.ones(n, bool)
    label = np.arange(n)
    chain: list[int] = []
    while True:
        if not chain:
            cand = np.flatnonzero(alive)
            if len(cand) < 2:
                break
            chain = [int(cand[0])]
        a = chain[-1]
        row = np.where(alive, W[a], -np.inf)
        b = int(np.argmax(row))
        if row[b] < threshold:
            # nothing averages above the threshold with a, and merges elsewhere can't
            # raise an average above the max of its parts: a is final
            alive[a] = False
            chain = []
            continue
        if len(chain) > 1 and chain[-2] == b:
            chain = chain[:-2]
            na, nb = size[a], size[b]
            new = (na * W[a] + nb * W[b]) / (na + nb)
            new[np.isneginf(W[a]) | np.isneginf(W[b])] = -np.inf     # a cannot-link sticks
            W[a], W[:, a] = new, new
            W[a, a] = -np.inf
            size[a] = na + nb
            alive[b] = False
            W[b], W[:, b] = -np.inf, -np.inf
            label[label == b] = a
        else:
            chain.append(b)
    return label


class _UF:
    def __init__(self):
        self.p = {}

    def find(self, x):
        self.p.setdefault(x, x)
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a, b):
        self.p[self.find(a)] = self.find(b)


def plan_clusters(tracks: list[dict], feedback, touched: list[dict], *, core_frontal: float,
                  cluster_t: float, attach_t: float, attach_margin: float) -> list[dict]:
    """Pure function: tracks of one workspace (with ``arc`` bytes or None) ->
    [{"person_id": id | None, "tracks": [...]}] covering every track."""
    import numpy as np
    by_id = {t["id"]: t for t in tracks}
    emb = {t["id"]: np.frombuffer(t["arc"], np.float32) for t in tracks if t.get("arc")}
    core = [t["id"] for t in tracks if t["id"] in emb and t["frontal"] >= core_frontal]
    core_set = set(core)

    # hard "different": same frame (overlapping seconds in one scene), operator "diff",
    # and two differently-named persons' anchors
    diff = set()
    scenes: dict[tuple, list] = {}
    for t in tracks:
        scenes.setdefault((t["tape_id"], t["scene_id"]), []).append(t)
    for ts in scenes.values():
        for i in range(len(ts)):
            for j in range(i + 1, len(ts)):
                if set(ts[i]["times"]) & set(ts[j]["times"]):
                    diff.add(frozenset((ts[i]["id"], ts[j]["id"])))
    uf = _UF()
    for kind, a, b in feedback:
        if a in by_id and b in by_id:
            if kind == "same":
                uf.union(a, b)
            else:
                diff.add(frozenset((a, b)))
    named = [p for p in touched if p["name"] and p["anchor"] in by_id]
    for i in range(len(named)):
        for j in range(i + 1, len(named)):
            if named[i]["name"].lower() != named[j]["name"].lower():
                diff.add(frozenset((named[i]["anchor"], named[j]["anchor"])))

    # units = tracks glued by operator "same"; a unit with any core member is core
    units: dict[int, list[int]] = {}
    for t in tracks:
        units.setdefault(uf.find(t["id"]), []).append(t["id"])

    # an anchor too weak to cluster (profile/blur) would strand the name on a lone
    # face: stand in the best frontal track glued to it by the operator, else the
    # person's best frontal track (never one the scan only added to it later if
    # the operator confirmed others)
    touched = [dict(p) for p in touched]
    for p in touched:
        if p["anchor"] in by_id and p["anchor"] not in core_set:
            pool = [m for m in units[uf.find(p["anchor"])] if m in core_set]
            if not pool and len(units[uf.find(p["anchor"])]) == 1:
                pool = [t["id"] for t in tracks if t["person_id"] == p["id"] and t["id"] in core_set]
            if pool:
                p["anchor"] = max(pool, key=lambda m: (by_id[m]["frontal"] >= 0.6, by_id[m]["rq"]))
    core_units = [u for u, ms in units.items() if any(m in core_set for m in ms)]
    clusters: list[list[int]] = []
    if core_units:
        order = [m for u in core_units for m in units[u] if m in core_set]
        A = np.stack([emb[m] for m in order])
        S = A @ A.T
        bounds = np.cumsum([0] + [sum(1 for m in units[u] if m in core_set) for u in core_units])[:-1]
        sizes = np.array([sum(1 for m in units[u] if m in core_set) for u in core_units], np.float64)
        U = np.add.reduceat(np.add.reduceat(S, bounds, axis=0), bounds, axis=1) / np.outer(sizes, sizes)
        cannot = np.zeros((len(core_units), len(core_units)), bool)
        pos = {m: k for k, u in enumerate(core_units) for m in units[u]}
        for pair in diff:
            a, b = tuple(pair)
            if a in pos and b in pos and pos[a] != pos[b]:
                cannot[pos[a], pos[b]] = cannot[pos[b], pos[a]] = True
        labels = avg_linkage(U, cluster_t, sizes, cannot)
        groups: dict[int, list[int]] = {}
        for k, lab in enumerate(labels):
            groups.setdefault(int(lab), []).extend(units[core_units[k]])
        clusters = list(groups.values())

    # weak units (profile/blur/tiny): join the clearly-closest group, or stay alone
    cl_core = [[m for m in c if m in core_set] for c in clusters]
    cl_mat = [np.stack([emb[m] for m in c]) for c in cl_core]
    member_of = {m: k for k, c in enumerate(clusters) for m in c}
    keep_alone: list[list[int]] = []
    for u, ms in units.items():
        if u in core_units:
            continue
        vecs = [emb[m] for m in ms if m in emb]
        if not vecs or not clusters:
            keep_alone.append(ms)
            continue
        scores = sorted(((float(np.mean(cl_mat[k] @ np.mean(vecs, axis=0))), k) for k in range(len(clusters))),
                        reverse=True)
        best, k = scores[0]
        second = scores[1][0] if len(scores) > 1 else -1.0
        blocked = any(frozenset((m, o)) in diff for m in ms for o in clusters[k])
        if best >= attach_t and best - second >= attach_margin and not blocked:
            clusters[k].extend(ms)
            for m in ms:
                member_of[m] = k
        else:
            keep_alone.append(ms)

    # tracks without any ArcFace view keep their current person (nothing better to go on)
    groups_out = [{"person_id": None, "tracks": c} for c in clusters]
    keep_by_person: dict[int, list[int]] = {}
    for ms in keep_alone:
        if all(m not in emb for m in ms):
            keep_by_person.setdefault(by_id[ms[0]]["person_id"], []).extend(ms)
        else:
            groups_out.append({"person_id": None, "tracks": ms})
    for pid, ms in keep_by_person.items():
        groups_out.append({"person_id": pid, "tracks": ms, "kept": True})

    # names/hidden follow their anchor track; named persons win over hidden ones
    claimed = set()
    for p in sorted(touched, key=lambda p: (p["name"] is None,)):
        for g in groups_out:
            if p["anchor"] in g["tracks"] and g["person_id"] in (None, p["id"]) and p["id"] not in claimed:
                g["person_id"] = p["id"]
                claimed.add(p["id"])
                break
    # other groups keep the id of the old person they overlap most (stable links)
    touched_ids = {p["id"] for p in touched}
    cands = []
    for gi, g in enumerate(groups_out):
        if g["person_id"] is not None:
            continue
        counts: dict[int, int] = {}
        for m in g["tracks"]:
            pid = by_id[m]["person_id"]
            if pid not in touched_ids:
                counts[pid] = counts.get(pid, 0) + 1
        cands += [(c, gi, pid) for pid, c in counts.items()]
    for _, gi, pid in sorted(cands, reverse=True):
        if groups_out[gi]["person_id"] is None and pid not in claimed:
            groups_out[gi]["person_id"] = pid
            claimed.add(pid)
    return groups_out


def import_rules(config, store) -> int:
    """Pick up ``<state>/face_rules-*.json`` once each: operator corrections made
    before they were recorded as feedback (reconstructed from the api log) —
    {"feedback_pairs": [[kind, a, b], ...], "groups": {person_id: {"tracks": [...]}}}."""
    import json
    added = 0
    for path in sorted(config.state.glob("face_rules-*.json")):
        try:
            doc = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        anchors = {int(pid): g["tracks"][0] for pid, g in (doc.get("groups") or {}).items() if g.get("tracks")}
        added += store.face_import_rules(path.name, [tuple(p) for p in doc.get("feedback_pairs") or []], anchors)
    return added


def recluster(config, store, workspace: str) -> dict:
    """Stage 3 for one workspace. Retries if the operator edits persons meanwhile."""
    for _attempt in range(3):
        gen = store.faces_gen()
        tracks = store.face_workspace_tracks(workspace)
        if not tracks:
            return {"persons": 0, "tracks": 0}
        groups = plan_clusters(tracks, store.face_feedback(), store.face_touched_persons(workspace),
                               core_frontal=config.faces_core_frontal, cluster_t=config.faces_cluster_t,
                               attach_t=config.faces_attach_t, attach_margin=config.faces_attach_margin)
        if store.face_apply_clusters(workspace, gen, groups):
            for g in groups:
                recompute_person(store, g["person_id"])
            return {"persons": len(groups), "tracks": len(tracks)}
    return {"persons": None, "tracks": len(tracks)}
