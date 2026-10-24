"""Photos & videos from digital cameras and phones ("Zdjęcia i filmy").

Read-only source folders (MINIDV_MEDIA_DIRS, e.g. a NAS share mounted ro) are
indexed into the ``media`` table; nothing under them is ever written. Everything
derived lives under ``<storage>/media/``:
  thumbs/<id>.jpg   grid thumbnail (photo, or a frame of a video)
  display/<id>.jpg  a browser-viewable copy for formats browsers can't show (HEIC, TIFF…)
  proxy/<id>.mp4    an H.264 copy of a video browsers can't play (MTS, AVI, HEVC, 10-bit…)

Recording date, in order: EXIF DateTimeOriginal (photos) / the container's
creation time (videos; phones write UTC, converted to the local zone) → a date in
the file name (IMG_20190704_123456, VID-20190704-WA0001, 2019-07-04 12.34.56) →
none ("bez daty"). The file's mtime is never used: on a copied archive it's the
copy date. An operator fix (``date_fix``: a date or "unknown") always wins.

In the face pipeline every item is a "scene" of the pseudo tape MEDIA_TAPE whose
scene_id is the media id, so scanning, refinement, clustering, suggestions and
the People view handle photos and videos with the DV scenes.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .media import max_plausible_year

MEDIA_TAPE = "@media"
PHOTO_EXT = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp", ".gif", ".tif", ".tiff", ".bmp"}
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".3gp", ".avi", ".mts", ".m2ts", ".mkv", ".mpg", ".mpeg", ".wmv", ".webm",
             ".vob"}
# a DVD (a folder of VTS_xx_y.VOB files, usually called VIDEO_TS) shows up as one
# video per title set: the pseudo path <folder>/VTS_01 (no extension, so never a
# real media file) stands for VTS_01_1.VOB + VTS_01_2.VOB + … played back to back
_DVD_TITLE = re.compile(r"/VTS_(\d\d)$", re.I)
_DVD_FILE = re.compile(r"(VTS_\d\d_\d|VIDEO_TS)\.(VOB|IFO|BUP)$", re.I)
_DVD_PART = re.compile(r"VTS_(\d\d)_([1-9])\.VOB$", re.I)
DVD_MIN_BYTES = 20 * 1024 * 1024         # smaller title sets are menus / trailers
BROWSER_IMAGES = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
SETTLE_S = 120          # a file touched this recently may still be copying in
# not life photos: screenshots, and software / system folders copied along with a disk
SKIP_DIRS = {"screenshots", "screenshot", "zrzuty ekranu", "program files", "program files (x86)", "windows",
             "appdata", "$recycle.bin", "recycler", "system volume information", "@eadir", "#recycle", ".@__thumb"}
_SCREENSHOT = re.compile(r"^(screen ?shot|zrzut[ _]ekranu|capture d.écran|bildschirmfoto)", re.I)
MIN_PHOTO_PX = 300      # smaller images are icons / interface graphics
THUMB_PX = 480
DISPLAY_PX = 2560

_NAME_DMY = re.compile(r"(?<!\d)(0[1-9]|[12]\d|3[01])[.\-_](0[1-9]|1[0-2])[.\-_]((?:19|20)\d{2})(?!\d)")
_NAME_DT = re.compile(
    r"(?<!\d)((?:19|20)\d{2})[-_.]?(0[1-9]|1[0-2])[-_.]?(0[1-9]|[12]\d|3[01])"
    r"(?:[ _T-]?([01]\d|2[0-3])[-_.:h]?([0-5]\d)(?:[-_.:m]?([0-5]\d)\d{0,3})?)?(?!\d)")   # PXL_…_235959123: ms


def roots(config) -> list[Path]:
    return [Path(p) for p in (config.media_dirs or "").split(",") if p.strip()]


def kind_of(path: Path) -> str | None:
    ext = path.suffix.lower()
    return "photo" if ext in PHOTO_EXT else "video" if ext in VIDEO_EXT else None


def derived(config, folder: str, media_id: int, ext: str) -> Path:
    return config.storage / "media" / folder / f"{int(media_id)}{ext}"


# ---- dates ------------------------------------------------------------------
def _sane(iso: str | None, lo: int = 1990) -> str | None:
    """A date read from a device clock (EXIF, video metadata): digital cameras
    didn't exist before ~1990, so anything older is a reset clock."""
    if not iso or len(iso) < 10 or not iso[:4].isdigit():
        return None
    return iso if lo <= int(iso[:4]) <= max_plausible_year() else None


# what a dead camera / phone clock falls back to — never a real date in a name
_CLOCK_RESETS = ("1970-01-01", "1980-01-01", "1904-01-01")


def _sane_name(iso: str | None) -> str | None:
    """A date somebody (or a scanner, or a DVD recorder) put in a file or folder
    name can be old: scanned prints, VHS / Hi8 transfers. Only clock-reset
    defaults are refused."""
    iso = _sane(iso, lo=1900)
    return None if not iso or iso[:10] in _CLOCK_RESETS else iso


