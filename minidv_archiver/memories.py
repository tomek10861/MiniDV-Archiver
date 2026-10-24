"""Films about one person ("Zmontuj film"), close to the iPhone's Memories.

A memory is planned from what face recognition already knows — which scenes,
photos and videos a person is in and in which seconds, how good each face is,
and when everything was recorded — and rendered with ffmpeg:

  * picking: the stretches where the person is on screen (a little before and
    after), the best faces first, spread over the whole period (round-robin over
    years / months / days), at most two moments per scene, a different pick each
    time (seeded jitter + what earlier films of this person already used);
  * timing: every piece lasts a whole number of music beats, so the cross-fades
    fall on the beat; a piece starts on the quietest moment near its start, so
    nobody is cut off mid-word where it can be helped;
  * picture: 1280x720 50p, 4:3 / vertical sources on a blurred copy of themselves
    instead of black bars, photos with a slow zoom, the date at the bottom when
    it changes, a title over the opening, the music's credits at the end;
  * sound: the recordings' own sound plus music that ducks while somebody talks
    (a compressor keyed by the speech band of the recordings).

Music: a built-in catalogue of free tracks (Kevin MacLeod, incompetech.com,
CC BY 4.0 — credited at the end of every film), any CC0 / CC BY track found
through Openverse (api.openverse.org), and the operator's own files in
<storage>/music/own. Downloads are cached in <storage>/music/cache.
"""
from __future__ import annotations

import json
import math
import random
import re
import shutil
import subprocess
import urllib.parse
import urllib.request
from pathlib import Path

W, H, FPS = 1280, 720, 50
XFADE = 0.6                 # cross-fade between pieces (s)
PHOTO_S = 3.2               # nominal photo length before beat fitting
MIN_PIECE, MAX_PIECE = 2.6, 7.5
TITLE_S = 4.0
END_S = 4.0
LENGTHS = {"short": 50, "medium": 110, "long": 180}
MONTHS_PL = ["stycznia", "lutego", "marca", "kwietnia", "maja", "czerwca", "lipca", "sierpnia", "września",
             "października", "listopada", "grudnia"]
MONTHS_PL_NOM = ["styczeń", "luty", "marzec", "kwiecień", "maj", "czerwiec", "lipiec", "sierpień", "wrzesień",
                 "październik", "listopad", "grudzień"]
MONTHS_EN = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
             "November", "December"]

_INCOMPETECH = "https://incompetech.com/music/royalty-free/mp3-royaltyfree/"
# (title, mood) — every URL checked to exist when the catalogue was put together
_MACLEOD = [
    ("Heartwarming", "sentimental"), ("Touching Moments Two - Higher", "sentimental"), ("Dreamer", "sentimental"),
    ("Healing", "sentimental"), ("Long Road Ahead", "sentimental"), ("Inspired", "sentimental"),
    ("Peaceful Desolation", "sentimental"), ("At Rest", "sentimental"),
    ("Carefree", "happy"), ("Life of Riley", "happy"), ("Easy Lemon", "happy"), ("Happy Alley", "happy"),
    ("Wholesome", "happy"), ("Somewhere Sunny", "happy"), ("Daily Beetle", "happy"), ("Bright Wish", "happy"),
    ("Fluffing a Duck", "happy"), ("Monkeys Spinning Monkeys", "happy"),
    ("Wallpaper", "calm"), ("Airport Lounge", "calm"), ("Laid Back Guitars", "calm"), ("Clean Soul", "calm"),
    ("Tea Roots", "calm"), ("Floating Cities", "calm"), ("Meditation Impromptu 01", "calm"),
    ("Dewdrop Fantasy", "calm"),
    ("Hyperfun", "energetic"), ("Rising Game", "energetic"), ("Feelin Good", "energetic"),
    ("Funkorama", "energetic"),
    ("Gymnopedie No 1", "classical"), ("Gymnopedie No 3", "classical"), ("Village Consort", "classical"),
    ("Thatched Villagers", "classical"),
]
MOODS = ("sentimental", "happy", "calm", "energetic", "classical")
OPENVERSE = "https://api.openverse.org/v1/audio/"
_OV_ID = re.compile(r"^ov-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$")
_SLUG = re.compile(r"[^a-z0-9]+")
AUDIO_EXT = {".mp3", ".m4a", ".aac", ".ogg", ".oga", ".opus", ".flac", ".wav"}


def _slug(s: str) -> str:
    return _SLUG.sub("-", s.lower()).strip("-")


