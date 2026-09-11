"""Precomputed tape / scene index kept in the job store (``library`` table).

Scanning ``tapes/*/tape.json`` + every scene ``*.json`` on each request gets slow
with thousands of recordings. Instead a background pass folds each tape into one
document (the tape.json fields plus a compact per-scene summary with an effective
date), keyed by a cheap staleness signature so unchanged tapes are skipped. The
api reads ``/api/tapes`` and the year/month timeline straight from that.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

_SANE_DATE = re.compile(r"^(19[89]\d|20[0-2]\d)-\d\d-\d\d$")
_SANE_TIME = re.compile(r"^\d\d-\d\d-\d\d$")
_HASH16 = re.compile(r"^[0-9a-f]{16}$")

# Bump when idx_scenes' fields change shape, to force every tape doc to be
# rebuilt on the next periodic sweep even though its source files didn't change.
_INDEX_SCHEMA = 2


def _scene_date(tape: dict, scene: dict) -> str | None:
    """Effective YYYY-MM-DD for a scene: tape override → valid VAUX date → the
    date baked into the scene_id → nothing."""
    override = tape.get("recording_date")
    if override and _SANE_DATE.match(override):
        return override
    rec = scene.get("recording") or {}
    dt = (rec.get("datetime") or "")[:10]
    if rec.get("datetime_valid") and _SANE_DATE.match(dt):
        return dt
    sid_date = (scene.get("scene_id") or "")[5:15]
    if _SANE_DATE.match(sid_date):
        return sid_date
    return None


def _scene_time(scene: dict) -> str | None:
    """Effective HH:MM:SS a scene was recorded (camera's own clock, from VAUX) —
    lets scenes from different tapes on the same day interleave in time order
    instead of just grouping by tape. Same VAUX source as the scene_id stamp,
    so the fallback below is not a weaker guess, just a differently-formatted read."""
    rec = scene.get("recording") or {}
    dt = rec.get("datetime") or ""
    if rec.get("datetime_valid") and len(dt) >= 19:
        return dt[11:19]
    sid_time = (scene.get("scene_id") or "")[16:24]
    if _SANE_TIME.match(sid_time):
        return sid_time.replace("-", ":")
    return None


def source_sig(tape_dir: Path) -> str:
    """Cheap change token — name/mtime/size of tape.json and the scene jsons."""
    parts = [f"schema:{_INDEX_SCHEMA}"]
    for p in sorted([tape_dir / "tape.json", *tape_dir.glob("[0-9]*.json")]):
        try:
            st = p.stat()
        except OSError:
            continue
        parts.append(f"{p.name}:{int(st.st_mtime)}:{st.st_size}")
    return ";".join(parts)


def build_tape_doc(tape_dir: Path) -> dict:
    """tape.json verbatim (so /api/tapes keeps its shape) + an ``_index`` block."""
    tape = json.loads((tape_dir / "tape.json").read_text())
    scenes = []
    for sj in sorted(tape_dir.glob("[0-9]*.json")):
        try:
            scenes.append(json.loads(sj.read_text()))
        except (OSError, ValueError):
            continue
    idx_scenes = []
    dates = []
    total_dv = 0
    for s in scenes:
        d = _scene_date(tape, s)
        if d:
            dates.append(d)
        arch = (s.get("files") or {}).get("archive") or {}
        total_dv += arch.get("size_compressed") or 0
        cap = s.get("capture") or {}
        idx_scenes.append({
            "scene_id": s.get("scene_id"), "scene_index": s.get("scene_index"),
            "date": d, "time": _scene_time(s), "tc": s.get("timecode") or {}, "frame_count": s.get("frame_count"),
            "standard": (s.get("video") or {}).get("standard"),
            "dv": arch.get("size_compressed"),
            "dropped_frames": cap.get("dropped_frames") or 0,
            "discontinuities": len(cap.get("source_discontinuities") or []),
            "decode_errors": cap.get("decode_errors"),
            "error_score": cap.get("error_score"),
            "fingerprint": s.get("fingerprint"),
        })
    dates.sort()
    doc = dict(tape)
    doc["_index"] = {"scenes": idx_scenes, "date_min": dates[0] if dates else None,
                     "date_max": dates[-1] if dates else None, "total_dv": total_dv}
    return doc


def reindex(config, store, *, force: bool = False) -> dict:
    """Fold every tape dir into the library table; drop entries for deleted tapes."""
    seen, rebuilt = set(), 0
    for tj in sorted(config.tapes.glob("*/tape.json")):
        d = tj.parent
        if d.parent != config.tapes:
            continue
        seen.add(d.name)
        sig = source_sig(d)
        if not force and store.index_sig(d.name) == sig:
            continue
        try:
            store.put_index(d.name, sig, build_tape_doc(d))
            rebuilt += 1
        except (OSError, ValueError):
            continue
    for gone in store.index_tape_ids() - seen:
        store.delete_index(gone)
    return {"tapes": len(seen), "rebuilt": rebuilt}


def reindex_one(config, store, tape_id: str) -> None:
    d = config.tapes / tape_id
    try:
        if (d / "tape.json").exists() and d.parent == config.tapes:
            store.put_index(tape_id, source_sig(d), build_tape_doc(d))
        else:
            store.delete_index(tape_id)
    except (OSError, ValueError):
        pass


def _ensure(config, store) -> None:
    """Lazy fill for an api process that came up before anything indexed."""
    have = store.index_tape_ids()
    on_disk = {p.parent.name for p in config.tapes.glob("*/tape.json")}
    if on_disk - have or have - on_disk:
        reindex(config, store)


def tapes_list(config, store) -> list[dict]:
    _ensure(config, store)
    return [{k: v for k, v in doc.items() if not k.startswith("_")} for doc in store.list_index()]


def timeline(config, store) -> dict:
    """Year → month aggregates for the iPhone-style browser (no scene lists here)."""
    _ensure(config, store)
    years: dict[int, dict] = {}
    undated = 0
    for doc in store.list_index():
        tid = doc.get("tape_id")
        for s in (doc.get("_index") or {}).get("scenes") or []:
            dt = s.get("date")
            if not dt:
                undated += 1
                continue
            y, m = int(dt[:4]), int(dt[5:7])
            yr = years.setdefault(y, {"year": y, "count": 0, "months": {}, "thumb": None})
            yr["count"] += 1
            mo = yr["months"].setdefault(m, {"month": m, "count": 0, "thumb": None})
            mo["count"] += 1
            if not mo["thumb"] and s.get("scene_id"):
                mo["thumb"] = {"tape_id": tid, "scene_id": s["scene_id"]}
            if not yr["thumb"]:
                yr["thumb"] = mo["thumb"]
    out = []
    for y in sorted(years, reverse=True):
        yr = years[y]
        yr["months"] = [yr["months"][m] for m in sorted(yr["months"], reverse=True)]
        out.append(yr)
    return {"years": out, "undated": undated}


def _hamming_close(a: str, b: str, max_bits: int = 5) -> bool:
    """frame_hashes are 64-bit average-hashes (see media.scene_probe_quality) — two
    separate captures of the same footage never land on byte-identical DV frames
    (different decode rounding, a scene boundary off by a frame or two), so an exact
    hash never matched in practice. Tolerate up to max_bits differing bits instead."""
    return bin(int(a, 16) ^ int(b, 16)).count("1") <= max_bits


def _hashes_match(a: dict, b: dict) -> bool:
    """True duplicate footage matches tightly at *every* sampled frame (start/mid/end),
    not just some of them. Requiring only e.g. 2 of 3 to be "close" sounds reasonable
    in isolation, but at a few thousand scenes the union-find grouping below turns any
    nonzero per-pair false-positive rate into a giant transitively-chained cluster of
    scenes that merely look vaguely similar (shared scenery, similar lighting) --
    that happened here (a ~290-scene mega-cluster) before this tightened to "all".
    Only well-formed 16-hex-char hashes count — a malformed one (or an old-format
    12-char SHA1 hash, from before frame_hashes switched to an average-hash) is
    excluded rather than treated as "doesn't match", so a stray bad value can't
    poison an otherwise-agreeing comparison."""
    pairs = [(x, y) for x, y in zip(a.get("frame_hashes") or [], b.get("frame_hashes") or [])
             if x and y and _HASH16.match(x) and _HASH16.match(y)]
    if len(pairs) < 2:
        return False
    return all(_hamming_close(x, y) for x, y in pairs)


def _same_recording(a: dict, b: dict) -> bool:
    """Two scene fingerprints that are very likely the same footage from separate captures."""
    if _hashes_match(a, b):
        return True
    ad = (a.get("datetime") or "")
    if ad and ad == b.get("datetime") and _SANE_DATE.match(ad[:10]) \
            and a.get("tc_start") and a.get("tc_start") == b.get("tc_start"):
        return True
    if a.get("tc_start") and a.get("tc_start") == b.get("tc_start") \
            and a.get("tc_end") == b.get("tc_end") \
            and abs((a.get("frame_count") or 0) - (b.get("frame_count") or 0)) <= 5:
        return True
    return False


def duplicates(config, store) -> dict:
    """Group scenes across tapes that look like the same recording, ordered best-first
    by error score (dropped + discontinuities + decode errors). Nothing is deleted."""
    _ensure(config, store)
    scenes, unprobed = [], 0
    for doc in store.list_index():
        tid = doc.get("tape_id")
        for s in (doc.get("_index") or {}).get("scenes") or []:
            fp = s.get("fingerprint") or {}
            if not (fp.get("frame_hashes") or fp.get("tc_start") or fp.get("datetime")):
                unprobed += 1
                continue
            scenes.append({"tape_id": tid, "scene_id": s.get("scene_id"), "scene_index": s.get("scene_index"),
                           "date": s.get("date"), "frame_count": s.get("frame_count"),
                           "error_score": s.get("error_score"), "decode_errors": s.get("decode_errors"),
                           "dropped_frames": s.get("dropped_frames"), "discontinuities": s.get("discontinuities"),
                           "fp": fp})
    parent = list(range(len(scenes)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    # Full pairwise comparison, not just within exact-hash buckets: frame_hashes now
    # match by Hamming distance (see _hamming_close), so two near-duplicate scenes
    # essentially never share an exact bucket key to begin with. A few thousand
    # scenes is cheap to compare O(n^2) (well under a second); this stops being
    # true in the tens-of-thousands range, which a home MiniDV archive won't reach.
    for i in range(len(scenes)):
        for j in range(i + 1, len(scenes)):
            if _same_recording(scenes[i]["fp"], scenes[j]["fp"]):
                parent[find(i)] = find(j)

    grouped: dict[int, list[dict]] = {}
    for i in range(len(scenes)):
        grouped.setdefault(find(i), []).append(scenes[i])
    BIG, out = 10 ** 9, []
    for members in grouped.values():
        # Same-tape duplicates count too (an over-recorded/duplicated scene on one
        # capture), not just the same footage recaptured under a different tape_id.
        if len(members) < 2:
            continue
        # Best = fewest errors; tied on errors -> the longer recording (more of the
        # moment actually captured); still tied -> doesn't matter, pick deterministically.
        members.sort(key=lambda m: (m["error_score"] if m["error_score"] is not None else BIG,
                                    -(m["frame_count"] or 0), m["tape_id"], m["scene_index"] or 0))
        for k, m in enumerate(members):
            m.pop("fp", None)
            m["best"] = k == 0
        out.append({"count": len(members), "members": members})
    out.sort(key=lambda g: (-g["count"], g["members"][0]["tape_id"]))
    return {"groups": out, "unprobed": unprobed, "scenes_indexed": len(scenes)}


def month_scenes(config, store, year: int, month: int) -> list[dict]:
    _ensure(config, store)
    res = []
    for doc in store.list_index():
        tid, label = doc.get("tape_id"), doc.get("label")
        for s in (doc.get("_index") or {}).get("scenes") or []:
            dt = s.get("date")
            if dt and int(dt[:4]) == year and int(dt[5:7]) == month:
                res.append({"tape_id": tid, "label": label, "scene_id": s.get("scene_id"),
                            "scene_index": s.get("scene_index"), "date": dt, "time": s.get("time"),
                            "tc": s.get("tc") or {},
                            "frame_count": s.get("frame_count"), "standard": s.get("standard")})
    # Scenes with a known recording time (VAUX) interleave chronologically across
    # tapes; scenes without one (no VAUX on that stretch of tape) sort after the
    # timed ones for that day, grouped by tape/scene order same as before.
    res.sort(key=lambda x: (x["date"], x["time"] or "99:99:99", x["tape_id"], x["scene_index"] or 0))
    return res