# Scanned prints / negatives and transferred tapes: their EXIF or container date
# is when they were scanned / grabbed, not when the picture was taken.
_ANALOG_WORDS = {"skan", "skany", "skanow", "skanów", "skanowane", "skanowanie", "zeskanowane", "scan", "scans",
                 "scanned", "scanning", "vhs", "svhs", "hi8", "video8", "8mm", "super8", "betamax", "grab", "graby",
                 "digitalizacja", "zdigitalizowane", "negatyw", "negatywy", "negative", "negatives", "slajd",
                 "slajdy", "slide", "slides", "odbitki", "kaseta", "kasety", "vhsrip"}
_ANALOG_NAME = re.compile(r"^(scan|skan|skanuj|scanimage|scanned|img_scan)[\W_]*\d*$", re.I)
_SCANNER = re.compile(r"scan|canoscan|perfection|coolscan|plustek|reflecta|photomyne|vuescan|silverfast|"
                      r"pic ?scanner|photoscan|epson", re.I)


def analog_origin(path: str | Path, *maker: str | None) -> bool:
    """True for a scan or a tape transfer: a scanner in the EXIF make / model /
    software (``maker``), a scanner's file name (Skanuj0001.jpg, scan_12.jpg) or a
    folder / file word like skany, VHS, Hi8, negatywy, slajdy."""
    if any(m and _SCANNER.search(str(m)) for m in maker):
        return True
    p = Path(path)
    if _ANALOG_NAME.match(p.stem):
        return True
    words = {w for part in p.parts[-4:] for w in re.split(r"[\W_]+", part.lower()) if w}
    return bool(words & _ANALOG_WORDS)


def date_from_folders(path: Path) -> str | None:
    """A date in the names of the two folders above a file ("Wesele 25.04.1987")."""
    return date_from_name(path.parent.name) or date_from_name(path.parent.parent.name)


def date_from_name(name: str) -> str | None:
    """'IMG_20190704_123456.jpg' -> '2019-07-04T12:34:56'; date-only names -> 'YYYY-MM-DD'."""
    m = _NAME_DT.search(name)
    if not m:
        d = _NAME_DMY.search(name)                     # "Wesele 15.08.2024" (folder names)
        return _sane_name(f"{d[3]}-{d[2]}-{d[1]}") if d else None
    y, mo, d, h, mi, s = m.groups()
    iso = f"{y}-{mo}-{d}" + (f"T{h}:{mi}:{s or '00'}" if h else "")
    return _sane_name(iso)


def _exif_dt(value) -> str | None:
    """EXIF '2019:07:04 12:34:56' -> ISO."""
    if not value:
        return None
    v = str(value).strip().replace("\x00", "")
    m = re.match(r"(\d{4})[:-](\d{2})[:-](\d{2})[ T](\d{2}):(\d{2}):(\d{2})", v)
    return _sane(f"{m[1]}-{m[2]}-{m[3]}T{m[4]}:{m[5]}:{m[6]}") if m else None


def _video_dt(info: dict, tz: str) -> str | None:
    """Local wall-clock recording time from ffprobe tags. Apple's creationdate
    carries its own offset (already local); creation_time is UTC."""
    tags = {}
    for src in [info.get("format", {})] + info.get("streams", []):
        for k, v in (src.get("tags") or {}).items():
            tags.setdefault(k.lower(), v)
    apple = tags.get("com.apple.quicktime.creationdate")
    if apple and len(apple) >= 19:
        return _sane(apple[:19].replace(" ", "T"))
    ct = tags.get("creation_time")
    if ct:
        try:
            dt = datetime.fromisoformat(ct.replace("Z", "+00:00"))
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return _sane(dt.astimezone(ZoneInfo(tz)).strftime("%Y-%m-%dT%H:%M:%S"))
    return None


def effective_date(row: dict) -> tuple[str | None, str | None]:
    """(YYYY-MM-DD, HH:MM:SS) shown on the timeline, or (None, None)."""
    fix = row.get("date_fix")
    if fix == "unknown":
        return None, None
    if fix:
        return fix[:10], None
    t = row.get("taken")
    if not t:
        return None, None
    return t[:10], (t[11:19] if len(t) >= 19 else None)


# ---- discovery ------------------------------------------------------------------
def walk(config):
    """(path, root, kind, size, mtime) for every settled media file under the roots."""
    now = time.time()
    for root in roots(config):
        if not root.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".") and d.lower() not in SKIP_DIRS]
            if any(_DVD_PART.match(fn) for fn in filenames):
                yield from _dvd_titles(Path(dirpath), filenames, root, now)
            # DVD structure files (menus, IFO/BUP, title parts) never count on their own
            filenames = [fn for fn in filenames if not _DVD_FILE.match(fn)]
            for fn in filenames:
                if fn.startswith(".") or _SCREENSHOT.match(fn):   # rsync / NAS temp files, screenshots
                    continue
                p = Path(dirpath) / fn
                kind = kind_of(p)
                if not kind:
                    continue
                try:
                    st = p.stat()
                except OSError:
                    continue
                if now - st.st_mtime < SETTLE_S or st.st_size == 0:
                    continue
                yield p, root, kind, st.st_size, st.st_mtime


def _dvd_titles(folder: Path, filenames: list[str], root: Path, now: float):
    """One pseudo item per DVD title set (see _DVD_TITLE), sized and dated by its VOBs."""
    titles: dict[str, list[os.stat_result]] = {}
    for fn in filenames:
        m = _DVD_PART.match(fn)
        if m:
            try:
                titles.setdefault(m[1], []).append((folder / fn).stat())
            except OSError:
                continue
    for num, stats in sorted(titles.items()):
        size, mtime = sum(s.st_size for s in stats), max(s.st_mtime for s in stats)
        if size >= DVD_MIN_BYTES and now - mtime >= SETTLE_S:
            yield folder / f"VTS_{num}", root, "video", size, mtime