def builtin_catalogue() -> list[dict]:
    return [{"id": "kml-" + _slug(t), "title": t, "artist": "Kevin MacLeod", "mood": mood, "source": "incompetech",
             "url": _INCOMPETECH + urllib.parse.quote(t) + ".mp3", "page": "https://incompetech.com",
             "license": "CC BY 4.0",
             "credit": f"„{t}” Kevin MacLeod (incompetech.com), licencja CC BY 4.0"} for t, mood in _MACLEOD]


def _music_dirs(config) -> tuple[Path, Path]:
    root = config.storage / "music"
    return root / "cache", root / "own"


def _cached(cache: Path, tid: str) -> Path | None:
    hits = [p for p in cache.glob(f"{tid}.*") if p.suffix.lower() in AUDIO_EXT]
    return hits[0] if hits else None


def catalogue(config) -> list[dict]:
    """Everything the operator can pick from: built-in tracks, own files, and
    tracks fetched from Openverse before (kept with their credits)."""
    cache, own = _music_dirs(config)
    out = []
    for t in builtin_catalogue():
        p = _cached(cache, t["id"])
        out.append({**t, "cached": bool(p), "duration": _duration(p) if p else None})
    if cache.exists():
        for meta in sorted(cache.glob("ov-*.json")):
            try:
                t = json.loads(meta.read_text())
            except (OSError, ValueError):
                continue
            p = _cached(cache, t["id"])
            if p:
                out.append({**t, "cached": True, "duration": _duration(p)})
    if own.exists():
        for p in sorted(own.iterdir()):
            if p.suffix.lower() in AUDIO_EXT and p.is_file():
                out.append({"id": "own-" + _slug(p.stem)[:60], "title": p.stem, "artist": "", "mood": "own",
                            "source": "own", "license": "", "credit": "", "cached": True, "duration": _duration(p),
                            "_path": str(p)})
    return out


def _http_json(url: str, timeout: float = 50) -> dict:      # Openverse can take most of a minute
    req = urllib.request.Request(url, headers={"User-Agent": "minidv-archiver (family archive)"})
    with urllib.request.urlopen(req, timeout=timeout) as r:     # noqa: S310 - fixed https hosts only
        return json.loads(r.read().decode())


def _ov_track(r: dict) -> dict:
    return {"id": "ov-" + r["id"], "title": (r.get("title") or "")[:120], "artist": (r.get("creator") or "")[:80],
            "mood": "search", "source": r.get("source") or "openverse", "url": r.get("url"),
            "page": r.get("foreign_landing_url"), "license": f"CC {(r.get('license') or '').upper()} "
            f"{r.get('license_version') or ''}".strip(), "credit": (r.get("attribution") or "")[:400],
            "duration": (r.get("duration") or 0) / 1000 or None}


_SEARCHES: dict[str, tuple[float, list[dict]]] = {}


def search(query: str, fetch=None) -> list[dict]:
    """Music on Openverse that may be reused in a film: CC0 / public domain / CC BY
    (/ BY-SA), 1–8 minutes. Openverse sometimes takes most of a minute to answer,
    so answers are kept for a day."""
    import time
    key = query.strip().lower()[:100]
    hit = _SEARCHES.get(key)
    if fetch is None and hit and time.time() - hit[0] < 86400:
        return hit[1]
    q = urllib.parse.urlencode({"q": key, "license": "cc0,pdm,by,by-sa", "category": "music", "page_size": 20})
    out = []
    for r in (fetch or _http_json)(f"{OPENVERSE}?{q}").get("results", []):
        t = _ov_track(r)
        if t["url"] and (t["duration"] is None or 60 <= t["duration"] <= 480):
            out.append(t)
    if fetch is None:
        _SEARCHES[key] = (time.time(), out)
    return out


