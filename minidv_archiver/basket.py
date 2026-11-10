"""The editing basket ("Kosz montażowy"): moments picked in "Szukaj" and kept for
cutting a film elsewhere. Exported as one ZIP: every moment as its own short
clip (cut a second wider on each side, re-encoded so it starts exactly there,
at its source's own size and frame rate — 50p stays 50p), photos as the
original files, and ``lista.csv`` saying where each piece comes from, when it
was recorded, who's in it and what's in it."""
from __future__ import annotations

import csv
import io
import re
import shutil
import subprocess
import zipfile
from pathlib import Path

PAD_S = 1.0


def _slug(s: str, n: int = 40) -> str:
    s = re.sub(r"[^\w.-]+", " ", s or "", flags=re.UNICODE).strip()
    return re.sub(r"\s+", " ", s)[:n].strip() or "x"


def _mmss(t: float) -> str:
    t = int(t)
    return f"{t // 3600}-{t % 3600 // 60:02d}-{t % 60:02d}" if t >= 3600 else f"{t // 60:02d}-{t % 60:02d}"


def export_zip(config, store, workspace: str, items: list[dict], out: Path, *, pad: float = PAD_S,
               progress=lambda f: None, nice: bool = True) -> int:
    """Write the ZIP; returns how many pieces went in."""
    from .vision import VisionDB, catalog
    cat = catalog(config, store)
    names = {p["id"]: p["name"] for p in store.face_persons(workspace) if p.get("name")}
    people = store.face_scene_people()
    vdb_path = config.state / "vision.db"
    tags = VisionDB(vdb_path).tags_by_scene() if vdb_path.exists() else {}
    work = out.with_suffix(".work")
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    pre = (["nice", "-n", "10"] if nice and shutil.which("nice") else [])
    rows, done = [], 0
    try:
        with zipfile.ZipFile(out.with_suffix(".part"), "w", zipfile.ZIP_STORED) as zf:
            for i, it in enumerate(items, 1):
                key = (it["tape_id"], it["scene_id"])
                c = cat.get(key)
                if not c:
                    continue
                base = f"{i:03d} {c['date'] or 'bez daty'} {_slug(c.get('name') or c['label'])}"
                if c["kind"] == "photo":
                    row = store.media_get(int(it["scene_id"]))
                    src = Path(row["path"]) if row and Path(row["path"]).exists() else Path(c["path"])
                    arc, a, b = f"{base}{src.suffix.lower()}", 0.0, 0.0
                    zf.write(src, arc)
                else:
                    dur = c["duration"] or 0
                    a = max(0.0, it["start"] - pad)
                    b = min(dur, it["end"] + pad) if dur else it["end"] + pad
                    clip = work / f"{i:03d}.mp4"
                    cmd = pre + ["ffmpeg", "-nostdin", "-v", "error", "-y", "-ss", f"{a:.2f}", "-i", c["path"],
                                 "-t", f"{max(0.5, b - a):.2f}", "-map", "0:v:0", "-map", "0:a?",
                                 "-c:v", "libx264", "-preset", "medium", "-crf", "16", "-pix_fmt", "yuv420p",
                                 "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
                                 "-metadata", f"title={c['label']} {_mmss(a)}",
                                 *(["-metadata", f"date={c['date']}"] if c["date"] else []), str(clip)]
                    if subprocess.run(cmd, capture_output=True).returncode or not clip.exists():
                        continue
                    arc = f"{base} {_mmss(a)}.mp4"
                    zf.write(clip, arc)
                    clip.unlink()
                here = people.get(key, {})
                rows.append([arc, c["date"] or "", c.get("time") or "", {"dv": "MiniDV", "dvd": "VHS/DVD",
                             "video": "film", "photo": "zdjęcie"}[c["kind"]], c["label"], c.get("name") or key[1],
                             f"{a:.1f}", f"{b:.1f}",
                             ", ".join(names[p] for p in here if p in names and
                                       (c["kind"] == "photo" or any(a <= t <= b for t in here[p]))),
                             ", ".join(sorted({r[0] for r in tags.get(key, []) if r[1] <= b and r[2] >= a}))])
                done += 1
                progress(i / max(1, len(items)))
            buf = io.StringIO()
            w = csv.writer(buf, delimiter=";")
            w.writerow(["plik", "data", "godzina", "rodzaj", "źródło", "nazwa", "od [s]", "do [s]", "osoby", "tagi"])
            w.writerows(rows)
            zf.writestr("lista.csv", "﻿" + buf.getvalue())     # BOM: Excel opens Polish letters right
        out.with_suffix(".part").replace(out)
        return done
    finally:
        shutil.rmtree(work, ignore_errors=True)
        out.with_suffix(".part").unlink(missing_ok=True)
