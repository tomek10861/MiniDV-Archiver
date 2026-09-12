from __future__ import annotations

import json
import mimetypes
import select
import socket
import subprocess
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from .config import Config
from .engine import Engine

CONFIG = Config()
ENGINE: Engine | None = None            # built lazily by build_engine() — importing this module has no side effects
STATIC = Path(__file__).resolve().parent.parent / "frontend"


def build_engine(role: str = "all") -> Engine:
    """Construct the process-wide Engine in the given role (idempotent)."""
    global ENGINE
    if ENGINE is None:
        ENGINE = Engine(CONFIG, role=role)
    return ENGINE


EXTRA_TYPES = {".dv": "video/x-dv", ".zst": "application/zstd", ".sha256": "text/plain",
               ".log": "text/plain; charset=utf-8", ".srt": "text/plain; charset=utf-8"}
INLINE_SUFFIXES = {".mp4", ".jpg", ".jpeg", ".png", ".webp"}


def content_type(name: str) -> str:
    for suffix, ctype in EXTRA_TYPES.items():
        if name.endswith(suffix):
            return ctype
    return mimetypes.guess_type(name)[0] or "application/octet-stream"


def archive_file(tape: str, rel: str) -> Path | None:
    """Resolve <tapes>/<tape>/<rel> for download, or None if it escapes or is missing."""
    if not tape or tape in (".", "..") or "/" in tape or "\\" in tape:
        return None
    if not rel or rel.startswith("/") or ".." in rel.split("/"):
        return None
    root = CONFIG.tapes.resolve()
    base = (root / tape).resolve()
    if base.parent != root or not base.is_dir():
        return None
    target = (base / rel).resolve()
    try:
        target.relative_to(base)
    except ValueError:
        return None
    return target if target.is_file() else None