def ensure_track(config, tid: str, fetch=_http_json, download=None) -> tuple[dict, Path]:
    """(track, local file) — downloading a catalogue / Openverse track once. Only
    ids are accepted from the browser: the URL always comes from the catalogue or
    from Openverse itself, never from the request."""
    cache, own = _music_dirs(config)
    cache.mkdir(parents=True, exist_ok=True)
    if tid.startswith("own-"):
        t = next((x for x in catalogue(config) if x["id"] == tid), None)
        if not t:
            raise ValueError("nie ma takiego utworu")
        return t, Path(t.pop("_path"))
    t = next((x for x in builtin_catalogue() if x["id"] == tid), None)
    if t is None:
        m = _OV_ID.match(tid)
        if not m:
            raise ValueError("nie ma takiego utworu")
        meta = cache / f"{tid}.json"
        if meta.exists():
            t = json.loads(meta.read_text())
        else:
            t = _ov_track(fetch(f"{OPENVERSE}{m[1]}/"))
            meta.write_text(json.dumps(t, ensure_ascii=False))
    p = _cached(cache, tid)
    if not p:
        ext = ".mp3" if "mp3" in (t.get("url") or "").lower() or t["source"] in ("incompetech", "jamendo") else ".audio"
        tmp = cache / f"{tid}.part"
        (download or _download)(t["url"], tmp)
        probe = _duration(tmp)
        if not probe:
            tmp.unlink(missing_ok=True)
            raise RuntimeError("pobrany plik nie jest dźwiękiem")
        p = cache / f"{tid}{ext if ext != '.audio' else '.mp3'}"
        tmp.replace(p)
    return t, p


def _download(url: str, dest: Path, limit: int = 60 * 1024 * 1024) -> None:
    if not url.startswith("https://"):
        raise ValueError("tylko https")
    req = urllib.request.Request(url, headers={"User-Agent": "minidv-archiver (family archive)"})
    with urllib.request.urlopen(req, timeout=60) as r, dest.open("wb") as f:   # noqa: S310
        got = 0
        while chunk := r.read(1 << 16):
            got += len(chunk)
            if got > limit:
                raise RuntimeError("plik za duży")
            f.write(chunk)


def _duration(p: Path | None) -> float | None:
    if not p or not p.exists():
        return None
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(p)],
                         capture_output=True, text=True).stdout.strip()
    try:
        return round(float(out), 2)
    except ValueError:
        return None


# ---- beats ------------------------------------------------------------------
def beat_grid(path: Path) -> tuple[float, float]:
    """(seconds per beat, time of the first beat) from an onset envelope's
    autocorrelation (60–180 BPM, folded into 80–160). Cached next to the file."""
    cache = path.with_name(path.name + ".beats.json")
    if cache.exists():
        try:
            d = json.loads(cache.read_text())
            return d["period"], d["offset"]
        except (OSError, ValueError, KeyError):
            pass
    import numpy as np
    sr, hop = 11025, 256
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-t", "120", "-ac", "1", "-ar", str(sr),
                          "-f", "s16le", "-"], capture_output=True).stdout
    x = np.frombuffer(raw, np.int16).astype(np.float32) / 32768
    if len(x) < sr * 4:
        return 0.5, 0.0
    frames = len(x) // hop
    energy = np.sqrt(np.add.reduceat(x[: frames * hop] ** 2, np.arange(0, frames * hop, hop)) / hop + 1e-9)
    env = np.maximum(0, np.diff(np.log(energy)))
    env -= env.mean()
    fr = sr / hop                                           # envelope frames per second
    ac = np.correlate(env, env, "full")[len(env) - 1:]
    lags = np.arange(len(ac))
    lo, hi = int(fr * 60 / 180), int(fr * 60 / 60)
    weight = np.exp(-0.5 * (np.log2(np.maximum(lags[lo:hi], 1) / (fr * 60 / 115)) / 1.0) ** 2)
    lag = lo + int(np.argmax(ac[lo:hi] * weight))
    period = lag / fr
    while period < 60 / 160:
        period *= 2
    while period > 60 / 80:
        period /= 2
    # refine to a fraction of a frame together with the phase: the (period, offset)
    # whose beat grid collects the most onsets; env[i] is the rise into frame i + 1
    best = (-1.0, period, 0.0)
    for p in np.linspace(period * 0.97, period * 1.03, 121):
        grid = np.arange(int((len(env) / fr - p) / p)) * p
        for o in np.arange(0, p, 1 / fr):
            sc = float(env[np.minimum(len(env) - 1, np.round((o + grid) * fr).astype(int))].sum())
            if sc > best[0]:
                best = (sc, float(p), float(o))
    period, offset = best[1], (best[2] + 1 / fr) % best[1]
    cache.write_text(json.dumps({"period": round(float(period), 4), "offset": round(float(offset), 4)}))
    return round(float(period), 4), round(float(offset), 4)


# ---- picking ----------------------------------------------------------------
def _bucket_key(dates: list[str]):
    years = {d[:4] for d in dates}
    if len(years) > 1:
        return lambda d: d[:4]
    months = {d[:7] for d in dates}
    if len(months) > 1:
        return lambda d: d[:7]
    return lambda d: d[:10]


