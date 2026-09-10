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


def source_sig(tape_dir: Path) -> str:
    """Cheap change token — name/mtime/size of tape.json and the scene jsons."""
    parts = []
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
        idx_scenes.append({
            "scene_id": s.get("scene_id"), "scene_index": s.get("scene_index"),
            "date": d, "tc": s.get("timecode") or {}, "frame_count": s.get("frame_count"),
            "standard": (s.get("video") or {}).get("standard"),
            "dv": arch.get("size_compressed"),
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


def month_scenes(config, store, year: int, month: int) -> list[dict]:
    _ensure(config, store)
    res = []
    for doc in store.list_index():
        tid, label = doc.get("tape_id"), doc.get("label")
        for s in (doc.get("_index") or {}).get("scenes") or []:
            dt = s.get("date")
            if dt and int(dt[:4]) == year and int(dt[5:7]) == month:
                res.append({"tape_id": tid, "label": label, "scene_id": s.get("scene_id"),
                            "scene_index": s.get("scene_index"), "date": dt, "tc": s.get("tc") or {},
                            "frame_count": s.get("frame_count"), "standard": s.get("standard")})
    res.sort(key=lambda x: (x["date"], x["tape_id"], x["scene_index"] or 0))
    return res