class Handler(BaseHTTPRequestHandler):
    server_version = "MiniDVArchive/0.1"

    def json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def body(self):
        length = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(length) or b"{}")

    def do_GET(self):
        path = unquote(urlparse(self.path).path)
        try:
            if path == "/api/capture/preview.mjpg":
                return self.stream_preview()
            if path == "/api/status":
                info = ENGINE.camera.info()  # sysfs only, never sends AV/C
                camera = {**info, "transport": "CONNECTED" if info["connected"] else "NO_CAMERA", "timecode": None,
                          "avc_enabled": CONFIG.allow_fcp, "mode": "auto" if CONFIG.allow_fcp else "manual"}
                return self.json({"camera": camera, "job": ENGINE.current_job(), **ENGINE.status()})
            if path == "/api/storage":
                return self.json(ENGINE.storage())
            if path == "/api/jobs":
                return self.json(ENGINE.jobs_list())
            if path.startswith("/api/jobs/"):
                job = ENGINE.job_by_ref(path.rsplit("/", 1)[-1])
                return self.json(job or {"error": "not found"}, 200 if job else 404)
            if path == "/api/tapes":
                return self.json(ENGINE.tapes())
            if path == "/api/timeline":
                return self.json(ENGINE.timeline())
            if path == "/api/duplicates":
                return self.json(ENGINE.duplicates())
            parts = path.strip("/").split("/")
            if len(parts) == 4 and parts[:2] == ["api", "timeline"] and parts[2].isdigit() and parts[3].isdigit():
                return self.json(ENGINE.timeline_month(int(parts[2]), int(parts[3])))
            if len(parts) >= 5 and parts[:2] == ["api", "tapes"] and parts[3] == "files":
                target = archive_file(parts[2], "/".join(parts[4:]))
                if not target:
                    return self.json({"error": "not found"}, 404)
                forced = urlparse(self.path).query == "dl=1" or "&dl=1" in self.path or "?dl=1" in self.path
                return self.send_file(target, inline=target.suffix.lower() in INLINE_SUFFIXES and not forced)
            if len(parts) == 4 and parts[:3] == ["api", "playlist", "build"]:
                token = parts[3].removesuffix(".mp4")
                job = ENGINE.playlist_status(token)
                disk = CONFIG.storage / "tmp" / "share" / f"{ENGINE.PLAYLIST_BUCKET}_{token}.mp4"
                if job.get("status") == "READY" and job.get("path") and Path(job["path"]).exists():
                    return self.send_file(Path(job["path"]), inline=False)
                if job.get("status") != "RUNNING" and job.get("status") != "QUEUED" and disk.exists():
                    return self.send_file(disk, inline=False)
                return self.json({"error": "nagranie jeszcze się przygotowuje "
                                  "(albo wygasło — zbuduj ponownie)", "state": job.get("status")}, 409)
            if len(parts) >= 5 and parts[:2] == ["api", "tapes"] and parts[3] == "compressed":
                token = parts[4].removesuffix(".mp4")
                if token in ("TAPE", "tape"):
                    token = "TAPE"
                job = ENGINE.compress_status(parts[2], token)
                disk = CONFIG.storage / "tmp" / "share" / f"{parts[2]}_{token}.mp4"
                if job.get("status") == "READY" and job.get("path") and Path(job["path"]).exists():
                    return self.send_file(Path(job["path"]), inline=False)
                if job.get("status") != "RUNNING" and job.get("status") != "QUEUED" and disk.exists():
                    return self.send_file(disk, inline=False)  # made by an earlier session
                return self.json({"error": "podgląd do udostępnienia jeszcze się przygotowuje "
                                  "(albo wygasł — kliknij FB ponownie)", "state": job.get("status")}, 409)
            if len(parts) >= 3 and parts[:2] == ["api", "tapes"]:
                tape = CONFIG.tapes / parts[2]
                if tape.parent != CONFIG.tapes or not tape.is_dir():
                    return self.json({"error": "not found"}, 404)
                if len(parts) == 3:
                    return self.json(json.loads((tape / "tape.json").read_text()))
                if len(parts) == 4 and parts[3] == "scenes":
                    return self.json([json.loads(p.read_text()) for p in sorted(tape.glob("[0-9]*.json"))])
                if len(parts) == 5 and parts[3] == "scenes":
                    target = tape / f"{parts[4]}.json"
                    return self.json(json.loads(target.read_text()))
            return self.static(path)
        except Exception as exc:
            return self.json({"error": str(exc)}, 500)

    def do_HEAD(self):
        path = unquote(urlparse(self.path).path)
        parts = path.strip("/").split("/")
        if len(parts) >= 5 and parts[:2] == ["api", "tapes"] and parts[3] == "files":
            target = archive_file(parts[2], "/".join(parts[4:]))
            if not target:
                self.send_error(404)
                return
            return self.send_file(target, inline=target.suffix.lower() in INLINE_SUFFIXES)
        target = STATIC / (path.lstrip("/") or "index.html")
        try:
            target = target.resolve()
            target.relative_to(STATIC.resolve())
            size = target.stat().st_size
        except (OSError, ValueError):
            self.send_error(404)
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type(target.name))
        self.send_header("Content-Length", str(size))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            if path in {"/api/tape/play", "/api/tape/stop", "/api/tape/rewind"}:
                if not CONFIG.allow_fcp:
                    return self.json({"error": "Sterowanie AV/C wyłączone w trybie zgodności DCR-PC2E "
                                     "(ustaw MINIDV_ALLOW_FCP=1, aby włączyć)"}, 409)
                name = path.rsplit("/", 1)[-1]
                if ENGINE.role == "all":                       # camera lives in this process
                    return self.json(ENGINE.camera.command(name))
                cid = ENGINE.store.enqueue_cam_command(name)   # split mode: only the grabber touches the bus
                res = ENGINE.store.wait_cam_command(cid, timeout=6)
                if res["status"] == "DONE":
                    return self.json(res["result"])
                return self.json({"error": f"kamera nie odpowiedziała ({res['status'].lower()})"}, 409)
            if path == "/api/capture/start":
                data = self.body()
                return self.json(ENGINE.start(data.get("tape_id"), data.get("rewind", True), data.get("duration"),
                                              data.get("manual_transport", False)), 202)
            if path == "/api/capture/stop":
                return self.json(ENGINE.stop(self.body().get("tape_id")))
            if path == "/api/playlist/build":
                data = self.body()
                items = data.get("items")
                if not isinstance(items, list) or not items:
                    return self.json({"error": "pusta lista scen"}, 400)
                return self.json(ENGINE.start_playlist(items, data.get("title")), 202)
            cparts = path.strip("/").split("/")
            if len(cparts) >= 4 and cparts[:2] == ["api", "tapes"] and cparts[-1] == "compress":
                data = self.body()
                restore = bool(data.get("restore"))
                if len(cparts) == 6 and cparts[3] == "scenes":
                    return self.json(ENGINE.start_compress(cparts[2], cparts[4], restore=restore), 202)
                scenes = data.get("scenes")
                if isinstance(scenes, list) and scenes:
                    return self.json(ENGINE.start_selection(cparts[2], scenes, bool(data.get("share")),
                                                            restore=restore), 202)
                return self.json(ENGINE.start_compress(cparts[2], None, restore=restore), 202)
            if len(cparts) == 4 and cparts[:2] == ["api", "tapes"] and cparts[3] == "reprobe":
                return self.json(ENGINE.start_reprobe(cparts[2], bool(self.body().get("force"))), 202)
            if len(cparts) == 5 and cparts[:2] == ["api", "tapes"] and cparts[3:5] == ["scenes", "delete"]:
                scenes = self.body().get("scenes")
                if not isinstance(scenes, list) or not scenes:
                    return self.json({"error": "brak listy scen"}, 400)
                return self.json(ENGINE.start_delete_scenes(cparts[2], scenes), 202)
            if len(cparts) == 4 and cparts[:2] == ["api", "tapes"] and cparts[3] == "rename":
                return self.json(ENGINE.rename_tape(cparts[2], self.body().get("new_id", "")))
            if len(cparts) == 4 and cparts[:2] == ["api", "tapes"] and cparts[3] == "meta":
                b = self.body()
                return self.json(ENGINE.set_tape_meta(cparts[2], label=b.get("label"),
                                                      recording_date=b.get("recording_date")))
            return self.json({"error": "not found"}, 404)
        except (ValueError, FileExistsError, FileNotFoundError, RuntimeError) as exc:
            return self.json({"error": str(exc)}, 409)

    def do_DELETE(self):
        parts = unquote(urlparse(self.path).path).strip("/").split("/")
        try:
            if parts[:2] == ["api", "tapes"] and len(parts) == 3:
                return self.json(ENGINE.delete_tape(parts[2]))
            if parts[:2] == ["api", "jobs"] and len(parts) == 3:
                return self.json(ENGINE.delete_job(parts[2]))
            return self.json({"error": "not found"}, 404)
        except (ValueError, FileNotFoundError, RuntimeError) as exc:
            return self.json({"error": str(exc)}, 409)
        except Exception as exc:
            return self.json({"error": str(exc)}, 500)

    def static(self, path):
        target = STATIC / (path.lstrip("/") or "index.html")
        try:
            target = target.resolve()
            target.relative_to(STATIC.resolve())
            data = target.read_bytes()
        except (OSError, ValueError):
            return self.json({"error": "not found"}, 404)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type(target.name))
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_file(self, target: Path, inline: bool = True):
        """Serve a file with single-range support so proxy MP4s scrub in <video>."""
        size = target.stat().st_size
        start, end, status = 0, size - 1, HTTPStatus.OK
        rng = self.headers.get("Range", "")
        if rng.startswith("bytes="):
            first, _, last = rng[6:].partition("-")
            try:
                start = int(first) if first else (size - int(last))
                end = int(last) if last and first else (size - 1)
                end = min(end, size - 1)
                if start < 0 or start > end:
                    raise ValueError
            except ValueError:
                self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            status = HTTPStatus.PARTIAL_CONTENT
        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", content_type(target.name))
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Disposition",
                         f'{"inline" if inline else "attachment"}; filename="{target.name}"')
        if status == HTTPStatus.PARTIAL_CONTENT:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if self.command == "HEAD":
            return
        with target.open("rb") as handle:
            handle.seek(start)
            remaining = length
            while remaining > 0:
                chunk = handle.read(min(262144, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def _client_gone(self) -> bool:
        """Non-blocking check for a client that closed its end (tab closed / navigated
        away). Needed because our read loop otherwise blocks on ffmpeg's stdout, which
        can sit idle for a long time (capture stalled/ended) with no data to notice a
        dead client by — that would leak the ffmpeg/tail pair until the capture ends."""
        try:
            r, _, _ = select.select([self.connection], [], [], 0)
            return bool(r) and self.connection.recv(1, socket.MSG_PEEK) == b""
        except OSError:
            return True

    def stream_preview(self):
        """Live MJPEG preview of the capture in progress, if any (read-only tap on the
        raw DV file — see Engine.preview_procs; never touches the FireWire capture)."""
        procs = ENGINE.preview_procs()
        if not procs:
            return self.json({"error": "brak aktywnego zgrywania"}, 404)
        tail, ff = procs
        try:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "multipart/x-mixed-replace;boundary=frame")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            while True:
                # capture ended -> ffmpeg/tail may now sit idle forever; stop rather than leak
                if not ENGINE.store.capture_job() or self._client_gone():
                    break
                ready, _, _ = select.select([ff.stdout], [], [], 2.0)
                if not ready:
                    continue
                chunk = ff.stdout.read(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            for p in (ff, tail):
                if p.poll() is None:
                    p.kill()
            for p in (ff, tail):
                try:
                    p.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass

    def log_message(self, fmt, *args):
        print(f"{self.address_string()} {fmt % args}")


def run(role: str = "all"):
    build_engine(role)
    label = "api" if role == "api" else f"{role} + api"
    print(f"MiniDV {label} listening on http://{CONFIG.bind}:{CONFIG.port}")
    ThreadingHTTPServer((CONFIG.bind, CONFIG.port), Handler).serve_forever()


def main():
    run("all")


if __name__ == "__main__":
    main()