def candidates(config, store, person: dict) -> list[dict]:
    """Every moment of ``person`` (faces.person_detail) a film could use."""
    from . import media_library as ml
    quality: dict[tuple, float] = {}
    for t in person.get("tracks", []):
        k = (t["tape_id"], t["scene_id"])
        quality[k] = max(quality.get(k, 0.0), float(t.get("quality") or 0))
    docs, out = {}, []
    for sc in person.get("scenes", []):
        key = (sc["tape_id"], sc["scene_id"])
        q = quality.get(key, 0.0)
        date = sc.get("date") if sc.get("date") and sc.get("date") != "unknown" else None
        base = {"tape_id": sc["tape_id"], "scene_id": sc["scene_id"], "date": date, "time": sc.get("time")}
        if sc["tape_id"] == ml.MEDIA_TAPE:
            row = store.media_get(int(sc["scene_id"]))
            if not row or row["status"] != "ready":
                continue
            if row["kind"] == "photo":
                out.append({**base, "kind": "photo", "path": row["path"], "start": 0.0, "length": PHOTO_S,
                            "score": q * 0.85})
                continue
            path, dur = ml.video_path(config, row), float(row.get("duration") or 0)
            kind = "video"
        else:
            if sc["tape_id"] not in docs:
                docs[sc["tape_id"]] = store.get_index(sc["tape_id"]) or {}
            meta = next((s for s in (docs[sc["tape_id"]].get("_index") or {}).get("scenes", [])
                         if s.get("scene_id") == sc["scene_id"]), {})
            path = config.tapes / sc["tape_id"] / f"{sc['scene_id']}.mp4"
            rate = 30000 / 1001 if meta.get("standard") == "NTSC" else 25
            dur = (meta.get("frame_count") or 0) / rate
            kind = "dv"
        if not path or not Path(path).exists() or dur < MIN_PIECE:
            continue
        for a, b in sc.get("segments") or []:
            seen = b - a + 1
            length = min(MAX_PIECE, max(3.5, seen + 2.0), dur)
            centre = (a + b + 1) / 2
            start = min(max(0.0, centre - length / 2), max(0.0, dur - length))
            out.append({**base, "kind": kind, "path": str(path), "start": round(start, 2), "length": round(length, 2),
                        "duration": dur, "score": q * math.sqrt(min(1.0, seen / 4))})
    return out


def pick(cands: list[dict], target_s: float, *, seed: int, avoid: set = frozenset(),
         date_from: str | None = None, date_to: str | None = None, sources=("dv", "video", "photo")) -> list[dict]:
    """The moments for one film, in recording order (see the module docstring)."""
    rnd = random.Random(seed)
    pool = [dict(c) for c in cands if c["kind"] in sources
            and (not date_from or (c["date"] or "") >= date_from)
            and (not date_to or (c["date"] or "9999")[: len(date_to)] <= date_to)]
    if sum(1 for c in pool if c["date"]) >= 6:
        pool = [c for c in pool if c["date"]]                      # undated only when there's nothing else
    for c in pool:
        c["_s"] = c["score"] * rnd.uniform(0.7, 1.0) * (0.4 if _key(c) in avoid else 1.0)
    if not pool:
        return []
    bucket = _bucket_key([c["date"] for c in pool if c["date"]] or ["0000"])
    buckets: dict[str, list[dict]] = {}
    for c in sorted(pool, key=lambda c: -c["_s"]):
        buckets.setdefault(bucket(c["date"]) if c["date"] else "?", []).append(c)
    has_video = any(c["kind"] != "photo" for c in pool)
    picked, per_scene, total, photos = [], {}, 0.0, 0
    taken_in: dict[str, int] = {b: 0 for b in buckets}
    while total < target_s:
        live = [b for b, cs in buckets.items() if cs]
        if not live:
            break
        b = min(live, key=lambda b: (taken_in[b], -buckets[b][0]["_s"]))
        c = buckets[b].pop(0)
        sk = (c["tape_id"], c["scene_id"])
        if per_scene.get(sk, 0) >= 2 or any(
                (p["tape_id"], p["scene_id"]) == sk and abs(p["start"] - c["start"]) < 10 for p in picked):
            continue
        if c["kind"] == "photo" and has_video and photos >= max(2, 0.4 * (len(picked) + 1)):
            continue
        picked.append(c)
        per_scene[sk] = per_scene.get(sk, 0) + 1
        photos += c["kind"] == "photo"
        taken_in[b] += 1
        total += c["length"] - XFADE
    picked.sort(key=lambda c: (c["date"] or "9999", c.get("time") or "", c["tape_id"], c["scene_id"], c["start"]))
    return [{k: v for k, v in c.items() if k != "_s"} for c in picked]