def source(row: dict) -> str:
    """What ffmpeg should read for a row: the file, or a DVD title's VOBs joined
    with the concat protocol (they are one MPEG program stream split at 1 GB)."""
    path = row["path"]
    m = _DVD_TITLE.search(path)
    if not m or Path(path).suffix:
        return path
    folder = Path(path).parent
    parts = sorted((p for p in folder.iterdir() if (pm := _DVD_PART.match(p.name)) and pm[1] == m[1]),
                   key=lambda p: int(_DVD_PART.match(p.name)[2]))
    return "concat:" + "|".join(str(p) for p in parts)


def is_dvd(row: dict) -> bool:
    path = row.get("path") or ""
    return bool(_DVD_TITLE.search(path)) and not Path(path).suffix


def scan(config, store) -> dict:
    """Register new/changed files, forget vanished ones. A root that isn't there
    right now (NAS asleep, NFS hiccup) is left alone — its items are not dropped."""
    seen, added = set(), 0
    live_roots = [r for r in roots(config) if r.is_dir() and any(r.iterdir())]
    for p, root, kind, size, mtime in walk(config):
        # a DVD title belongs to the folder holding VIDEO_TS ("wesele rodziców"), not to VIDEO_TS
        dvd = kind == "video" and _DVD_TITLE.search(str(p)) and not p.suffix
        home = p.parent.parent if dvd and p.parent.name.upper() == "VIDEO_TS" else p.parent
        album = str(home.relative_to(root.parent))
        name = p.name
        if dvd:                                        # DVD: "wesele rodziców — DVD", not "VTS_01"
            num = int(p.name[-2:])
            name = f"{home.name} — DVD" + (f" {num}" if num > 1 else "")
        if store.media_seen(str(p), album=album, name=name, kind=kind, size=size, mtime=mtime,
                            workspace=config.media_workspace):
            added += 1
        seen.add(str(p))
    gone = [m["id"] for m in store.media_paths() if m["path"] not in seen
            and any(m["path"].startswith(str(r) + "/") for r in live_roots)]
    removed = store.media_delete(gone) + store.media_take_orphans()     # files gone, plus their clips
    for mid, path in removed:
        for folder, ext in (("thumbs", ".jpg"), ("display", ".jpg"), ("proxy", ".mp4")):
            derived(config, folder, mid, ext).unlink(missing_ok=True)
        if path.startswith(str(config.storage) + "/"):                   # a clip we cut ourselves
            Path(path).unlink(missing_ok=True)
    return {"seen": len(seen), "added": added, "removed": [mid for mid, _ in removed]}


# ---- per-item processing ----------------------------------------------------
def _pillow():
    from PIL import Image, ImageOps
    try:
        import pillow_heif
        pillow_heif.register_heif_opener()
    except ImportError:
        pass
    return Image, ImageOps


def load_image(path: Path, max_side: int):
    """Upright RGB PIL image no larger than max_side."""
    Image, ImageOps = _pillow()
    im = Image.open(path)
    im.draft("RGB", (max_side, max_side))            # cheap JPEG downscale while decoding
    im = ImageOps.exif_transpose(im).convert("RGB")
    im.thumbnail((max_side, max_side))
    return im


def _process_photo(config, row: dict) -> dict:
    Image, ImageOps = _pillow()
    path = Path(row["path"])
    with Image.open(path) as im:
        exif = im.getexif()
        sub = exif.get_ifd(0x8769) if exif else {}
        taken = _exif_dt(sub.get(0x9003) or sub.get(0x9004) or exif.get(0x0132))
        camera = " ".join(str(exif.get(t) or "").strip() for t in (0x010F, 0x0110)).strip() or None
        software = str(exif.get(0x0131) or "").strip() if exif else ""
        up = ImageOps.exif_transpose(im)
        width, height = up.size
    if max(width, height) < MIN_PHOTO_PX:                 # an icon / button, not a photo
        return {"status": "ignored", "width": width, "height": height}
    taken, src = _pick_date(path, taken, "exif", analog_origin(path, camera, software))
    thumb = load_image(path, THUMB_PX)
    out = derived(config, "thumbs", row["id"], ".jpg")
    out.parent.mkdir(parents=True, exist_ok=True)
    thumb.save(out, "JPEG", quality=82)
    if path.suffix.lower() not in BROWSER_IMAGES:
        disp = derived(config, "display", row["id"], ".jpg")
        disp.parent.mkdir(parents=True, exist_ok=True)
        load_image(path, DISPLAY_PX).save(disp, "JPEG", quality=88)
    return {"status": "ready", "taken": taken, "date_source": src, "width": width, "height": height,
            "camera": camera[:80] if camera else None, "playable": 1}


