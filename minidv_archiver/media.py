from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path


NICE_PREFIX: list[str] = []  # set by process_capture(nice=True) to yield CPU/IO to a concurrent capture


def run(cmd: list[str], log=None, timeout=None) -> subprocess.CompletedProcess:
    if NICE_PREFIX and cmd and cmd[0] in {"zstd", "ffmpeg", "dvgrab"}:
        cmd = NICE_PREFIX + cmd
    cp = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
    if log:
        log(cp.stdout.rstrip())
    if cp.returncode:
        raise RuntimeError(f"command failed ({cp.returncode}): {' '.join(cmd)}\n{cp.stdout[-4000:]}")
    return cp


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as src:
        for chunk in iter(lambda: src.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_zstd_stream(path: Path) -> str:
    digest = hashlib.sha256()
    proc = subprocess.Popen(["zstd", "-q", "-dc", str(path)], stdout=subprocess.PIPE)
    assert proc.stdout
    for chunk in iter(lambda: proc.stdout.read(8 * 1024 * 1024), b""):
        digest.update(chunk)
    if proc.wait() != 0:
        raise RuntimeError(f"zstd decompression failed: {path}")
    return digest.hexdigest()


def probe_json(cmd: list[str]) -> dict:
    """Run an ffprobe JSON query with stderr kept OUT of stdout, so json.loads never
    chokes on DV decoder noise (concealment errors from ragged manual-STOP tail scenes)."""
    cp = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    if cp.returncode:
        raise RuntimeError(f"command failed ({cp.returncode}): {' '.join(cmd)}")
    try:
        return json.loads(cp.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"ffprobe returned non-JSON ({' '.join(cmd)}): {cp.stdout[:200]!r}") from exc


def ffprobe(path: Path) -> dict:
    return probe_json(["ffprobe", "-v", "quiet", "-show_format", "-show_streams", "-print_format", "json", str(path)])


def last_frame_timecode(path: Path, frame_size: int, temp_root: Path) -> str | None:
    """Probe the embedded timecode from the final complete DV frame."""
    with tempfile.NamedTemporaryFile(prefix="last-frame-", suffix=".dv", dir=temp_root) as tmp:
        with path.open("rb") as source:
            source.seek(-frame_size, os.SEEK_END)
            tmp.write(source.read(frame_size))
            tmp.flush()
        data = ffprobe(Path(tmp.name))
    return data.get("format", {}).get("tags", {}).get("timecode")


_DECODE_ERR = re.compile(rb"conceal|error while decoding|corrupt|Invalid data found|damaged", re.I)


def scene_probe_quality(path: Path, frame_count: int) -> tuple[int, list[str]]:
    """One decode pass over a raw DV scene: count concealment / decode errors (tape /
    head damage — distinct from capture-transport dropped frames) and hash three
    16x16 grayscale frames (start / middle / end) as a content fingerprint, so the
    same footage can be matched across separate captures."""
    n = max(1, frame_count)
    picks = sorted({0, n // 2, n - 1})
    sel = "+".join(rf"eq(n\,{p})" for p in picks)
    try:
        cp = subprocess.run(
            ["ffmpeg", "-nostdin", "-v", "warning", "-i", str(path),
             "-vf", f"select='{sel}',scale=16:16,format=gray", "-vsync", "0", "-f", "rawvideo", "-"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=1800)
    except (subprocess.TimeoutExpired, OSError):
        return 0, []
    errors = sum(1 for ln in (cp.stderr or b"").splitlines() if _DECODE_ERR.search(ln))
    data, size = cp.stdout or b"", 16 * 16
    hashes = [hashlib.sha1(data[i * size:(i + 1) * size]).hexdigest()[:12]
              for i in range(len(data) // size)]
    return errors, hashes


def first_frame_interlaced(path: Path) -> tuple[bool, bool | None]:
    frames = probe_json(["ffprobe", "-v", "quiet", "-select_streams", "v:0", "-read_intervals", "%+#1",
                         "-show_frames", "-show_entries", "frame=interlaced_frame,top_field_first", "-of", "json",
                         str(path)]).get("frames", [])
    return (bool(frames and frames[0].get("interlaced_frame")),
            bool(frames[0].get("top_field_first")) if frames else None)


def recording_datetime(path: Path, temp_root: Path) -> tuple[str | None, str]:
    probe_dir = Path(tempfile.mkdtemp(prefix="date-", dir=temp_root))
    try:
        cp = run(["dvgrab", "-I", str(path), "-timestamp", "-frames", "1", "-format", "raw", str(probe_dir / "probe")])
        candidates = list(probe_dir.glob("*.dv"))
        joined = " ".join([cp.stdout] + [p.name for p in candidates])
        match = re.search(r"(\d{4})[.-](\d{2})[.-](\d{2})[_ ](\d{2})[-:](\d{2})[-:](\d{2})", joined)
        if match:
            return f"{match[1]}-{match[2]}-{match[3]}T{match[4]}:{match[5]}:{match[6]}", "DV_VAUX"
        return None, "unavailable"
    finally:
        shutil.rmtree(probe_dir, ignore_errors=True)


def split_capture(capture: Path, split_dir: Path, log) -> tuple[list[Path], list[str]]:
    split_dir.mkdir(parents=True, exist_ok=True)
    cp = run(["dvgrab", "-I", str(capture), "-autosplit", "-srt", "-format", "raw", "-size", "0",
              str(split_dir / "scene")], log=log)
    scenes = sorted(split_dir.glob("scene[0-9][0-9][0-9].dv"))
    if not scenes:
        raise RuntimeError("dvgrab produced no scenes")
    discontinuities = [line.replace("\x07", "").strip() for line in cp.stdout.splitlines()
                       if "frame dropped" in line.lower()]
    return scenes, discontinuities


def _meta_args(meta: dict | None) -> list[str]:
    out: list[str] = []
    for key, val in (meta or {}).items():
        if val:
            out += ["-metadata", f"{key}={val}"]
    return out


def scene_metadata(tape_id: str, index: int, dt: str | None, dt_source: str,
                   tc_start: str | None, tc_end: str | None) -> dict:
    """Container tags for a proxy so a downloaded file still says which tape it came from."""
    valid = dt if (dt and dt[:4].isdigit() and 1990 <= int(dt[:4]) <= 2025) else None
    comment = f"MiniDV {tape_id} · scena {index} · TC {tc_start or '?'}-{tc_end or '?'}"
    if dt:
        comment += f" · nagrano {dt.replace('T', ' ')} ({dt_source})"
    return {"title": f"{tape_id} · scena {index:02d}", "comment": comment,
            "creation_time": (valid + ".000000Z") if valid else None,
            "date": valid[:10] if valid else None}


def encode_mp4(source: Path, target: Path, interlaced: bool, log, preset: str = "medium",
               meta: dict | None = None, top_field_first: bool | None = None) -> None:
    cmd = ["ffmpeg", "-y", "-v", "warning", "-i", str(source), "-map", "0:v:0", "-map", "0:a?", "-c:v", "libx264",
           "-preset", preset, "-crf", "16", "-pix_fmt", "yuv420p"]
    if interlaced:
        # DV is always bottom-field-first; pass the probed parity explicitly rather
        # than trusting bwdif's "auto" (a wrong guess = juddery motion).
        parity = "tff" if top_field_first else "bff" if top_field_first is False else "auto"
        cmd += ["-vf", f"bwdif=mode=send_field:parity={parity}:deint=interlaced"]
    cmd += ["-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart"] + _meta_args(meta) + [str(target)]
    run(cmd, log=log)


def restore_mp4(sources: list[Path], target: Path, *, vf: str, crf: int, preset: str,
                decimate: bool = False, meta: dict | None = None, log=None) -> None:
    """Denoise / repair variant — decoded from the DV master(s), filtered, re-encoded.
    `sources` are .dv.zst archive paths (raw DV concatenates, so several = one pass)."""
    global NICE_PREFIX
    NICE_PREFIX = ["nice", "-n", "10"] if shutil.which("nice") else []
    raw = target.with_suffix(".src.dv")
    try:
        with raw.open("wb") as out:
            for z in sources:
                if subprocess.run(["zstd", "-q", "-dc", str(z)], stdout=out).returncode:
                    raise RuntimeError(f"zstd decompression failed: {z}")
        chain = f"mpdecimate,{vf}" if decimate else vf
        cmd = ["ffmpeg", "-y", "-v", "warning", "-f", "dv", "-i", str(raw), "-map", "0:v:0", "-map", "0:a?",
               "-vf", chain]
        if decimate:
            cmd += ["-fps_mode", "vfr", "-af", "aresample=async=1"]
        cmd += ["-c:v", "libx264", "-preset", preset, "-crf", str(crf), "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart"] + _meta_args(meta) + [str(target)]
        run(cmd, log=log)
    finally:
        raw.unlink(missing_ok=True)
        NICE_PREFIX = []


def compress_share(source, target: Path, *, max_mb: int, crf: int, preset: str,
                   meta: dict | None = None, log=None) -> None:
    """Re-encode to ~max_mb, same resolution — for Messenger/FB. `source` is one proxy
    Path, or a list of proxies to concatenate first (in one pass, via the concat demuxer)."""
    global NICE_PREFIX
    NICE_PREFIX = ["nice", "-n", "8"] if shutil.which("nice") else []
    listing = None
    try:
        if isinstance(source, (list, tuple)):
            listing = target.with_suffix(".join.txt")
            listing.write_text("".join(f"file '{Path(p).resolve()}'\n" for p in source))
            in_args = ["-f", "concat", "-safe", "0", "-i", str(listing)]
            probe_in = ["-f", "concat", "-safe", "0", "-i", str(listing)]
        else:
            in_args = probe_in = ["-i", str(source)]
        info = probe_json(["ffprobe", "-v", "quiet", "-select_streams", "v:0",
                           "-show_entries", "format=duration:stream=r_frame_rate", "-of", "json", *probe_in])
        dur = max(1.0, float((info.get("format") or {}).get("duration") or 1))
        vbps = int(min(4_000_000, max(500_000, max_mb * 1024 * 1024 * 8 * 0.93 / dur - 128_000)))
        # the proxy is 50p/60p (bwdif send_field); halve it back to 25p/30p for the share copy.
        num, _, den = ((info.get("streams") or [{}])[0].get("r_frame_rate") or "50/1").partition("/")
        try:
            half_fps = f"{int(num)}/{int(den or 1) * 2}"
        except ValueError:
            half_fps = "25/1"
        run(["ffmpeg", "-y", "-v", "warning", *in_args, "-map", "0:v:0", "-map", "0:a?",
             "-r", half_fps, "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
             "-maxrate", str(vbps), "-bufsize", str(vbps * 2), "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart"] + _meta_args(meta) + [str(target)], log=log)
    finally:
        if listing:
            listing.unlink(missing_ok=True)
        NICE_PREFIX = []


def concat_mp4(sources: list[Path], target: Path, meta: dict | None = None, log=None) -> None:
    """Join several proxies (identical encode params) into one MP4 — stream copy, no re-encode."""
    global NICE_PREFIX
    NICE_PREFIX = ["nice", "-n", "8"] if shutil.which("nice") else []
    listing = target.with_suffix(".concat.txt")
    listing.write_text("".join(f"file '{Path(p).resolve()}'\n" for p in sources))
    try:
        run(["ffmpeg", "-y", "-v", "warning", "-f", "concat", "-safe", "0", "-i", str(listing),
             "-c", "copy", "-movflags", "+faststart"] + _meta_args(meta) + [str(target)], log=log)
    finally:
        listing.unlink(missing_ok=True)
        NICE_PREFIX = []


def process_capture(capture: Path, tape_id: str, storage: Path, zstd_level: int, log, state,
                    capture_drop_lines: list[str] | None = None, nice: bool = False,
                    mp4_preset: str = "medium") -> dict:
    global NICE_PREFIX
    NICE_PREFIX = (["nice", "-n", "12", "ionice", "-c", "3"]
                   if nice and shutil.which("nice") and shutil.which("ionice") else [])
    try:
        return _process_capture(capture, tape_id, storage, zstd_level, log, state, capture_drop_lines or [], mp4_preset)
    finally:
        NICE_PREFIX = []


def _process_capture(capture: Path, tape_id: str, storage: Path, zstd_level: int, log, state,
                     capture_drop_lines: list[str], mp4_preset: str) -> dict:
    work = capture.parent
    split_dir = work / "split"
    tape_dir = storage / "tapes" / tape_id
    if tape_dir.exists():
        raise RuntimeError(f"tape already exists: {tape_id}")
    tape_dir.mkdir(parents=True)
    (tape_dir / "thumbnails").mkdir()
    state("DETECTING_SCENES")
    scenes, discontinuities = split_capture(capture, split_dir, log)
    results = []
    checksum_lines = []
    for index, scene in enumerate(scenes, 1):
        dt, dt_source = recording_datetime(scene, storage / "tmp")
        stamp = dt.replace("T", "_").replace(":", "-") if dt else "UNKNOWN-DATE"
        scene_id = f"{index:04d}_{stamp}"
        archive = tape_dir / f"{scene_id}.dv.zst"
        proxy = tape_dir / f"{scene_id}.mp4"
        meta_path = tape_dir / f"{scene_id}.json"
        probe = ffprobe(scene)
        video = next(s for s in probe["streams"] if s.get("codec_type") == "video")
        audios = [s for s in probe["streams"] if s.get("codec_type") == "audio"]
        interlaced, top_first = first_frame_interlaced(scene)
        source_hash = sha256(scene)
        state("COMPRESSING", scene_id)
        run(["zstd", "-q", f"-{zstd_level}", "-T0", str(scene), "-o", str(archive)], log=log)
        state("VERIFYING_ARCHIVES", scene_id)
        run(["zstd", "-q", "-t", str(archive)], log=log)
        restored_hash = sha256_zstd_stream(archive)
        if source_hash != restored_hash:
            raise RuntimeError(f"byte verification failed for {scene_id}; original retained")
        frame_size = 144000 if video.get("height") == 576 else 120000
        frame_count = scene.stat().st_size // frame_size
        timecode_start = probe.get("format", {}).get("tags", {}).get("timecode")
        timecode_end = last_frame_timecode(scene, frame_size, storage / "tmp")
        state("ENCODING_MP4", scene_id)
        encode_mp4(scene, proxy, interlaced, log, mp4_preset,
                   scene_metadata(tape_id, index, dt, dt_source, timecode_start, timecode_end),
                   top_field_first=top_first)
        state("VERIFYING_MP4", scene_id)
        proxy_probe = ffprobe(proxy)
        if not proxy_probe.get("streams"):
            raise RuntimeError(f"invalid MP4: {proxy}")
        state("PROBING_QUALITY", scene_id)
        decode_errors, frame_hashes = scene_probe_quality(scene, frame_count)
        error_score = len(capture_drop_lines) + len(discontinuities) + decode_errors
        meta = {
            "schema_version": 1, "scene_index": index, "scene_id": scene_id, "tape_id": tape_id,
            "recording": {"datetime": dt, "datetime_source": dt_source, "datetime_valid": bool(dt)},
            "timecode": {"start": timecode_start, "end": timecode_end},
            "video": {"format": "DV", "standard": "PAL" if video.get("height") == 576 else "NTSC",
                      "resolution": f"{video.get('width')}x{video.get('height')}", "frame_rate": video.get("r_frame_rate"),
                      "sample_aspect_ratio": video.get("sample_aspect_ratio"), "display_aspect_ratio": video.get("display_aspect_ratio"),
                      "pixel_format": video.get("pix_fmt"), "interlaced": interlaced, "top_field_first": top_first},
            "audio": [{"codec": a.get("codec_name"), "sample_rate": a.get("sample_rate"), "channels": a.get("channels")} for a in audios],
            "capture": {"dropped_frames": len(capture_drop_lines), "errors": capture_drop_lines,
                        "source_discontinuities": discontinuities,
                        "decode_errors": decode_errors, "error_score": error_score},
            "fingerprint": {"datetime": dt, "tc_start": timecode_start, "tc_end": timecode_end,
                            "frame_count": frame_count, "frame_hashes": frame_hashes},
            "source": {"start_frame": sum(r["frame_count"] for r in results), "end_frame": sum(r["frame_count"] for r in results) + frame_count - 1},
            "files": {"archive": {"filename": archive.name, "sha256_uncompressed": source_hash,
                       "sha256_compressed": sha256(archive), "size_uncompressed": scene.stat().st_size,
                       "size_compressed": archive.stat().st_size, "zstd_level": zstd_level, "verified_byte_for_byte": True},
                      "proxy": {"filename": proxy.name, "sha256": sha256(proxy), "size": proxy.stat().st_size,
                                "deinterlace": "bwdif send_field" if interlaced else "none"}},
            "frame_count": frame_count,
        }
        meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False) + "\n")
        # Thumbnail is a convenience, not part of archival integrity: seek to the scene
        # midpoint (capped at 1 s so sub-second manual-STOP tail scenes still yield a frame)
        # and never fail the tape over it.
        thumb_ss = min(1.0, frame_count / (25 if video.get("height") == 576 else 30) / 2)
        try:
            run(["ffmpeg", "-y", "-v", "error", "-ss", f"{thumb_ss:.2f}", "-i", str(proxy), "-frames:v", "1",
                 "-update", "1", "-pix_fmt", "yuvj420p",
                 str(tape_dir / "thumbnails" / f"{scene_id}.jpg")], log=log)
        except RuntimeError as exc:
            log(f"thumbnail skipped for {scene_id}: {exc}")
        checksum_lines += [f"{meta['files']['archive']['sha256_compressed']}  {archive.name}",
                           f"{meta['files']['proxy']['sha256']}  {proxy.name}", f"{sha256(meta_path)}  {meta_path.name}"]
        results.append({"scene_index": index, "scene_id": scene_id, "frame_count": frame_count})
        scene.unlink()
    tape = {"schema_version": 1, "tape_id": tape_id, "scene_count": len(results), "scenes": results,
            "source_discontinuities": discontinuities}
    proxies = sorted(tape_dir.glob("[0-9]*.mp4"))
    if proxies:
        state("BUILDING_TAPE_PROXY")
        listing = storage / "tmp" / f"concat-{tape_id}.txt"
        listing.write_text("".join(f"file '{p.resolve()}'\n" for p in proxies))
        try:
            # stream copy of the per-scene proxies (identical encode params) -> one
            # continuous review file the player can scrub end to end.
            first_dt = next((r["scene_id"][5:15].replace("_", "-") for r in results
                             if r["scene_id"][5:9].isdigit() and 1990 <= int(r["scene_id"][5:9]) <= 2025), None)
            run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy",
                 "-movflags", "+faststart"]
                + _meta_args({"title": f"{tape_id} — cała taśma", "date": first_dt,
                              "comment": f"MiniDV {tape_id} · {len(results)} scen · sklejony podgląd"})
                + [str(tape_dir / "tape.mp4")], log=log)
            tape["proxy_full"] = {"filename": "tape.mp4", "size": (tape_dir / "tape.mp4").stat().st_size}
        except RuntimeError as exc:
            log(f"tape.mp4 concat skipped: {exc}")
        listing.unlink(missing_ok=True)
    (tape_dir / "tape.json").write_text(json.dumps(tape, indent=2) + "\n")
    checksum_lines.append(f"{sha256(tape_dir / 'tape.json')}  tape.json")
    (tape_dir / "tape.sha256").write_text("\n".join(checksum_lines) + "\n")
    shutil.copy2(work / "capture.log", tape_dir / "capture.log") if (work / "capture.log").exists() else None
    return tape