def _key(c: dict) -> tuple:
    return (c["tape_id"], c["scene_id"], int(c["start"] // 5))


def used_keys(plans: list[list[dict]]) -> set:
    return {_key(c) for plan in plans for c in plan}


def fit_to_beats(items: list[dict], period: float, offset: float, music_s: float | None) -> list[dict]:
    """Piece lengths in whole beats (each cross-fade then starts on a beat), the
    first cut after the title. Lowest-scoring pieces drop out until the film
    (title .. end card) fits in the music."""
    beat = max(0.3, period)
    out = []
    for c in items:
        k = max(2, round((c["length"] - XFADE) / beat))
        while k * beat + XFADE < MIN_PIECE:
            k += 1
        d = k * beat + XFADE
        if c["kind"] != "photo":
            d = min(d, c["duration"] - c["start"]) if c.get("duration") else d
            if d < MIN_PIECE:
                continue
        out.append({**c, "d": round(d, 3)})
    if music_s:
        budget = music_s - END_S + XFADE - offset
        while out and total_length(out) > budget:
            out.remove(min(out, key=lambda c: c["score"]))
    return out


def total_length(items: list[dict]) -> float:
    """Length of the film: pieces + end card, minus the overlaps."""
    return sum(c["d"] for c in items) + END_S - XFADE * len(items)


# ---- captions / title / credits -------------------------------------------
def caption(date: str | None, lang: str = "pl") -> str | None:
    if not date or date == "unknown":
        return None
    y = date[:4]
    if len(date) >= 10:
        m, d = int(date[5:7]), int(date[8:10])
        return f"{d} {MONTHS_PL[m - 1]} {y}" if lang == "pl" else f"{MONTHS_EN[m - 1]} {d}, {y}"
    if len(date) >= 7:
        m = int(date[5:7])
        return f"{MONTHS_PL_NOM[m - 1]} {y}" if lang == "pl" else f"{MONTHS_EN[m - 1]} {y}"
    return y


_FONTS = ["/usr/share/fonts/truetype/dejavu/DejaVuSans{}.ttf", "/usr/share/fonts/dejavu/DejaVuSans{}.ttf",
          "/System/Library/Fonts/Supplemental/Arial{}.ttf", "/Library/Fonts/Arial{}.ttf"]


def _font(size: int, bold: bool = False):
    from PIL import ImageFont
    for f in _FONTS:
        p = f.format("-Bold" if bold and "DejaVu" in f else (" Bold" if bold else ""))
        if Path(p).exists():
            return ImageFont.truetype(p, size)
    return ImageFont.load_default(size)


def text_layer(path: Path, lines: list[tuple[str, int, bool]], *, w: int, h: int, where: str,
               footer: str | None = None) -> None:
    """A transparent w x h PNG with soft-shadowed text: ``where`` 'bottom-left'
    (dates), 'centre' (title, over a soft darkening) or 'end' (end card, with a
    small ``footer`` line at the bottom). lines = (text, size px at 720p, bold)."""
    from PIL import Image, ImageChops, ImageDraw, ImageFilter
    s = h / 720
    if footer:
        lines = lines + [("", 1, False)]
    fonts = [_font(max(10, int(size * s)), bold) for _, size, bold in lines]
    draw0 = ImageDraw.Draw(Image.new("L", (1, 1)))
    boxes = [draw0.textbbox((0, 0), t, font=f) for (t, _, _), f in zip(lines, fonts)]
    gap = int(10 * s)
    block_h = sum(b[3] - b[1] for b in boxes) + gap * (len(lines) - 1)
    if where == "bottom-left":
        x0, y0 = int(56 * s), h - int(56 * s) - block_h
    else:
        x0, y0 = None, (h - block_h) // 2 + (int(40 * s) if where == "centre" else 0)
    shadow = Image.new("L", (w, h), 0)
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ds, dl = ImageDraw.Draw(shadow), ImageDraw.Draw(layer)
    y = y0
    for (t, _, _), f, b in zip(lines, fonts, boxes):
        tw = b[2] - b[0]
        x = x0 if x0 is not None else (w - tw) // 2
        ds.text((x - b[0], y - b[1]), t, font=f, fill=200)
        dl.text((x - b[0], y - b[1]), t, font=f, fill=(255, 255, 255, 255))
        y += (b[3] - b[1]) + gap
    if footer:
        f = _font(max(9, int(20 * s)))
        b = ds.textbbox((0, 0), footer, font=f)
        x, y = (w - (b[2] - b[0])) // 2 - b[0], h - int(48 * s) - (b[3] - b[1]) - b[1]
        ds.text((x, y), footer, font=f, fill=200)
        dl.text((x, y), footer, font=f, fill=(255, 255, 255, 220))
    shadow = shadow.filter(ImageFilter.GaussianBlur(max(2, int(6 * s))))
    out = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    if where == "centre":                                   # a soft dark wash behind the title
        wash = Image.new("L", (w, h), 0)
        ImageDraw.Draw(wash).ellipse((w * 0.12, h * 0.22, w * 0.88, h * 0.78), fill=110)
        shadow = ImageChops.lighter(shadow, wash.filter(ImageFilter.GaussianBlur(int(90 * s))))
    out.putalpha(shadow.point(lambda v: int(v * 0.75)))
    out.alpha_composite(layer)
    out.save(path)



def photo_still(src: Path, dest: Path, w: int, h: int) -> None:
    """A 2w x 2h frame of a photo for the slow zoom: filling the screen when its
    shape is close to it, else on a blurred, darker copy of itself."""
    from PIL import Image, ImageEnhance, ImageFilter
    from .media_library import load_image
    W2, H2 = 2 * w, 2 * h
    im = load_image(src, max(W2, H2) * 2).convert("RGB")
    ratio = im.width / im.height
    if 1.25 <= ratio <= 2.1:
        scale = max(W2 / im.width, H2 / im.height)
        im = im.resize((round(im.width * scale), round(im.height * scale)), Image.LANCZOS)
        left, top = (im.width - W2) // 2, (im.height - H2) // 2
        im.crop((left, top, left + W2, top + H2)).save(dest, quality=93)
        return
    small = im.copy()
    small.thumbnail((W2 // 8, H2 // 8))
    scale = max(W2 / small.width, H2 / small.height)
    bg = small.resize((max(1, round(small.width * scale)), max(1, round(small.height * scale))), Image.BILINEAR)
    left, top = (bg.width - W2) // 2, (bg.height - H2) // 2
    bg = bg.crop((left, top, left + W2, top + H2)).filter(ImageFilter.GaussianBlur(24))
    bg = ImageEnhance.Brightness(ImageEnhance.Color(bg).enhance(0.8)).enhance(0.75)
    fit = min(W2 / im.width, H2 / im.height)
    fg = im.resize((round(im.width * fit), round(im.height * fit)), Image.LANCZOS)
    bg.paste(fg, ((W2 - fg.width) // 2, (H2 - fg.height) // 2))
    bg.save(dest, quality=93)


# ---- rendering ----------------------------------------------------------------
def _quiet_start(path: str, t: float, radius: float = 0.8) -> float:
    """The quietest moment within ``radius`` s of ``t`` (40 ms windows), so a
    piece doesn't open in the middle of a word when there's a pause nearby."""
    try:
        import numpy as np
    except ImportError:
        return t
    a = max(0.0, t - radius)
    raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{a:.3f}", "-i", path, "-t", f"{2 * radius:.3f}",
                          "-vn", "-ac", "1", "-ar", "8000", "-f", "s16le", "-"], capture_output=True).stdout
    x = np.frombuffer(raw, np.int16).astype(np.float32)
    n = len(x) // 320
    if n < 3:
        return t
    rms = np.sqrt((x[: n * 320].reshape(n, 320) ** 2).mean(axis=1))
    return round(a + int(np.argmin(rms)) * 0.04, 3)


def _fill_vf(w: int, h: int) -> str:
    """Square the pixels, then: the picture fitted, over a blurred, darker copy of
    itself filling the frame (instead of black bars)."""
    return (f"scale='trunc(iw*sar/2)*2':ih,setsar=1,split[a][b];"
            f"[a]scale={w // 4}:{h // 4}:force_original_aspect_ratio=increase,crop={w // 4}:{h // 4},"
            f"boxblur=8:2,scale={w}:{h},eq=brightness=-0.07:saturation=0.8[bg];"
            f"[b]scale={w}:{h}:force_original_aspect_ratio=decrease:flags=lanczos[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2")


# VHS / MiniDV frames carry black edges and head-switching noise at the bottom
# (TVs hid them as overscan): a film shows the picture without them. ~2 % a side.
_OVERSCAN = "crop=iw-28:ih-20:14:6"


def _is_sd(path: str) -> bool:
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                          "-of", "csv=p=0", path], capture_output=True, text=True).stdout.strip()
    try:
        w, h = (int(x) for x in out.split(",")[:2])
    except ValueError:
        return False
    return (w, h) in ((720, 576), (720, 480), (704, 576), (768, 576), (640, 480))


def _piece_cmd(c: dict, part: Path, cap: Path | None, *, w: int, h: int, still: Path | None) -> list[str]:
    d = c["d"]
    enc = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "14", "-pix_fmt", "yuv420p", "-c:a", "pcm_s16le",
           "-ar", "48000", "-ac", "2", "-t", f"{d:.3f}", str(part)]
    if still is not None:
        frames = int(round(d * FPS))
        vin = ["-loop", "1", "-framerate", str(FPS), "-t", f"{d:.3f}", "-i", str(still)]
        zoom = (f"[0:v]zoompan=z='1+0.07*on/{frames}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:"
                f"s={w}x{h}:fps={FPS},setsar=1[v0]")
        ain = ["-f", "lavfi", "-t", f"{d:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
        amap = "1:a"
        vf = zoom
    else:
        vin = ["-ss", f"{c['start']:.3f}", "-t", f"{d:.3f}", "-i", c["path"]]
        has_audio = bool(subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries",
                                         "stream=index", "-of", "csv=p=0", c["path"]],
                                        capture_output=True, text=True).stdout.strip())
        ain = [] if has_audio else ["-f", "lavfi", "-t", f"{d:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
        amap = None if has_audio else "1:a"
        sd = _is_sd(c["path"])
        vf = f"[0:v]{_OVERSCAN + ',' if sd else ''}{_fill_vf(w, h)},fps={FPS}[v0]"
    inputs = vin + ain
    n_in = 1 + (1 if ain else 0)
    if cap:
        inputs += ["-loop", "1", "-t", f"{d:.3f}", "-i", str(cap)]
        vf += (f";[{n_in}:v]format=rgba,fade=in:st=0.5:d=0.7:alpha=1,fade=out:st={max(1.0, d - 1.6):.2f}:d=0.7:"
               f"alpha=1[cap];[v0][cap]overlay=0:0:format=auto[v1]")
        vout = "[v1]"
    else:
        vout = "[v0]"
    vf += f";{vout}format=yuv420p[vo]"
    if amap is None:
        af = ["-af", "aresample=48000,aformat=channel_layouts=stereo,loudnorm=I=-20:TP=-2:LRA=11,"
                     "aresample=48000,apad"]
        amaps = ["-map", "0:a:0"]
    else:
        af, amaps = [], ["-map", amap]
    return ["ffmpeg", "-nostdin", "-y", "-v", "error", *inputs, "-filter_complex", vf, "-map", "[vo]", *amaps,
            *af, *enc]


def render(config, plan: list[dict], out: Path, *, title: str, subtitle: str, music: tuple[dict, Path] | None,
           offset: float = 0.0, lang: str = "pl", w: int | None = None, h: int | None = None, progress=None,
           check=lambda: None, nice: bool = True) -> float:
    """Render a planned film (items with ``d`` from fit_to_beats). Returns its length."""
    w, h = w or W, h or H
    work = out.with_suffix(".work")
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    pre = (["nice", "-n", "15"] if nice and shutil.which("nice") else [])
    report = progress or (lambda f: None)
    try:
        parts, last_cap = [], None
        for i, c in enumerate(plan):
            check()
            cap_text = caption(c.get("date"), lang)
            cap = None
            if cap_text and cap_text != last_cap:
                cap = work / f"cap{i:03d}.png"
                text_layer(cap, [(cap_text, 34, False)], w=w, h=h, where="bottom-left")
            last_cap = cap_text or last_cap
            still = None
            if c["kind"] == "photo":
                still = work / f"still{i:03d}.jpg"
                photo_still(Path(c["path"]), still, w, h)
            elif i:
                c = {**c, "start": _quiet_start(c["path"], c["start"])}
            part = work / f"p{i:03d}.mkv"
            subprocess.run(pre + _piece_cmd(c, part, cap, w=w, h=h, still=still), check=True, capture_output=True)
            parts.append(part)
            report(0.85 * (i + 1) / (len(plan) + 1))
        # end card: black with the music's credits
        end = work / "end.mkv"
        credit = music[0].get("credit") if music else None
        credits = work / "end.png"
        text_layer(credits, [(title, 56, True), (subtitle, 30, False)], w=w, h=h, where="end",
                   footer=(("Muzyka: " if lang == "pl" else "Music: ") + credit) if credit else None)
        subprocess.run(pre + ["ffmpeg", "-nostdin", "-y", "-v", "error", "-f", "lavfi", "-i",
                              f"color=c=black:s={w}x{h}:r={FPS}:d={END_S}", "-loop", "1", "-t", f"{END_S}", "-i",
                              str(credits), "-f", "lavfi", "-t", f"{END_S}", "-i", "anullsrc=r=48000:cl=stereo",
                              "-filter_complex", f"[1:v]format=rgba,fade=in:st={XFADE}:d=0.8:alpha=1[t];"
                              "[0:v][t]overlay=0:0,format=yuv420p[v]", "-map", "[v]", "-map", "2:a",
                              "-c:v", "libx264", "-preset", "veryfast", "-crf", "14", "-c:a", "pcm_s16le",
                              "-t", f"{END_S}", str(end)], check=True, capture_output=True)
        parts.append(end)
        durations = [c["d"] for c in plan] + [END_S]
        total = sum(durations) - XFADE * (len(durations) - 1)
        title_png = work / "title.png"
        text_layer(title_png, [(title, 96, True), (subtitle, 38, False)], w=w, h=h, where="centre")

        inputs = [x for p in parts for x in ("-i", str(p))]
        n = len(parts)
        g, v, a, off = [], "[0:v]", "[0:a]", 0.0
        for k in range(1, n):
            off += durations[k - 1] - XFADE
            g.append(f"{v}[{k}:v]xfade=transition=fade:duration={XFADE}:offset={off:.3f}[x{k}]")
            g.append(f"{a}[{k}:a]acrossfade=d={XFADE}:c1=tri:c2=tri[y{k}]")
            v, a = f"[x{k}]", f"[y{k}]"
        inputs += ["-loop", "1", "-t", f"{TITLE_S}", "-i", str(title_png)]
        g.append(f"[{n}:v]format=rgba,fade=in:st=0.4:d=0.8:alpha=1,fade=out:st={TITLE_S - 1:.2f}:d=0.9:alpha=1[tt]")
        g.append(f"{v}[tt]overlay=0:0:eof_action=pass,fade=in:st=0:d=0.8,format=yuv420p[vout]")
        if music:
            inputs += ["-ss", f"{max(0.0, offset - 0.0):.3f}", "-i", str(music[1])]
            m = n + 1
            g.append(f"{a}asplit[orig][key0]")
            g.append("[key0]highpass=f=250,lowpass=f=3500,volume=2[key]")
            g.append(f"[{m}:a]aresample=48000,aformat=channel_layouts=stereo,atrim=0:{total:.3f},"
                     f"afade=t=in:d=1.2,afade=t=out:st={max(0.0, total - 3.5):.3f}:d=3.5,volume=0.55[mus]")
            g.append("[mus][key]sidechaincompress=threshold=0.035:ratio=9:attack=40:release=700:makeup=1[duck]")
            g.append("[orig][duck]amix=inputs=2:normalize=0:duration=first,alimiter=limit=0.95[aout]")
        else:
            g.append(f"{a}anull[aout]")
        cmd = ["ffmpeg", "-nostdin", "-y", "-v", "error", *inputs, "-filter_complex", ";".join(g),
               "-map", "[vout]", "-map", "[aout]", "-c:v", "libx264", "-preset", "medium", "-crf", "18",
               "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-t", f"{total:.3f}",
               "-metadata", f"title={title} — {subtitle}",
               "-metadata", f"comment={('Muzyka: ' + music[0]['credit']) if music and music[0].get('credit') else ''}",
               "-movflags", "+faststart", "-progress", "pipe:1", "-nostats", str(out.with_suffix(".part.mp4"))]
        proc = subprocess.Popen(pre + cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for line in proc.stdout:
            if line.startswith("out_time_us="):
                try:
                    report(0.85 + 0.15 * min(1.0, int(line.split("=")[1]) / 1e6 / total))
                except ValueError:
                    pass
        err = proc.stderr.read()
        if proc.wait():
            raise RuntimeError(err[-400:] or "ffmpeg")
        out.with_suffix(".part.mp4").replace(out)
        return round(total, 2)
    finally:
        shutil.rmtree(work, ignore_errors=True)
        out.with_suffix(".part.mp4").unlink(missing_ok=True)