def _process_video(config, row: dict) -> dict:
    from .media import ffprobe
    path = Path(row["path"])
    src_in = source(row)
    info = ffprobe(src_in)
    v = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), None)
    if not v:
        raise RuntimeError("brak ścieżki wideo")
    a = next((s for s in info.get("streams", []) if s.get("codec_type") == "audio"), None)
    dur = float(info.get("format", {}).get("duration") or v.get("duration") or 0)
    tags = {k.lower(): v for src_ in [info.get("format", {})] + info.get("streams", [])
            for k, v in (src_.get("tags") or {}).items()}
    # a DVD (VHS transfer) has no recording date of its own: its folder may
    analog = is_dvd(row) or analog_origin(path, tags.get("encoder"), tags.get("make"), tags.get("model"))
    taken, src = _pick_date(path, _video_dt(info, config.default_recording_tz), "meta", analog)
    playable = (path.suffix.lower() in (".mp4", ".m4v", ".mov") and v.get("codec_name") == "h264"
                and v.get("pix_fmt") in ("yuv420p", "yuvj420p")
                and (a is None or a.get("codec_name") in ("aac", "mp3")))
    out = derived(config, "thumbs", row["id"], ".jpg")
    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(_nice() + ["ffmpeg", "-nostdin", "-v", "error", "-y", "-ss", f"{min(30.0, dur * 0.1):.2f}",
                              "-i", src_in, "-frames:v", "1", "-vf",
                              f"scale='trunc(iw*sar/2)*2':ih,setsar=1,scale={THUMB_PX}:-2", str(out)],
                   check=True, timeout=300)
    return {"status": "ready", "taken": taken, "date_source": src, "width": v.get("width"),
            "height": v.get("height"), "duration": dur, "playable": int(playable),
            "fps": stream_fps(v) if playable else None}


def _pick_date(path: Path, device: str | None, device_src: str, analog: bool) -> tuple[str | None, str | None]:
    """(taken, date_source). A camera / phone file: its own clock first, then a
    date in its name. A scan or tape transfer: the date in its name or folder;
    its own clock only says when it was scanned, so without a name date it stays
    undated ("scan": not dated from file times / neighbours either)."""
    if analog:
        # a name / folder date close to the scan's own clock is the scanning
        # session ("2010-06-08 (H)/… Skany"), not when the picture was taken
        def older(d: str | None) -> bool:
            return bool(d) and (not device or int(d[:4]) < int(device[:4]) - 1 or
                                (int(d[:4]) == int(device[:4]) - 1 and d[5:10] < device[5:10]))
        if older(named := date_from_name(path.name)):
            return named, "name"
        if older(folder := date_from_folders(path)):
            return folder, "folder"
        return None, "scan" if device else None
    if device:
        return device, device_src
    named = date_from_name(path.name)
    return named, "name" if named else None


def stream_fps(stream: dict) -> float | None:
    """Frames per second of an ffprobe video stream (average rate first: phones
    record variable frame rate and r_frame_rate is then the timebase's ceiling)."""
    for key in ("avg_frame_rate", "r_frame_rate"):
        num, _, den = (stream.get(key) or "").partition("/")
        try:
            fps = float(num) / float(den or 1)
        except (ValueError, ZeroDivisionError):
            continue
        if 1 <= fps <= 240:
            return round(fps, 3)
    return None


def file_fps(path) -> float | None:
    from .media import ffprobe
    try:
        v = next((s for s in ffprobe(path).get("streams", []) if s.get("codec_type") == "video"), None)
    except Exception:  # noqa: BLE001 - unreadable file: the rate stays unknown
        return None
    return stream_fps(v) if v else None


def _nice() -> list[str]:
    p = ["nice", "-n", "19"] if shutil.which("nice") else []
    return p + (["ionice", "-c", "3"] if p and shutil.which("ionice") else [])


def _no_report(stage: str, i: int, total: int, detail: str, frac: float = 0.0) -> None:
    pass


def process_pending(config, store, check=lambda: None, busy=lambda: False, budget_s: float = 600,
                    report=_no_report) -> int:
    """Thumbnails / dates / probes for newly seen files (fast, minutes per thousand).
    Returns after ``budget_s`` so the folder check (new / deleted files) comes round
    again regularly even with a long backlog."""
    done, until = 0, time.monotonic() + budget_s
    rows = store.media_pending()
    for i, row in enumerate(rows):
        if time.monotonic() > until:
            break
        report("thumbs", i, len(rows), row["name"])
        check()
        while busy():
            time.sleep(10)
        try:
            fields = (_process_photo if row["kind"] == "photo" else _process_video)(config, row)
        except Exception as exc:  # noqa: BLE001 - one broken file must not stop the rest
            fields = {"status": "error", "error": str(exc)[:300]}
        store.media_update(row["id"], **fields)
        done += 1
    return done


