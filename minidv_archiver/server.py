from __future__ import annotations

import json
import mimetypes
import select
import socket
import subprocess
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

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
        query = parse_qs(urlparse(self.path).query)
        workspace = (query.get("workspace") or [None])[0]
        source = (query.get("sources") or query.get("source") or ["all"])[0]
        try:
            if path == "/api/capture/preview.mjpg":
                return self.stream_preview()
            if path == "/api/status":
                info = ENGINE.camera.info()  # sysfs only, never sends AV/C
                camera = {**info, "transport": "CONNECTED" if info["connected"] else "NO_CAMERA", "timecode": None,
                          "avc_enabled": CONFIG.allow_fcp, "mode": "auto" if CONFIG.allow_fcp else "manual"}
                return self.json({"camera": camera, "job": ENGINE.current_job(),
                                  "default_recording_tz": CONFIG.default_recording_tz, **ENGINE.status()})
            if path == "/api/storage":
                return self.json(ENGINE.storage())
            if path == "/api/sysload":
                from . import sysload
                return self.json(sysload.status())
            if path == "/api/jobs":
                return self.json(ENGINE.jobs_list())
            if path.startswith("/api/jobs/"):
                job = ENGINE.job_by_ref(path.rsplit("/", 1)[-1])
                return self.json(job or {"error": "not found"}, 200 if job else 404)
            if path == "/api/workspaces":
                return self.json(ENGINE.list_workspaces())
            if path == "/api/tapes":
                return self.json(ENGINE.tapes(workspace))
            if path == "/api/timeline":
                return self.json(ENGINE.timeline(workspace, source))
            if path == "/api/duplicates":
                return self.json(ENGINE.duplicates(workspace))
            if path == "/api/faces/status":
                return self.json(ENGINE.faces_status(workspace))
            if path == "/api/persons":
                return self.json(ENGINE.persons(workspace))
            if path == "/api/media/status":
                return self.json(ENGINE.media_status())
            if path == "/api/media/albums":
                return self.json(ENGINE.media_albums(workspace))
            if path == "/api/media/album":
                return self.json(ENGINE.media_album(workspace, (query.get("name") or [""])[0]))
            mparts = path.strip("/").split("/")
            if len(mparts) >= 3 and mparts[:2] == ["api", "media"] and mparts[2].isdigit():
                return self.media_file(int(mparts[2]), mparts[3] if len(mparts) == 4 else "")
            if path == "/api/faces/suggestions":
                return self.json(ENGINE.face_suggestions(workspace))
            if path == "/api/music":
                from . import memories as mem
                return self.json({"moods": list(mem.MOODS),
                                  "tracks": [{k: v for k, v in t.items() if not k.startswith("_") and k != "url"}
                                             for t in mem.catalogue(CONFIG)]})
            if path == "/api/music/search":
                from . import memories as mem
                q = (query.get("q") or [""])[0].strip()
                return self.json({"tracks": [{k: v for k, v in t.items() if k != "url"} for t in mem.search(q)]
                                  if q else []})
            if len(mparts) == 4 and mparts[:2] == ["api", "music"] and mparts[3] == "audio":
                from . import memories as mem
                _, local = mem.ensure_track(CONFIG, mparts[2])
                return self.send_file(local)
            if len(mparts) == 4 and mparts[:2] == ["api", "persons"] and mparts[2].isdigit() and mparts[3] == "memories":
                return self.json(ENGINE.memories_of(int(mparts[2])))
            if len(mparts) == 4 and mparts[:2] == ["api", "memories"] and mparts[2].isdigit() and \
                    mparts[3] in ("file", "poster"):
                target = ENGINE.memory_file(int(mparts[2]), poster=mparts[3] == "poster")
                if not target:
                    return self.json({"error": "not found"}, 404)
                return self.send_file(target, inline="dl" not in query)
            parts = path.strip("/").split("/")
            if len(parts) == 3 and parts[:2] == ["api", "persons"] and parts[2].isdigit():
                try:
                    return self.json(ENGINE.person(int(parts[2])))
                except FileNotFoundError as exc:      # e.g. just merged into someone else
                    return self.json({"error": str(exc)}, 404)
            if (len(parts) == 4 and parts[:3] == ["api", "faces", "thumb"]
                    and parts[3].endswith(".jpg") and parts[3][:-4].isdigit()):
                from .faces import thumb_path
                target = thumb_path(CONFIG, int(parts[3][:-4]))
                return self.send_file(target) if target.is_file() else self.json({"error": "not found"}, 404)
            if len(parts) == 4 and parts[:2] == ["api", "timeline"] and parts[2].isdigit() and parts[3].isdigit():
                return self.json(ENGINE.timeline_month(int(parts[2]), int(parts[3]), workspace, source))
            if len(parts) >= 5 and parts[:2] == ["api", "tapes"] and parts[3] == "files":
                target = archive_file(parts[2], "/".join(parts[4:]))
                if not target:
                    return self.json({"error": "not found"}, 404)
                forced = urlparse(self.path).query == "dl=1" or "&dl=1" in self.path or "?dl=1" in self.path
                return self.send_file(target, inline=target.suffix.lower() in INLINE_SUFFIXES and not forced)
            if len(parts) == 4 and parts[:3] == ["api", "playlist", "build"]:
                name = parts[3]
                ext = ".zip" if name.endswith(".zip") else ".mp4"
                token = name.removesuffix(ext)
                job = ENGINE.playlist_status(token)
                disk = CONFIG.storage / "tmp" / "share" / f"{ENGINE.PLAYLIST_BUCKET}_{token}{ext}"
                if job.get("status") == "READY" and job.get("path") and Path(job["path"]).exists():
                    return self.send_file(Path(job["path"]), inline=False)
                if job.get("status") != "RUNNING" and job.get("status") != "QUEUED" and disk.exists():
                    return self.send_file(disk, inline=False)
                return self.json({"error": "nagranie jeszcze się przygotowuje "
                                  "(albo wygasło — zbuduj ponownie)", "state": job.get("status")}, 409)
            if len(parts) >= 5 and parts[:2] == ["api", "tapes"] and parts[3] == "compressed":
                name = parts[4]
                ext = ".zip" if name.endswith(".zip") else ".mp4"
                token = name.removesuffix(ext)
                if token in ("TAPE", "tape"):
                    token = "TAPE"
                job = ENGINE.compress_status(parts[2], token)
                disk = CONFIG.storage / "tmp" / "share" / f"{parts[2]}_{token}{ext}"
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
                                              data.get("manual_transport", False), data.get("timezone"),
                                              data.get("workspace")), 202)
            if path == "/api/capture/stop":
                return self.json(ENGINE.stop(self.body().get("tape_id")))
            if path == "/api/workspaces":
                return self.json(ENGINE.create_workspace(self.body().get("name", "")), 201)
            if path == "/api/playlist/build":
                data = self.body()
                items = data.get("items")
                if not isinstance(items, list) or not items:
                    return self.json({"error": "pusta lista scen"}, 400)
                return self.json(ENGINE.start_playlist(items, data.get("title"), restore=bool(data.get("restore")),
                                                        stabilize=bool(data.get("stabilize")),
                                                        upscale=bool(data.get("upscale")),
                                                        archive=bool(data.get("archive"))), 202)
            if path == "/api/duplicates/dismiss":
                members = self.body().get("members")
                if not isinstance(members, list) or len(members) < 2:
                    return self.json({"error": "potrzeba co najmniej 2 scen"}, 400)
                return self.json(ENGINE.dismiss_duplicate_group(members))
            if path in ("/api/persons/dissolve", "/api/persons/delete"):
                ids = self.body().get("ids")
                if not isinstance(ids, list) or not ids or not all(isinstance(i, int) for i in ids):
                    return self.json({"error": "brak listy osób"}, 400)
                return self.json(ENGINE.dissolve_persons(ids) if path.endswith("dissolve") else ENGINE.delete_persons(ids))
            if path == "/api/media/album/date":
                b = self.body()
                ws = (parse_qs(urlparse(self.path).query).get("workspace") or [None])[0]
                return self.json(ENGINE.media_set_album_date(ws, str(b.get("album") or ""), b.get("date")))
            if path == "/api/media/rescan":
                return self.json(ENGINE.media_rescan(), 202)
            mp = path.strip("/").split("/")
            if len(mp) == 4 and mp[:2] == ["api", "media"] and mp[2].isdigit() and mp[3] == "date":
                b = self.body()
                return self.json(ENGINE.media_set_date(int(mp[2]), b.get("date"), whole=bool(b.get("whole"))))
            if len(mp) == 4 and mp[:2] == ["api", "persons"] and mp[2].isdigit() and mp[3] == "memories":
                return self.json(ENGINE.create_memory(int(mp[2]), self.body()), 202)
            if len(mp) == 4 and mp[:2] == ["api", "memories"] and mp[2].isdigit() and mp[3] == "music":
                return self.json(ENGINE.memory_music(int(mp[2]), self.body().get("music")), 202)
            if len(mp) == 4 and mp[:2] == ["api", "media"] and mp[2].isdigit() and mp[3] == "reencode":
                return self.json(ENGINE.media_request_reproxy(int(mp[2])), 202)
            if path == "/api/faces/scan":
                return self.json(ENGINE.start_face_scan(), 202)
            if path == "/api/faces/scan/cancel":
                return self.json(ENGINE.cancel_face_scan())
            cparts = path.strip("/").split("/")
            if len(cparts) == 3 and cparts[:2] == ["api", "persons"] and cparts[2].isdigit():
                b = self.body()
                return self.json(ENGINE.update_person(int(cparts[2]), name=b.get("name"), hidden=b.get("hidden"),
                                                      group=b.get("group")))
            if len(cparts) == 4 and cparts[:2] == ["api", "persons"] and cparts[2].isdigit() and cparts[3] == "reject":
                other = self.body().get("other")
                if not isinstance(other, int):
                    return self.json({"error": "brak osoby"}, 400)
                return self.json(ENGINE.reject_suggestion(int(cparts[2]), other))
            if len(cparts) == 4 and cparts[:2] == ["api", "persons"] and cparts[2].isdigit() and cparts[3] == "merge":
                into = self.body().get("into")
                if not isinstance(into, int):
                    return self.json({"error": "brak osoby docelowej"}, 400)
                return self.json(ENGINE.merge_persons(int(cparts[2]), into))
            if len(cparts) == 5 and cparts[:3] == ["api", "faces", "tracks"] and cparts[3].isdigit() and cparts[4] == "move":
                pid = self.body().get("person_id")
                if pid is not None and not isinstance(pid, int):
                    return self.json({"error": "zła osoba"}, 400)
                return self.json(ENGINE.move_face(int(cparts[3]), pid))
            if len(cparts) >= 4 and cparts[:2] == ["api", "tapes"] and cparts[-1] == "compress":
                data = self.body()
                restore = bool(data.get("restore"))
                # only meaningful together with restore=True (repair from the DV master);
                # start_compress/start_selection ignore them otherwise
                stabilize, upscale = bool(data.get("stabilize")), bool(data.get("upscale"))
                if len(cparts) == 6 and cparts[3] == "scenes":
                    return self.json(ENGINE.start_compress(cparts[2], cparts[4], restore=restore,
                                                            stabilize=stabilize, upscale=upscale), 202)
                scenes = data.get("scenes")
                if isinstance(scenes, list) and scenes:
                    return self.json(ENGINE.start_selection(cparts[2], scenes, bool(data.get("share")),
                                                            restore=restore, stabilize=stabilize, upscale=upscale,
                                                            archive=bool(data.get("archive"))), 202)
                return self.json(ENGINE.start_compress(cparts[2], None, restore=restore,
                                                        stabilize=stabilize, upscale=upscale), 202)
            if len(cparts) == 4 and cparts[:2] == ["api", "tapes"] and cparts[3] == "reprobe":
                return self.json(ENGINE.start_reprobe(cparts[2], bool(self.body().get("force"))), 202)
            if len(cparts) == 4 and cparts[:2] == ["api", "tapes"] and cparts[3] == "backfill-meta":
                return self.json(ENGINE.start_metadata_backfill(cparts[2], bool(self.body().get("force"))), 202)
            if len(cparts) == 5 and cparts[:2] == ["api", "tapes"] and cparts[3:5] == ["scenes", "delete"]:
                scenes = self.body().get("scenes")
                if not isinstance(scenes, list) or not scenes:
                    return self.json({"error": "brak listy scen"}, 400)
                return self.json(ENGINE.start_delete_scenes(cparts[2], scenes), 202)
            if len(cparts) == 4 and cparts[:2] == ["api", "tapes"] and cparts[3] == "rename":
                return self.json(ENGINE.rename_tape(cparts[2], self.body().get("new_id", "")))
            if len(cparts) == 4 and cparts[:2] == ["api", "tapes"] and cparts[3] == "scene-dates":
                dates = self.body().get("dates")
                if not isinstance(dates, dict) or not dates:
                    return self.json({"error": "brak dat scen"}, 400)
                return self.json(ENGINE.set_scene_dates(cparts[2], dates))
            if len(cparts) == 4 and cparts[:2] == ["api", "tapes"] and cparts[3] == "meta":
                b = self.body()
                return self.json(ENGINE.set_tape_meta(cparts[2], label=b.get("label"),
                                                      recording_date=b.get("recording_date"),
                                                      undated_date=b.get("undated_date")))
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
            if parts[:2] == ["api", "memories"] and len(parts) == 3 and parts[2].isdigit():
                return self.json(ENGINE.delete_memory(int(parts[2])))
            return self.json({"error": "not found"}, 404)
        except (ValueError, FileNotFoundError, RuntimeError) as exc:
            return self.json({"error": str(exc)}, 409)
        except Exception as exc:
            return self.json({"error": str(exc)}, 500)

    def media_file(self, media_id: int, what: str):
        """thumb.jpg | view (photo, browser-viewable) | video (playable) | original | "" (json)."""
        from . import media_library as ml
        try:
            row = ENGINE.media_item(media_id)
        except FileNotFoundError:
            return self.json({"error": "not found"}, 404)
        if not what:
            return self.json(ml.public(row))
        if what == "thumb.jpg":
            target = ml.derived(CONFIG, "thumbs", media_id, ".jpg")
        elif what == "view":
            disp = ml.derived(CONFIG, "display", media_id, ".jpg")
            target = disp if disp.exists() else Path(row["path"])
        elif what == "video":
            target = ml.video_path(CONFIG, row)
        elif what == "original":
            target = Path(row["path"])
            if ml.is_dvd(row):                          # a DVD title is several VOBs: hand out its MP4
                target = ml.video_path(CONFIG, row)
        else:
            return self.json({"error": "not found"}, 404)
        # originals are only ever served from inside the configured media folders
        inside = lambda p: any(str(p.resolve()).startswith(str(r.resolve()) + "/") for r in ml.roots(CONFIG))  # noqa: E731
        if target is None or not target.is_file() or (
                target.is_relative_to(CONFIG.storage) is False and not inside(target)):
            return self.json({"error": "not found"}, 404)
        return self.send_file(target, inline=what != "original" or "dl=1" not in self.path)

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