def _ffmpeg_with_progress(cmd: list[str], duration: float, on_frac) -> tuple[int, str]:
    """Run ffmpeg, calling on_frac(0..1) from its -progress output; (returncode, stderr tail)."""
    proc = subprocess.Popen(cmd[:-1] + ["-progress", "pipe:1", "-nostats", cmd[-1]], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
    for line in proc.stdout:
        if line.startswith("out_time_us=") and duration:
            try:
                on_frac(min(1.0, int(line.split("=", 1)[1]) / 1e6 / duration))
            except ValueError:
                pass
    err = proc.stderr.read()
    return proc.wait(), err[-300:]


def dvd_interlaced(src: str) -> bool:
    """A DVD title is interlaced unless its stream says progressive (a film DVD).
    VHS transfers are always interlaced; measured on the 1987 wedding disc every
    frame is TFF with distinct motion in each field."""
    from .media import ffprobe
    v = next((s for s in ffprobe(src).get("streams", []) if s.get("codec_type") == "video"), {})
    return v.get("field_order") not in ("progressive",)


def dvd_proxy_cmd(src: str, out: Path, *, interlaced: bool, crf: int, preset: str,
                  keyframes: list[float] = ()) -> list[str]:
    """DVD (VHS/camcorder transfer) -> browser copy without throwing anything away:
    every field becomes a frame (bwdif send_field: 50 real motion samples a second,
    nothing interpolated in time; field order read from each frame's flags), the
    native 720x576 is kept with its pixel aspect (players show 4:3 / 16:9, one
    resampling step less), no denoise / sharpening. Keyframes at least every 2 s
    (plus ``keyframes``) so lossless clip cuts land close to where they should."""
    vf = ("bwdif=mode=send_field:parity=auto:deint=interlaced," if interlaced else "") + "format=yuv420p"
    kf = ["-force_key_frames", ",".join(f"{t:.3f}" for t in keyframes)] if keyframes else []
    return ["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", src, "-map", "0:v:0", "-map", "0:a:0?",
            "-vf", vf, "-c:v", "libx264", "-preset", preset, "-crf", str(crf), "-g", "100", *kf,
            "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(out)]


def build_proxies(config, store, check=lambda: None, busy=lambda: False, budget_s: float = 600,
                  report=_no_report) -> int:
    """H.264 copies of videos browsers can't play (slow; one at a time, idle priority).
    Time-boxed like process_pending, so deletions show up while a queue is long."""
    done, until = 0, time.monotonic() + budget_s
    rows = store.media_needing_proxy()
    for i, row in enumerate(rows):
        if time.monotonic() > until:
            break
        report("convert", i, len(rows), row["name"])
        check()
        while busy():
            time.sleep(10)
        out = derived(config, "proxy", row["id"], ".mp4")
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_suffix(".part.mp4")
        if is_dvd(row):
            cmd = dvd_proxy_cmd(source(row), tmp, interlaced=dvd_interlaced(source(row)),
                                crf=config.media_dvd_crf, preset=config.media_dvd_preset)
        else:
            # bwdif only touches frames flagged interlaced (MTS, AVI); the sar step makes
            # anamorphic pixels square so 4:3 / 16:9 come out right
            cmd = ["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", source(row), "-map", "0:v:0", "-map", "0:a:0?",
                   "-vf", "bwdif=mode=send_frame:deint=interlaced,"
                          "scale='trunc(iw*sar/2)*2':ih,setsar=1,"
                          "scale='min(1280,iw)':-2,format=yuv420p", "-c:v", "libx264",
                   "-preset", "veryfast", "-crf", "23", "-c:a", "aac", "-b:a", "128k",
                   "-movflags", "+faststart", str(tmp)]
        rc, err = _ffmpeg_with_progress(
            _nice() + cmd, row.get("duration") or 0,
            lambda f, i=i, n=row["name"]: report("convert", i, len(rows), n, f))
        if rc == 0 and tmp.exists():
            tmp.replace(out)
            store.media_update(row["id"], proxy=1, fps=file_fps(out))
        else:
            tmp.unlink(missing_ok=True)
            store.media_update(row["id"], proxy=-1, error=err or "ffmpeg")
        done += 1
    return done


def backfill_fps(config, store, budget_s: float = 60) -> int:
    """Record the frame rate of playable videos indexed before it was kept (the
    player steps frame by frame with it). A header probe each, cheap."""
    done, until = 0, time.monotonic() + budget_s
    for row in store.media_needing_fps():
        if time.monotonic() > until:
            break
        p = Path(row["path"]) if row["parent_id"] or row["playable"] else video_path(config, row)
        store.media_update(row["id"], fps=(file_fps(p) if p and p.exists() else None) or 0)
        done += 1
    return done


def keyframe_times(path: Path) -> list[float]:
    cmd = ["ffprobe", "-v", "error", "-select_streams", "v:0", "-skip_frame", "nokey", "-show_entries",
           "frame=pts_time", "-of", "csv=p=0", str(path)]
    out = subprocess.run(_nice() + cmd, capture_output=True, text=True).stdout
    times = []
    for line in out.splitlines():           # "0.000000" or "0.000000," (a frame with side data)
        try:
            times.append(float(line.split(",")[0]))
        except ValueError:
            continue
    return sorted(times)


def _kf_at(keyframes: list[float], t: float) -> float:
    """Where a lossless cut asked to start at ``t`` really starts: the last keyframe
    at or before it (ffmpeg -ss + -c copy)."""
    return max((k for k in keyframes if k <= t + 0.005), default=0.0)


def reencode_dvd(config, store, row: dict, report=_no_report, check=lambda: None) -> bool:
    """Re-encode one DVD title's copy with today's settings (dvd_proxy_cmd), on
    explicit request only (``reproxy``). The film's parts keep their ids, bounds and
    names; each new part starts on a keyframe forced at its exact bound, and every
    face time found in a part moves by how much earlier the old part file began
    (old cuts started on the keyframe before the bound), so "jump to the face"
    still lands on the same moment. Originals are only read; the old files are
    replaced only once every new one is complete."""
    proxy = derived(config, "proxy", row["id"], ".mp4")
    parts = store.media_children(row["id"]) if row.get("split") == 1 else []
    src = source(row)
    interlaced = dvd_interlaced(src)
    from .media import ffprobe
    v = next((s for s in ffprobe(src).get("streams", []) if s.get("codec_type") == "video"), {})
    rate = (stream_fps(v) or 25.0) * (2 if interlaced else 1)
    # the bound on a frame of the new copy, so the forced keyframe is exactly there
    starts = [int(p["clip_start"] * rate + 1e-6) / rate for p in parts]
    tmp = proxy.with_suffix(".next.mp4")
    made: list[Path] = [tmp]
    try:
        rc, err = _ffmpeg_with_progress(
            _nice() + dvd_proxy_cmd(src, tmp, interlaced=interlaced, crf=config.media_dvd_crf,
                                    preset=config.media_dvd_preset, keyframes=[t for t in starts if t > 0]),
            row.get("duration") or 0, lambda f: report("convert", 0, 1, row["name"], f))
        if rc or not tmp.exists():
            raise RuntimeError(err or "ffmpeg")
        check()
        old_kf = keyframe_times(proxy) if parts and proxy.exists() else []
        new_kf = keyframe_times(tmp) if parts else []
        moves = []
        for p, k in zip(parts, starts):
            out = Path(p["path"]).with_suffix(".next.mp4")
            made.append(out)
            cp = subprocess.run(_nice() + ["ffmpeg", "-nostdin", "-v", "error", "-y", "-ss", f"{k + 0.002:.3f}",
                                           "-i", str(tmp), "-t", f"{p['clip_end'] - k:.3f}", "-map", "0:v:0",
                                           "-map", "0:a?", "-c", "copy", "-avoid_negative_ts", "make_zero",
                                           "-movflags", "+faststart", str(out)], capture_output=True, text=True)
            if cp.returncode or not out.exists():
                raise RuntimeError((cp.stderr or "ffmpeg")[-300:])
            moves.append((p, out, _kf_at(old_kf, p["clip_start"]) - _kf_at(new_kf, k)))
        check()
        fps = file_fps(tmp)
        tmp.replace(proxy)
        for p, out, delta in moves:
            out.replace(p["path"])
            store.face_shift_times(MEDIA_TAPE, str(p["id"]), delta, p["clip_end"] - p["clip_start"] + 1)
            store.media_update(p["id"], fps=fps, size=Path(p["path"]).stat().st_size)
            thumb = derived(config, "thumbs", p["id"], ".jpg")
            subprocess.run(_nice() + ["ffmpeg", "-nostdin", "-v", "error", "-y", "-ss",
                                      f"{min(2.0, (p['clip_end'] - p['clip_start']) * 0.1):.2f}", "-i", p["path"],
                                      "-frames:v", "1", "-vf", f"scale='trunc(iw*sar/2)*2':ih,setsar=1,"
                                      f"scale={THUMB_PX}:-2", str(thumb)])
        store.media_update(row["id"], proxy=1, fps=fps, reproxy=0, error=None)
        return True
    except Exception as exc:  # noqa: BLE001 - the old copy stays as it was
        store.media_update(row["id"], reproxy=-1, error=str(exc)[-300:])
        return False
    finally:
        for f in made:
            f.unlink(missing_ok=True)


# ---- long videos -> scene clips -----------------------------------------------
def detect_cuts(path: Path, threshold: float = 0.3) -> list[tuple[float, float]]:
    """(time, score) of picture changes (shot cuts, scene changes), from a small
    4 fps copy of the video — cheap enough for an hour-long film."""
    cmd = _nice() + ["ffmpeg", "-nostdin", "-v", "error", "-i", str(path), "-an", "-vf",
                     f"fps=4,scale=256:-2,select='gt(scene,{threshold})',metadata=print:file=-", "-f", "null", "-"]
    out = subprocess.run(cmd, capture_output=True, text=True).stdout
    cuts, t = [], None
    for line in out.splitlines():
        m = re.search(r"pts_time:([\d.]+)", line)
        if m:
            t = float(m[1])
            continue
        m = re.search(r"lavfi\.scene_score=([\d.]+)", line)
        if m and t is not None:
            cuts.append((t, float(m[1])))
            t = None
    return cuts


def plan_clips(cuts: list[tuple[float, float]], duration: float, min_len: float = 45,
               max_len: float = 300) -> list[tuple[float, float]]:
    """Cut a long video into clips of roughly min_len..max_len seconds, each ending
    on the strongest picture change inside that window (or at max_len if there is
    none — a long static shot). A short tail joins the clip before it."""
    bounds, start = [0.0], 0.0
    while duration - start > max_len:
        window = [c for c in cuts if start + min_len <= c[0] <= start + max_len]
        start = max(window, key=lambda c: c[1])[0] if window else start + max_len
        bounds.append(start)
    if len(bounds) > 1 and duration - bounds[-1] < min_len:
        bounds.pop()
    bounds.append(duration)
    return [(bounds[i], bounds[i + 1]) for i in range(len(bounds) - 1)]


def _mmss(s: float) -> str:
    s = int(s)
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


def _shift(taken: str | None, seconds: float) -> str | None:
    if not taken or len(taken) < 19:
        return taken
    from datetime import timedelta
    return (datetime.fromisoformat(taken) + timedelta(seconds=int(seconds))).strftime("%Y-%m-%dT%H:%M:%S")


def split_long_videos(config, store, check=lambda: None, busy=lambda: False, budget_s: float = 600,
                      report=_no_report) -> list[int]:
    """Replace videos longer than media_split_min_s by scene clips (lossless cuts,
    each a small file that opens at once). Returns the ids of the split parents."""
    done, until = [], time.monotonic() + budget_s
    rows = store.media_needing_split(config.media_split_min_s)
    for i, row in enumerate(rows):
        if time.monotonic() > until:
            break
        report("split", i, len(rows), row["name"])
        check()
        while busy():
            time.sleep(10)
        src = video_path(config, row)
        if src is None:
            continue
        clips = plan_clips(detect_cuts(src), row["duration"] or 0)
        if len(clips) < 2:
            store.media_update(row["id"], split=0)
            continue
        stem = Path(row["name"]).stem
        made = []
        for k, (a, b) in enumerate(clips):
            out = config.storage / "media" / "clips" / f"{row['id']}_{k:03d}.mp4"
            out.parent.mkdir(parents=True, exist_ok=True)
            cp = subprocess.run(_nice() + ["ffmpeg", "-nostdin", "-v", "error", "-y", "-ss", f"{a:.3f}", "-i", str(src),
                                           "-t", f"{b - a:.3f}", "-map", "0:v:0", "-map", "0:a?", "-c", "copy",
                                           "-avoid_negative_ts", "make_zero", "-movflags", "+faststart", str(out)],
                                capture_output=True, text=True)
            if cp.returncode or not out.exists():
                for _, p in made:
                    Path(p).unlink(missing_ok=True)
                store.media_update(row["id"], split=0, error=(cp.stderr or "ffmpeg")[-300:])
                made = None
                break
            made.append((k, str(out)))
        if made is None:
            continue
        for k, path in made:
            a, b = clips[k]
            cid = store.media_add_clip(row, path=path, start=a, end=b, taken=_shift(row["taken"], a),
                                       name=f"{stem} — {k + 1:02d}/{len(clips)} ({_mmss(a)}–{_mmss(b)})")
            thumb = derived(config, "thumbs", cid, ".jpg")
            subprocess.run(_nice() + ["ffmpeg", "-nostdin", "-v", "error", "-y", "-ss", f"{min(2.0, (b - a) * 0.1):.2f}",
                                      "-i", path, "-frames:v", "1", "-vf",
                                      f"scale='trunc(iw*sar/2)*2':ih,setsar=1,scale={THUMB_PX}:-2", str(thumb)])
        store.media_update(row["id"], split=1)
        done.append(row["id"])
    return done


# ---- dates from file times (no EXIF / no metadata / nothing in the name) -------
FILE_DATE_TOLERANCE_S = 90 * 60     # EXIF vs file time agree within this -> the folder's file times are real
COPY_WINDOW_S = 3 * 86400           # a file time this close to when we first saw it is a copy date


def date_from_file_times(config, store) -> int:
    """Some files carry no date at all (e.g. Sony Cyber-shot MOV*.MPG, MPEG-1 with no
    tags) but kept their original file time through the copies. That time is used
    only where it can be trusted:
      * a folder with ≥3 EXIF-dated photos: ≥70% of them must have a file time
        matching their EXIF time (camera clock = file time, local zone);
      * otherwise: not a bulk copy (≤ half the folder within 10 min of each
        other) and not the day the archive was copied in.
    Accepted dates get date_source "file" (shown as approximate); rejected ones
    "none", so they're not looked at again. Returns how many were dated."""
    tz = ZoneInfo(config.default_recording_tz)
    local = lambda mt: datetime.fromtimestamp(mt, tz).replace(tzinfo=None)  # noqa: E731
    by_album: dict[str, list[dict]] = {}
    for row in store.media_undated():
        by_album.setdefault(row["album"], []).append(row)
    dated = 0
    for album, rows in by_album.items():
        folder = store.media_album_rows(album)
        ref = [r for r in folder if r["date_source"] == "exif" and r["taken"] and len(r["taken"]) >= 19 and r["mtime"]]
        if len(ref) >= 3:
            agree = sum(1 for r in ref
                        if abs((local(r["mtime"]) - datetime.fromisoformat(r["taken"])).total_seconds())
                        <= FILE_DATE_TOLERANCE_S)
            trusted = agree / len(ref) >= 0.7
        else:
            mts = sorted(r["mtime"] for r in folder if r["mtime"])
            mid = mts[len(mts) // 2] if mts else 0
            trusted = len(mts) < 3 or sum(1 for m in mts if abs(m - mid) <= 600) / len(mts) <= 0.5
        for row in rows:
            seen = datetime.fromisoformat(row["added_at"]).timestamp() if row.get("added_at") else time.time()
            taken = None
            if trusted and row["mtime"] and seen - row["mtime"] > COPY_WINDOW_S:
                taken = _sane(local(row["mtime"]).strftime("%Y-%m-%dT%H:%M:%S"))
            store.media_update(row["id"], taken=taken, date_source="file" if taken else "none")
            if taken:
                dated += 1
                for clip in store.media_children(row["id"]):         # parts of a split film follow
                    store.media_update(clip["id"], taken=_shift(taken, clip["clip_start"] or 0),
                                       date_source="file")
    return dated


NEIGHBOR_MAX_GAP = 20              # file-counter distance to the dated neighbour
NEIGHBOR_MAX_SPAN_S = 36 * 3600    # the two neighbours must be this close in time
_COUNTER = re.compile(r"(\d{3,})\D*$")


def _counter(name: str) -> int | None:
    """The camera's file counter: DSC07359.JPG / MOV07362.MPG / IMG_1234.JPG -> 7359 / 7362 / 1234."""
    m = _COUNTER.search(Path(name).stem)
    return int(m[1]) if m else None


def date_from_neighbors(config, store) -> int:
    """Files still undated after date_from_file_times (date_source "none"): a camera
    numbers photos and videos with one counter (DSC07359, MOV07362, DSC07363), so a
    file sits between its numbered neighbours in time. Dated when the nearest dated
    file before and after it (same folder, ≤ NEIGHBOR_MAX_GAP apart) are within
    NEIGHBOR_MAX_SPAN_S of each other — at the midpoint, date_source "neighbors".
    Otherwise "nodate" (final: the operator can still set it)."""
    by_album: dict[str, list[dict]] = {}
    for row in store.media_rows_by_source("none"):
        by_album.setdefault(row["album"], []).append(row)
    dated = 0
    for album, rows in by_album.items():
        known = sorted((c, datetime.fromisoformat(r["taken"]))
                       for r in store.media_album_rows(album)
                       if r["taken"] and r["date_source"] not in ("none", "nodate", "neighbors")
                       and (c := _counter(r["name"])) is not None)
        for row in rows:
            n, taken = _counter(row["name"]), None
            if n is not None:
                before = [k for k in known if k[0] < n and n - k[0] <= NEIGHBOR_MAX_GAP]
                after = [k for k in known if k[0] > n and k[0] - n <= NEIGHBOR_MAX_GAP]
                if before and after:
                    a, b = before[-1][1], after[0][1]
                    if abs((b - a).total_seconds()) <= NEIGHBOR_MAX_SPAN_S:
                        taken = (a + (b - a) / 2).strftime("%Y-%m-%dT%H:%M:%S")
            store.media_update(row["id"], taken=taken, date_source="neighbors" if taken else "nodate")
            if taken:
                dated += 1
                for clip in store.media_children(row["id"]):
                    store.media_update(clip["id"], taken=_shift(taken, clip["clip_start"] or 0),
                                       date_source="neighbors")
    return dated


def redate_analog(store, dry_run: bool = False) -> list[tuple[int, str | None, str | None]]:
    """Once, for files indexed before scans / tape transfers were told apart:
    re-date them from their name or folder (never touching an operator's fix).
    Returns [(id, old taken, new taken)] of what changed."""
    changed = []
    for row in store.media_ready_originals():
        if row["date_fix"] or not analog_origin(row["path"], row.get("camera")):
            continue
        path = Path(row["path"])
        taken, src = _pick_date(path, row["taken"] if row["date_source"] in ("exif", "meta") else None,
                                row["date_source"] or "", True)
        if taken == row["taken"] and src == row["date_source"]:
            continue
        if not taken and row["date_source"] not in ("exif", "meta", "file", "neighbors"):
            continue                                     # undated already, nothing better to say
        changed.append((row["id"], row["taken"], taken))
        if dry_run:
            continue
        store.media_update(row["id"], taken=taken, date_source=src or "scan")
        for clip in store.media_children(row["id"]):
            store.media_update(clip["id"], taken=_shift(taken, clip["clip_start"] or 0), date_source=src or "scan")
    return changed


def video_path(config, row: dict) -> Path | None:
    """What a browser (and the face scanner) should play: the original if it's
    already H.264 MP4, else the proxy once it's built."""
    if row["kind"] != "video":
        return None
    if row.get("playable"):
        return Path(row["path"])
    p = derived(config, "proxy", row["id"], ".mp4")
    return p if row.get("proxy") == 1 and p.exists() else None


def face_source(config, store, scene_id: str) -> tuple[str, Path] | None:
    """('photo' | 'video', path) for the face pipeline, or None if not ready yet."""
    row = store.media_get(int(scene_id)) if str(scene_id).isdigit() else None
    if not row or row["status"] != "ready":
        return None
    if row["kind"] == "photo":
        return "photo", Path(row["path"])
    p = video_path(config, row)
    return ("video", p) if p else None


def public(row: dict) -> dict:
    """An item as the api returns it (no host paths)."""
    date, tm = effective_date(row)
    return {"id": row["id"], "kind": row["kind"], "name": row["name"], "album": row["album"], "date": date,
            "time": tm, "date_source": "fix" if row.get("date_fix") else row.get("date_source"),
            "date_fix": row.get("date_fix"), "width": row.get("width"), "height": row.get("height"),
            "duration": row.get("duration"), "camera": row.get("camera"), "status": row["status"],
            "playable": bool(row.get("playable")) or row.get("proxy") == 1, "size": row.get("size"),
            "parent_id": row.get("parent_id"), "clip_start": row.get("clip_start"), "clip_end": row.get("clip_end"),
            "fps": row.get("fps") or None}

