"""SQLite-backed job store — the only mutable state shared between the grabber,
converter and api processes.

Design goals:
  * faithful round-trip of the job / build dicts the rest of the code already uses
    (the full dict lives in a JSON ``payload`` column; a few fields are lifted out
    for querying),
  * atomic hand-off — ``claim_next_*`` moves a row out of its queue in a single
    ``BEGIN IMMEDIATE`` transaction, so two processes never take the same job,
  * WAL mode + ``busy_timeout`` for cross-process concurrency; an internal lock
    serialises the connection across threads inside one process.

Nothing here touches the archive under ``tapes/`` — that stays plain files.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

TERMINAL = {"COMPLETED", "ERROR", "CANCELLED"}
# stage=process rows waiting for the converter
_QUEUEABLE = ("QUEUED", "CAPTURED")
MAX_JOBS = 30

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    tape_id    TEXT PRIMARY KEY,
    status     TEXT NOT NULL,
    stage      TEXT,
    cancel     INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT,
    payload    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS builds (
    key        TEXT PRIMARY KEY,
    tape_id    TEXT,
    status     TEXT NOT NULL,
    updated_at TEXT,
    payload    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cam_commands (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'QUEUED',
    result     TEXT,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS library (
    tape_id    TEXT PRIMARY KEY,
    source_sig TEXT,
    indexed_at TEXT,
    payload    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS dup_dismissed (
    scene_a      TEXT NOT NULL,
    scene_b      TEXT NOT NULL,
    dismissed_at TEXT,
    PRIMARY KEY (scene_a, scene_b)
);
CREATE TABLE IF NOT EXISTS workspaces (
    name       TEXT PRIMARY KEY,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS face_persons (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace  TEXT NOT NULL,
    name       TEXT,
    hidden     INTEGER NOT NULL DEFAULT 0,
    centroid   BLOB,
    n          INTEGER NOT NULL DEFAULT 0,
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS face_persons_ws ON face_persons(workspace);
CREATE TABLE IF NOT EXISTS face_tracks (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id  INTEGER NOT NULL,
    workspace  TEXT NOT NULL,
    tape_id    TEXT NOT NULL,
    scene_id   TEXT NOT NULL,
    embedding  BLOB NOT NULL,
    quality    REAL NOT NULL DEFAULT 0,
    times      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS face_tracks_person ON face_tracks(person_id);
CREATE INDEX IF NOT EXISTS face_tracks_scene ON face_tracks(tape_id, scene_id);
CREATE TABLE IF NOT EXISTS face_scanned (
    tape_id    TEXT NOT NULL,
    scene_id   TEXT NOT NULL,
    model      TEXT,
    faces      INTEGER,
    scanned_at TEXT,
    PRIMARY KEY (tape_id, scene_id)
);
-- photos & videos from read-only camera/phone folders (see media_library.py)
CREATE TABLE IF NOT EXISTS media (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    path        TEXT NOT NULL UNIQUE,
    album       TEXT NOT NULL,
    name        TEXT NOT NULL,
    kind        TEXT NOT NULL,
    size        INTEGER,
    mtime       REAL,
    status      TEXT NOT NULL DEFAULT 'new',
    error       TEXT,
    taken       TEXT,
    date_source TEXT,
    date_fix    TEXT,
    width       INTEGER,
    height      INTEGER,
    duration    REAL,
    camera      TEXT,
    playable    INTEGER,
    proxy       INTEGER NOT NULL DEFAULT 0,
    workspace   TEXT NOT NULL DEFAULT 'default',
    added_at    TEXT
);
CREATE INDEX IF NOT EXISTS media_album ON media(workspace, album);
CREATE INDEX IF NOT EXISTS media_status ON media(status);
-- films about one person ("Zmontuj film", see memories.py): params = what the
-- operator chose, plan = the picked moments (kept so the music can be swapped)
CREATE TABLE IF NOT EXISTS memories (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace  TEXT NOT NULL DEFAULT 'default',
    person_id  INTEGER NOT NULL,
    created_at TEXT,
    updated_at TEXT,
    status     TEXT NOT NULL DEFAULT 'QUEUED',
    progress   REAL,
    title      TEXT,
    params     TEXT,
    plan       TEXT,
    music      TEXT,
    path       TEXT,
    size       INTEGER,
    duration   REAL,
    error      TEXT
);
CREATE INDEX IF NOT EXISTS memories_person ON memories(person_id);
-- operator corrections the re-clustering must honour: kind 'same' / 'diff' between two tracks
CREATE TABLE IF NOT EXISTS face_feedback (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL,
    a          INTEGER NOT NULL,
    b          INTEGER NOT NULL,
    created_at TEXT
);
"""

# columns added after the first release of face recognition (ALTER TABLE on open)
_MIGRATIONS = {
    "face_tracks": [("arc", "BLOB"), ("rq", "REAL"), ("frontal", "REAL"), ("refined", "TEXT")],
    "face_persons": [("anchor", "INTEGER"), ("grp", "TEXT")],
    # long videos split into scene clips: clips point at their parent; split =
    # NULL not looked at yet, 0 kept whole, 1 replaced by its clips
    "media": [("parent_id", "INTEGER"), ("clip_start", "REAL"), ("clip_end", "REAL"), ("split", "INTEGER"),
              # frame rate of what the browser plays (proxy / clip / original); reproxy =
              # 1 when the operator asked for a DVD copy to be re-encoded with today's settings
              ("fps", "REAL"), ("reproxy", "INTEGER")],
}
# a media row that stands for itself (not a long video replaced by its clips)
_VISIBLE = "(split IS NULL OR split=0)"

# the one face-scan build row (singleton: see JobStore.enqueue_face_scan)
FACES_KEY = "_faces/SCAN"


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class JobStore:
    def __init__(self, path: Path | str):
        self.path = str(path)
        self._lock = threading.RLock()
        self._orphans: list = []          # clips of media files that changed (see media_seen)
        self._conn = sqlite3.connect(self.path, timeout=15, isolation_level=None,
                                     check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=15000")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        with self._lock:
            self._conn.executescript(_SCHEMA)
            for table, cols in _MIGRATIONS.items():
                have = {r[1] for r in self._conn.execute(f"PRAGMA table_info({table})")}
                for col, typ in cols:
                    if col not in have:
                        self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")

    # ---- low-level -------------------------------------------------------
    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _immediate(self):
        """Context-managed BEGIN IMMEDIATE / COMMIT / ROLLBACK."""
        store = self

        class _Tx:
            def __enter__(self):
                store._conn.execute("BEGIN IMMEDIATE")

            def __exit__(self, exc_type, exc, tb):
                store._conn.execute("ROLLBACK" if exc_type else "COMMIT")
                return False

        return _Tx()

    # ---- jobs ---------------------------------------------------------
    def put_job(self, job: dict) -> dict:
        job = dict(job)
        job.setdefault("updated_at", now())
        with self._lock:
            self._conn.execute(
                "INSERT INTO jobs (tape_id, status, stage, cancel, updated_at, payload) "
                "VALUES (?,?,?,?,?,?) ON CONFLICT(tape_id) DO UPDATE SET "
                "status=excluded.status, stage=excluded.stage, cancel=excluded.cancel, "
                "updated_at=excluded.updated_at, payload=excluded.payload",
                (job["tape_id"], job.get("status", "CREATED"), job.get("stage"),
                 int(bool(job.get("cancel"))), job["updated_at"], json.dumps(job)))
        return job

    def get_job(self, tape_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT payload FROM jobs WHERE tape_id=?", (tape_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def all_job_ids(self) -> list[str]:
        with self._lock:
            return [r[0] for r in self._conn.execute("SELECT tape_id FROM jobs")]

    def list_jobs(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute("SELECT payload FROM jobs ORDER BY updated_at DESC").fetchall()
        return [json.loads(r[0]) for r in rows]

    def delete_job(self, tape_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM jobs WHERE tape_id=?", (tape_id,))

    def replace_all_jobs(self, jobs: dict[str, dict]) -> None:
        with self._lock, self._immediate():
            self._conn.execute("DELETE FROM jobs")
            for tape_id, job in jobs.items():
                job = {**job, "tape_id": tape_id}
                job.setdefault("updated_at", now())
                self._conn.execute(
                    "INSERT INTO jobs (tape_id, status, stage, cancel, updated_at, payload) VALUES (?,?,?,?,?,?)",
                    (tape_id, job.get("status", "CREATED"), job.get("stage"),
                     int(bool(job.get("cancel"))), job["updated_at"], json.dumps(job)))

    def mark_cancel(self, tape_id: str) -> dict | None:
        with self._lock, self._immediate():
            row = self._conn.execute("SELECT payload FROM jobs WHERE tape_id=?", (tape_id,)).fetchone()
            if not row:
                return None
            job = json.loads(row[0])
            job["cancel"] = True
            job["updated_at"] = now()
            self._conn.execute("UPDATE jobs SET cancel=1, updated_at=?, payload=? WHERE tape_id=?",
                               (job["updated_at"], json.dumps(job), tape_id))
            return job

    def clear_cancel(self, tape_id: str) -> dict | None:
        """A capture-stage cancel that has been honored (partial tape archived, see
        Engine._capture_job) must not carry over and poison the processing stage —
        _set()'s cancel guard only ever applies there."""
        with self._lock, self._immediate():
            row = self._conn.execute("SELECT payload FROM jobs WHERE tape_id=?", (tape_id,)).fetchone()
            if not row:
                return None
            job = json.loads(row[0])
            job["cancel"] = False
            job["updated_at"] = now()
            self._conn.execute("UPDATE jobs SET cancel=0, updated_at=?, payload=? WHERE tape_id=?",
                               (job["updated_at"], json.dumps(job), tape_id))
            return job

    def pending(self) -> list[str]:
        """stage=process rows the converter has not claimed yet (oldest first)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT tape_id FROM jobs WHERE stage='process' AND status IN (%s) ORDER BY updated_at"
                % ",".join("?" * len(_QUEUEABLE)), _QUEUEABLE).fetchall()
        return [r[0] for r in rows]

    def capture_job(self) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT payload FROM jobs WHERE stage='capture' AND status NOT IN (%s) "
                "ORDER BY updated_at DESC LIMIT 1" % ",".join("?" * len(TERMINAL)),
                tuple(TERMINAL)).fetchone()
        return json.loads(row[0]) if row else None

    def processing_job(self) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT payload FROM jobs WHERE stage='process' AND status NOT IN (%s) "
                "AND status NOT IN (%s) ORDER BY updated_at DESC LIMIT 1"
                % (",".join("?" * len(TERMINAL)), ",".join("?" * len(_QUEUEABLE))),
                (*TERMINAL, *_QUEUEABLE)).fetchone()
        return json.loads(row[0]) if row else None

    def claim_next_capture(self) -> dict | None:
        """For the grabber's dispatch loop: take a freshly-created capture job."""
        with self._lock, self._immediate():
            row = self._conn.execute(
                "SELECT payload FROM jobs WHERE stage='capture' AND status='CREATED' "
                "ORDER BY updated_at LIMIT 1").fetchone()
            if not row:
                return None
            job = json.loads(row[0])
            job["status"], job["updated_at"] = "CHECKING_STORAGE", now()
            self._conn.execute("UPDATE jobs SET status=?, updated_at=?, payload=? WHERE tape_id=?",
                               (job["status"], job["updated_at"], json.dumps(job), job["tape_id"]))
            return job

    def claim_next_process(self) -> dict | None:
        with self._lock, self._immediate():
            row = self._conn.execute(
                "SELECT payload FROM jobs WHERE stage='process' AND status IN (%s) ORDER BY updated_at LIMIT 1"
                % ",".join("?" * len(_QUEUEABLE)), _QUEUEABLE).fetchone()
            if not row:
                return None
            job = json.loads(row[0])
            job["status"], job["updated_at"] = "ANALYZING_DV", now()
            self._conn.execute("UPDATE jobs SET status=?, updated_at=?, payload=? WHERE tape_id=?",
                               (job["status"], job["updated_at"], json.dumps(job), job["tape_id"]))
            return job

    def enqueue_process(self, tape_id: str) -> None:
        """Move a captured job into the converter queue (keeps the CAPTURED label)."""
        with self._lock, self._immediate():
            row = self._conn.execute("SELECT payload FROM jobs WHERE tape_id=?", (tape_id,)).fetchone()
            job = json.loads(row[0]) if row else {"tape_id": tape_id, "history": [], "logs": ""}
            job["stage"], job["status"], job["updated_at"] = "process", "CAPTURED", now()
            self._conn.execute(
                "INSERT INTO jobs (tape_id, status, stage, cancel, updated_at, payload) VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(tape_id) DO UPDATE SET status='CAPTURED', stage='process', "
                "updated_at=excluded.updated_at, payload=excluded.payload",
                (tape_id, "CAPTURED", "process", int(bool(job.get("cancel"))), job["updated_at"], json.dumps(job)))

    def prune(self, keep: int = MAX_JOBS) -> None:
        with self._lock, self._immediate():
            rows = self._conn.execute(
                "SELECT tape_id, status FROM jobs ORDER BY updated_at DESC").fetchall()
            for tape_id, status in rows[keep:]:
                if status in TERMINAL:
                    self._conn.execute("DELETE FROM jobs WHERE tape_id=?", (tape_id,))

    def tape_busy(self, tape_id: str, ignore_builds: bool = False) -> bool:
        """True while a non-terminal job or a running build holds this tape.

        ignore_builds skips the build check — for a build's own worker thread
        checking busy-ness as it starts that very build: the build is itself
        QUEUED/RUNNING at that point, so the plain check would always see itself
        and refuse to proceed. A capture/processing job is still a real conflict
        either way and always checked."""
        with self._lock:
            j = self._conn.execute(
                "SELECT 1 FROM jobs WHERE tape_id=? AND status NOT IN (%s) LIMIT 1"
                % ",".join("?" * len(TERMINAL)), (tape_id, *TERMINAL)).fetchone()
            if j:
                return True
            if ignore_builds:
                return False
            b = self._conn.execute(
                "SELECT 1 FROM builds WHERE tape_id=? AND status IN ('QUEUED','RUNNING') LIMIT 1",
                (tape_id,)).fetchone()
            return bool(b)

    # ---- builds (share / concat re-encodes) -----------------------------
    def get_build(self, key: str) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT payload FROM builds WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def put_build(self, build: dict) -> dict:
        """Upsert; a ``cancel`` flag already set on the row (request_build_cancel,
        from another process) survives the worker writing its own copy back."""
        build = dict(build)
        build["updated_at"] = now()
        with self._lock:
            self._conn.execute(
                "INSERT INTO builds (key, tape_id, status, updated_at, payload) VALUES (?,?,?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET tape_id=excluded.tape_id, status=excluded.status, "
                "updated_at=excluded.updated_at, payload=CASE "
                "WHEN json_extract(builds.payload, '$.cancel') AND excluded.status IN ('QUEUED','RUNNING') "
                "THEN json_set(excluded.payload, '$.cancel', json('true')) ELSE excluded.payload END",
                (build["key"], build.get("tape_id"), build.get("status", "QUEUED"),
                 build["updated_at"], json.dumps(build)))
        return build

    def list_builds(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute("SELECT payload FROM builds ORDER BY updated_at DESC").fetchall()
        return [json.loads(r[0]) for r in rows]

    def builds_for_tape(self, tape_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute("SELECT payload FROM builds WHERE tape_id=?", (tape_id,)).fetchall()
        return [json.loads(r[0]) for r in rows]

    def delete_build(self, key: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM builds WHERE key=?", (key,))

    def prune_builds(self, max_age_hours: float = 6) -> None:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=max_age_hours)).isoformat()
        with self._lock:
            self._conn.execute("DELETE FROM builds WHERE status IN ('READY','ERROR','CANCELLED') AND updated_at < ?",
                               (cutoff,))

    def claim_next_build(self) -> dict | None:
        """Next share/restore/... build. The face scan has its own worker (it can
        run for days; it must not hold up a quick FB export), so it's skipped here."""
        with self._lock, self._immediate():
            row = self._conn.execute(
                "SELECT payload FROM builds WHERE status='QUEUED' AND key<>? ORDER BY updated_at LIMIT 1",
                (FACES_KEY,)).fetchone()
            if not row:
                return None
            build = json.loads(row[0])
            build["status"], build["updated_at"] = "RUNNING", now()
            self._conn.execute("UPDATE builds SET status='RUNNING', updated_at=?, payload=? WHERE key=?",
                               (build["updated_at"], json.dumps(build), build["key"]))
            return build

    # ---- tape / scene index (rebuilt periodically; keeps /api/tapes + timeline fast) ----
    def put_index(self, tape_id: str, sig: str, doc: dict) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO library (tape_id, source_sig, indexed_at, payload) VALUES (?,?,?,?) "
                "ON CONFLICT(tape_id) DO UPDATE SET source_sig=excluded.source_sig, "
                "indexed_at=excluded.indexed_at, payload=excluded.payload",
                (tape_id, sig, now(), json.dumps(doc)))

    def get_index(self, tape_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT payload FROM library WHERE tape_id=?", (tape_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def index_sig(self, tape_id: str) -> str | None:
        with self._lock:
            row = self._conn.execute("SELECT source_sig FROM library WHERE tape_id=?", (tape_id,)).fetchone()
        return row[0] if row else None

    def list_index(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute("SELECT payload FROM library ORDER BY tape_id").fetchall()
        return [json.loads(r[0]) for r in rows]

    def index_tape_ids(self) -> set[str]:
        with self._lock:
            return {r[0] for r in self._conn.execute("SELECT tape_id FROM library")}

    def delete_index(self, tape_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM library WHERE tape_id=?", (tape_id,))

    # ---- duplicate-detection false positives, dismissed per scene pair -----
    def dismiss_duplicate_pair(self, scene_a: str, scene_b: str) -> None:
        a, b = sorted((scene_a, scene_b))
        with self._lock:
            self._conn.execute(
                "INSERT INTO dup_dismissed (scene_a, scene_b, dismissed_at) VALUES (?,?,?) "
                "ON CONFLICT(scene_a, scene_b) DO NOTHING", (a, b, now()))

    def dismissed_duplicate_pairs(self) -> set[tuple[str, str]]:
        with self._lock:
            return {(r[0], r[1]) for r in self._conn.execute("SELECT scene_a, scene_b FROM dup_dismissed")}

    # ---- workspaces: operator-created labels so several people's captures ----
    # don't mix in one view. "default" always exists implicitly (every tape
    # without one belongs to it) — it never needs a row here.
    def ensure_workspace(self, name: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO workspaces (name, created_at) VALUES (?,?) ON CONFLICT(name) DO NOTHING",
                (name, now()))

    def list_workspaces(self) -> list[str]:
        with self._lock:
            return [r[0] for r in self._conn.execute("SELECT name FROM workspaces ORDER BY created_at")]

    # ---- face scan: one singleton build row, cancellable ------------------
    def enqueue_face_scan(self, trigger: str) -> dict:
        """Queue the face scan unless one is already QUEUED/RUNNING — checked and
        written in one IMMEDIATE transaction, so the cron and a click (or two api
        processes) can never start two scans side by side."""
        with self._lock, self._immediate():
            row = self._conn.execute("SELECT status FROM builds WHERE key=?", (FACES_KEY,)).fetchone()
            if row and row[0] in ("QUEUED", "RUNNING"):
                raise RuntimeError("skanowanie twarzy już trwa")
            build = {"key": FACES_KEY, "token": "SCAN", "tape_id": "_faces", "mode": "faces",
                     "title": "Rozpoznawanie twarzy", "status": "QUEUED", "trigger": trigger,
                     "path": "", "size": None, "error": None, "progress": 0.0, "cancel": False,
                     "updated_at": now(), "queued_at": now()}
            self._conn.execute(
                "INSERT INTO builds (key, tape_id, status, updated_at, payload) VALUES (?,?,?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET tape_id=excluded.tape_id, status=excluded.status, "
                "updated_at=excluded.updated_at, payload=excluded.payload",
                (FACES_KEY, "_faces", "QUEUED", build["updated_at"], json.dumps(build)))
        return build

    def claim_face_scan(self) -> dict | None:
        with self._lock, self._immediate():
            row = self._conn.execute("SELECT payload FROM builds WHERE key=? AND status='QUEUED'",
                                     (FACES_KEY,)).fetchone()
            if not row:
                return None
            build = json.loads(row[0])
            build["status"], build["updated_at"], build["started_at"] = "RUNNING", now(), now()
            self._conn.execute("UPDATE builds SET status='RUNNING', updated_at=?, payload=? WHERE key=?",
                               (build["updated_at"], json.dumps(build), FACES_KEY))
            return build

    def request_build_cancel(self, key: str) -> dict | None:
        """Flag a QUEUED/RUNNING build for cancellation. A QUEUED one is cancelled
        on the spot; a RUNNING one is stopped by its worker at the next check."""
        with self._lock, self._immediate():
            row = self._conn.execute("SELECT payload, status FROM builds WHERE key=?", (key,)).fetchone()
            if not row or row[1] not in ("QUEUED", "RUNNING"):
                return None
            build = json.loads(row[0])
            build["cancel"] = True
            if row[1] == "QUEUED":
                build["status"] = "CANCELLED"
            build["updated_at"] = now()
            self._conn.execute("UPDATE builds SET status=?, updated_at=?, payload=? WHERE key=?",
                               (build["status"], build["updated_at"], json.dumps(build), key))
            return build

    def build_cancelled(self, key: str) -> bool:
        with self._lock:
            row = self._conn.execute("SELECT json_extract(payload, '$.cancel') FROM builds WHERE key=?",
                                     (key,)).fetchone()
        return bool(row and row[0])

    def get_meta(self, k: str) -> str | None:
        with self._lock:
            row = self._conn.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return row[0] if row else None

    def set_meta(self, k: str, v: str) -> None:
        with self._lock:
            self._conn.execute("INSERT INTO meta (k, v) VALUES (?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                               (k, v))

    # ---- faces: persons (clusters, per workspace) and their tracks ------------
    # A track = one person's appearance in one scene (timestamps + embedding).
    # ``faces_gen`` is bumped on every operator edit that moves tracks between
    # persons, so a running scan knows to reload its cached centroids.
    def faces_gen(self) -> int:
        return int(self.get_meta("faces_gen") or 0)

    def _bump_faces_gen(self) -> None:
        self._conn.execute("INSERT INTO meta (k, v) VALUES ('faces_gen', '1') "
                           "ON CONFLICT(k) DO UPDATE SET v=CAST(v AS INTEGER)+1")

    def face_scanned_keys(self, model: str) -> set[tuple[str, str]]:
        with self._lock:
            return {(r[0], r[1]) for r in self._conn.execute(
                "SELECT tape_id, scene_id FROM face_scanned WHERE model=?", (model,))}

    def face_scanned_count(self, tape_ids: set[str] | None = None) -> int:
        with self._lock:
            rows = self._conn.execute("SELECT tape_id FROM face_scanned").fetchall()
        return sum(1 for r in rows if tape_ids is None or r[0] in tape_ids)

    def face_person_centroids(self, workspace: str) -> list[tuple[int, bytes, int]]:
        with self._lock:
            return [tuple(r) for r in self._conn.execute(
                "SELECT id, centroid, n FROM face_persons WHERE workspace=? AND centroid IS NOT NULL", (workspace,))]

    def face_person_create(self, workspace: str, centroid: bytes | None = None, n: int = 0) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO face_persons (workspace, centroid, n, created_at) VALUES (?,?,?,?)",
                (workspace, centroid, n, now()))
            return int(cur.lastrowid)

    def face_person_set_centroid(self, person_id: int, centroid: bytes | None, n: int) -> None:
        with self._lock:
            self._conn.execute("UPDATE face_persons SET centroid=?, n=? WHERE id=?", (centroid, n, person_id))

    def face_save_scene(self, tape_id: str, scene_id: str, model: str, tracks: list[dict]) -> list[int] | None:
        """Atomically replace one scene's tracks and mark it scanned. Returns the
        new track ids (same order), or None — nothing written — if a person a track
        points at was merged/deleted meanwhile (the caller re-matches and retries)."""
        with self._lock, self._immediate():
            for t in tracks:
                if not self._conn.execute("SELECT 1 FROM face_persons WHERE id=?", (t["person_id"],)).fetchone():
                    return None
            self._conn.execute("DELETE FROM face_tracks WHERE tape_id=? AND scene_id=?", (tape_id, scene_id))
            ids = []
            for t in tracks:
                cur = self._conn.execute(
                    "INSERT INTO face_tracks (person_id, workspace, tape_id, scene_id, embedding, quality, times) "
                    "VALUES (?,?,?,?,?,?,?)", (t["person_id"], t["workspace"], tape_id, scene_id,
                                               t["embedding"], t["quality"], json.dumps(t["times"])))
                ids.append(int(cur.lastrowid))
            self._conn.execute(
                "INSERT INTO face_scanned (tape_id, scene_id, model, faces, scanned_at) VALUES (?,?,?,?,?) "
                "ON CONFLICT(tape_id, scene_id) DO UPDATE SET model=excluded.model, faces=excluded.faces, "
                "scanned_at=excluded.scanned_at", (tape_id, scene_id, model, len(tracks), now()))
            return ids

    def face_persons(self, workspace: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT p.id, p.name, p.hidden, COUNT(t.id), COUNT(DISTINCT t.tape_id || '/' || t.scene_id), "
                "(SELECT id FROM face_tracks WHERE person_id=p.id ORDER BY COALESCE(rq, quality) DESC LIMIT 1), p.grp "
                "FROM face_persons p LEFT JOIN face_tracks t ON t.person_id=p.id "
                "WHERE p.workspace=? GROUP BY p.id", (workspace,)).fetchall()
        return [{"id": r[0], "name": r[1], "hidden": bool(r[2]), "tracks": r[3], "scenes": r[4], "cover": r[5],
                 "group": r[6]} for r in rows]

    def face_person(self, person_id: int) -> dict | None:
        with self._lock:
            r = self._conn.execute("SELECT id, workspace, name, hidden, grp FROM face_persons WHERE id=?",
                                   (person_id,)).fetchone()
        return {"id": r[0], "workspace": r[1], "name": r[2], "hidden": bool(r[3]), "group": r[4]} if r else None

    def face_tracks(self, person_id: int, with_embedding: bool = False) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, tape_id, scene_id, COALESCE(rq, quality), times, embedding FROM face_tracks WHERE person_id=? "
                "ORDER BY tape_id, scene_id, id", (person_id,)).fetchall()
        out = []
        for r in rows:
            t = {"id": r[0], "tape_id": r[1], "scene_id": r[2], "quality": r[3], "times": json.loads(r[4])}
            if with_embedding:
                t["embedding"] = r[5]
            out.append(t)
        return out

    def face_track(self, track_id: int) -> dict | None:
        with self._lock:
            r = self._conn.execute("SELECT id, person_id, workspace, tape_id, scene_id FROM face_tracks WHERE id=?",
                                   (track_id,)).fetchone()
        return {"id": r[0], "person_id": r[1], "workspace": r[2], "tape_id": r[3], "scene_id": r[4]} if r else None

    def face_person_update(self, person_id: int, *, name=None, hidden=None, group=None) -> None:
        with self._lock:
            # naming / hiding / grouping = "this face is somebody": pin the face the
            # operator was looking at, so re-clustering knows which new group it belongs to
            self._anchor(person_id)
            if group is not None:
                self._conn.execute("UPDATE face_persons SET grp=? WHERE id=?", (group or None, person_id))
            if name is not None:
                self._conn.execute("UPDATE face_persons SET name=? WHERE id=?", (name or None, person_id))
            if hidden is not None:
                self._conn.execute("UPDATE face_persons SET hidden=? WHERE id=?", (int(bool(hidden)), person_id))

    def face_person_by_name(self, workspace: str, name: str, exclude: int | None = None) -> int | None:
        with self._lock:
            r = self._conn.execute(
                "SELECT id FROM face_persons WHERE workspace=? AND lower(name)=lower(?) AND id<>? LIMIT 1",
                (workspace, name, exclude or -1)).fetchone()
        return r[0] if r else None

    def face_merge(self, src: int, dst: int) -> None:
        """Fold person ``src`` into ``dst`` (same workspace only). ``dst`` keeps
        its name, or takes ``src``'s if it had none."""
        with self._lock, self._immediate():
            a = self._conn.execute("SELECT workspace, name, grp FROM face_persons WHERE id=?", (src,)).fetchone()
            b = self._conn.execute("SELECT workspace, name, grp FROM face_persons WHERE id=?", (dst,)).fetchone()
            if not a or not b:
                raise FileNotFoundError("nie ma takiej osoby")
            if a[0] != b[0]:
                raise ValueError("nie można łączyć osób z różnych workspace")
            ca, cb = self._anchor(src), self._anchor(dst)
            if ca and cb:
                self._feedback("same", ca, cb)
            self._conn.execute("UPDATE face_tracks SET person_id=? WHERE person_id=?", (dst, src))
            if not b[1] and a[1]:
                self._conn.execute("UPDATE face_persons SET name=? WHERE id=?", (a[1], dst))
            if not b[2] and a[2]:
                self._conn.execute("UPDATE face_persons SET grp=? WHERE id=?", (a[2], dst))
            self._conn.execute("DELETE FROM face_persons WHERE id=?", (src,))
            self._bump_faces_gen()

    def face_move_track(self, track_id: int, person_id: int | None) -> tuple[int, int]:
        """Move one track to another person of its workspace, or (person_id=None)
        split it off into a new unnamed person. Returns (old, new) person ids."""
        with self._lock, self._immediate():
            t = self._conn.execute("SELECT person_id, workspace FROM face_tracks WHERE id=?", (track_id,)).fetchone()
            if not t:
                raise FileNotFoundError("nie ma takiej twarzy")
            old_anchor = self._anchor(t[0], exclude=track_id)
            if old_anchor:
                self._feedback("diff", track_id, old_anchor)
            if person_id is None:
                person_id = int(self._conn.execute(
                    "INSERT INTO face_persons (workspace, created_at) VALUES (?,?)", (t[1], now())).lastrowid)
            else:
                p = self._conn.execute("SELECT workspace FROM face_persons WHERE id=?", (person_id,)).fetchone()
                if not p:
                    raise FileNotFoundError("nie ma takiej osoby")
                if p[0] != t[1]:
                    raise ValueError("nie można przenieść twarzy do innego workspace")
                new_anchor = self._anchor(person_id)
                if new_anchor:
                    self._feedback("same", track_id, new_anchor)
            self._conn.execute("UPDATE face_tracks SET person_id=? WHERE id=?", (person_id, track_id))
            self._bump_faces_gen()
            return t[0], person_id

    def face_drop_tracks(self, tape_id: str, scene_ids: list[str] | None = None) -> tuple[list[int], set[int]]:
        """Forget a deleted tape's (or some of its scenes') tracks + scanned marks.
        Returns (dropped track ids, affected person ids); unnamed persons left
        with no tracks at all are deleted."""
        with self._lock, self._immediate():
            if scene_ids is None:
                where, args = "tape_id=?", [tape_id]
            else:
                where, args = (f"tape_id=? AND scene_id IN ({','.join('?' * len(scene_ids))})",
                               [tape_id, *scene_ids])
            rows = self._conn.execute(f"SELECT id, person_id FROM face_tracks WHERE {where}", args).fetchall()
            self._conn.execute(f"DELETE FROM face_tracks WHERE {where}", args)
            self._conn.execute(f"DELETE FROM face_scanned WHERE {where}", args)
            persons = {r[1] for r in rows}
            self._conn.execute("DELETE FROM face_persons WHERE name IS NULL AND id NOT IN "
                               "(SELECT DISTINCT person_id FROM face_tracks)")
            if rows:
                self._bump_faces_gen()
        return [r[0] for r in rows], persons

    def face_rename_tape(self, old: str, new: str) -> None:
        with self._lock, self._immediate():
            self._conn.execute("UPDATE face_tracks SET tape_id=? WHERE tape_id=?", (new, old))
            self._conn.execute("UPDATE face_scanned SET tape_id=? WHERE tape_id=?", (new, old))

    # ---- refinement + re-clustering (faces.refine / faces.recluster) ----------
    def _feedback(self, kind: str, a: int, b: int) -> None:
        if a != b:
            self._conn.execute("INSERT INTO face_feedback (kind, a, b, created_at) VALUES (?,?,?,?)",
                               (kind, a, b, now()))

    def _anchor(self, person_id: int, exclude: int | None = None) -> int | None:
        """The person's anchor track — set to its current cover (best face) the first
        time the operator acts on the person."""
        r = self._conn.execute("SELECT anchor FROM face_persons WHERE id=?", (person_id,)).fetchone()
        if not r:
            return None
        if r[0] and r[0] != exclude and self._conn.execute(
                "SELECT 1 FROM face_tracks WHERE id=? AND person_id=?", (r[0], person_id)).fetchone():
            return r[0]
        c = self._conn.execute("SELECT id FROM face_tracks WHERE person_id=? AND id<>? "
                               "ORDER BY COALESCE(rq, quality) DESC LIMIT 1", (person_id, exclude or -1)).fetchone()
        if c:
            self._conn.execute("UPDATE face_persons SET anchor=? WHERE id=?", (c[0], person_id))
        return c[0] if c else None

    def face_import_rules(self, name: str, pairs: list, anchors: dict[int, int]) -> int:
        """One-off import of operator corrections reconstructed from elsewhere (see
        face_refine.import_rules); idempotent per ``name``. Pairs whose tracks no
        longer exist are skipped. Returns how many pairs were added."""
        key = f"face_rules_imported:{name}"
        with self._lock, self._immediate():
            if self._conn.execute("SELECT 1 FROM meta WHERE k=?", (key,)).fetchone():
                return 0
            have = {r[0] for r in self._conn.execute("SELECT id FROM face_tracks")}
            n = 0
            for kind, a, b in pairs:
                if kind in ("same", "diff", "confirm") and a in have and b in have:
                    self._feedback(kind, a, b)
                    n += 1
            for pid, track in anchors.items():
                if track in have:
                    self._conn.execute("UPDATE face_persons SET anchor=? WHERE id=? AND anchor IS NULL", (track, pid))
            self._conn.execute("INSERT INTO meta (k, v) VALUES (?, ?)", (key, now()))
            return n

    def face_signature(self, workspace: str) -> tuple:
        """Cheap change token for anything that affects suggestions."""
        with self._lock:
            return (self._conn.execute("SELECT v FROM meta WHERE k='faces_gen'").fetchone(),
                    self._conn.execute("SELECT count(*), sum(arc IS NOT NULL), sum(person_id) FROM face_tracks "
                                       "WHERE workspace=?", (workspace,)).fetchone(),
                    self._conn.execute("SELECT count(*), group_concat(COALESCE(name,'') || hidden) FROM face_persons "
                                       "WHERE workspace=?", (workspace,)).fetchone(),
                    self._conn.execute("SELECT count(*) FROM face_feedback").fetchone())

    def face_reject_pair(self, person_id: int, other_id: int) -> None:
        """Operator: these two persons are not the same — a hard "diff" between
        their anchor faces (re-clustering and suggestions both honour it)."""
        with self._lock, self._immediate():
            a, b = self._anchor(person_id), self._anchor(other_id)
            if not a or not b:
                raise FileNotFoundError("nie ma takiej osoby")
            self._feedback("diff", a, b)

    def face_feedback(self) -> list[tuple[str, int, int]]:
        with self._lock:
            return [tuple(r) for r in self._conn.execute("SELECT kind, a, b FROM face_feedback ORDER BY id")]

    def face_scenes_to_refine(self, tag: str) -> list[tuple[str, str]]:
        with self._lock:
            return [tuple(r) for r in self._conn.execute(
                "SELECT DISTINCT tape_id, scene_id FROM face_tracks WHERE refined IS NOT ? "
                "ORDER BY tape_id, scene_id", (tag,))]

    def face_scene_tracks(self, tape_id: str, scene_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute("SELECT id, times, embedding FROM face_tracks WHERE tape_id=? AND scene_id=?",
                                      (tape_id, scene_id)).fetchall()
        return [{"id": r[0], "times": json.loads(r[1]), "embedding": r[2]} for r in rows]

    def face_save_refined(self, track_id: int, *, arc: bytes | None, rq: float, frontal: float, tag: str) -> None:
        with self._lock:
            self._conn.execute("UPDATE face_tracks SET arc=?, rq=?, frontal=?, refined=? WHERE id=?",
                               (arc, rq, frontal, tag, track_id))

    def face_workspaces(self) -> list[str]:
        with self._lock:
            return [r[0] for r in self._conn.execute("SELECT DISTINCT workspace FROM face_tracks")]

    def face_workspace_tracks(self, workspace: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, person_id, tape_id, scene_id, times, arc, rq, frontal FROM face_tracks "
                "WHERE workspace=?", (workspace,)).fetchall()
        return [{"id": r[0], "person_id": r[1], "tape_id": r[2], "scene_id": r[3], "times": json.loads(r[4]),
                 "arc": r[5], "rq": r[6] or 0.0, "frontal": r[7] or 0.0} for r in rows]

    def face_touched_persons(self, workspace: str) -> list[dict]:
        """Persons the operator acted on (named, hidden or put in a group), with their anchor track."""
        with self._lock:
            ids = [r[0] for r in self._conn.execute(
                "SELECT id FROM face_persons WHERE workspace=? AND (name IS NOT NULL OR hidden=1 OR grp IS NOT NULL)",
                (workspace,))]
            out = []
            for pid in ids:
                anchor = self._anchor(pid)
                r = self._conn.execute("SELECT name, hidden, grp FROM face_persons WHERE id=?", (pid,)).fetchone()
                out.append({"id": pid, "name": r[0], "hidden": bool(r[1]), "group": r[2], "anchor": anchor})
        return out

    def face_apply_clusters(self, workspace: str, gen: int, groups: list[dict]) -> bool:
        """Re-assign every track of the workspace in one transaction.
        groups: [{"person_id": int | None (None = new person), "tracks": [ids]}].
        Refused (False) if the operator edited persons since ``gen`` was read."""
        with self._lock, self._immediate():
            cur = self._conn.execute("SELECT v FROM meta WHERE k='faces_gen'").fetchone()
            if int(cur[0] if cur else 0) != gen:
                return False
            for g in groups:
                pid = g["person_id"]
                if pid is None or not self._conn.execute("SELECT 1 FROM face_persons WHERE id=?", (pid,)).fetchone():
                    pid = int(self._conn.execute("INSERT INTO face_persons (workspace, created_at) VALUES (?,?)",
                                                 (workspace, now())).lastrowid)
                g["person_id"] = pid
                self._conn.executemany("UPDATE face_tracks SET person_id=? WHERE id=?",
                                       [(pid, t) for t in g["tracks"]])
            self._conn.execute("DELETE FROM face_persons WHERE workspace=? AND name IS NULL AND hidden=0 AND grp IS NULL "
                               "AND id NOT IN "
                               "(SELECT DISTINCT person_id FROM face_tracks)", (workspace,))
            return True

    # ---- media (photos / videos) ------------------------------------------------
    _MEDIA_COLS = ("id", "path", "album", "name", "kind", "size", "mtime", "status", "error", "taken",
                   "date_source", "date_fix", "width", "height", "duration", "camera", "playable", "proxy",
                   "workspace", "added_at", "parent_id", "clip_start", "clip_end", "split", "fps", "reproxy")

    def _media_rows(self, where: str = "", args=()) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(f"SELECT {', '.join(self._MEDIA_COLS)} FROM media {where}", args).fetchall()
        return [dict(zip(self._MEDIA_COLS, r)) for r in rows]

    def media_seen(self, path: str, *, album: str, name: str, kind: str, size: int, mtime: float,
                   workspace: str) -> bool:
        """Register a file; a new or changed one (size/mtime) goes back to 'new'.
        True when it needs (re)processing."""
        with self._lock, self._immediate():
            r = self._conn.execute("SELECT id, size, mtime FROM media WHERE path=?", (path,)).fetchone()
            if r and r[1] == size and abs((r[2] or 0) - mtime) < 1:
                return False
            if r:
                # changed file: its old clips (if it was split) are stale
                self._orphans += [tuple(c) for c in self._conn.execute(
                    "SELECT id, path FROM media WHERE parent_id=?", (r[0],))]
                self._conn.execute("DELETE FROM media WHERE parent_id=?", (r[0],))
                self._conn.execute("UPDATE media SET size=?, mtime=?, status='new', error=NULL, proxy=0, split=NULL "
                                   "WHERE id=?", (size, mtime, r[0]))
            else:
                self._conn.execute(
                    "INSERT INTO media (path, album, name, kind, size, mtime, workspace, added_at) "
                    "VALUES (?,?,?,?,?,?,?,?)", (path, album, name, kind, size, mtime, workspace, now()))
            return True

    def media_take_orphans(self) -> list[tuple[int, str]]:
        """(id, path) of clips dropped because their source file changed."""
        out, self._orphans = self._orphans, []
        return out

    def media_paths(self) -> list[dict]:
        with self._lock:
            return [{"id": r[0], "path": r[1]} for r in
                    self._conn.execute("SELECT id, path FROM media WHERE parent_id IS NULL")]

    def media_delete(self, ids: list[int]) -> list[tuple[int, str]]:
        """Delete rows and their clips; returns every (id, path) removed."""
        if not ids:
            return []
        with self._lock, self._immediate():
            marks = ",".join("?" * len(ids))
            gone = [tuple(r) for r in self._conn.execute(
                f"SELECT id, path FROM media WHERE id IN ({marks}) OR parent_id IN ({marks})", (*ids, *ids))]
            self._conn.execute(f"DELETE FROM media WHERE id IN ({marks}) OR parent_id IN ({marks})", (*ids, *ids))
        return gone

    def media_pending(self, limit: int = 500) -> list[dict]:
        return self._media_rows("WHERE status='new' ORDER BY id LIMIT ?", (limit,))

    def media_needing_proxy(self, limit: int = 50) -> list[dict]:
        return self._media_rows("WHERE status='ready' AND kind='video' AND playable=0 AND proxy=0 "
                                "AND parent_id IS NULL ORDER BY id LIMIT ?", (limit,))

    def media_needing_split(self, min_s: float, limit: int = 20) -> list[dict]:
        """Long videos not looked at yet, whose playable form (original or proxy) exists."""
        return self._media_rows("WHERE status='ready' AND kind='video' AND parent_id IS NULL AND split IS NULL "
                                "AND duration >= ? AND (playable=1 OR proxy=1) ORDER BY id LIMIT ?", (min_s, limit))

    def media_add_clip(self, parent: dict, *, path: str, start: float, end: float, name: str,
                       taken: str | None) -> int:
        with self._lock:
            return int(self._conn.execute(
                "INSERT INTO media (path, album, name, kind, size, mtime, status, taken, date_source, date_fix, "
                "width, height, duration, camera, playable, proxy, workspace, added_at, parent_id, clip_start, "
                "clip_end, fps) VALUES (?,?,?,'video',?,?,'ready',?,?,?,?,?,?,?,1,0,?,?,?,?,?,?)",
                (path, parent["album"], name, Path(path).stat().st_size, parent["mtime"], taken,
                 parent["date_source"], parent["date_fix"], parent["width"], parent["height"], end - start,
                 parent["camera"], parent["workspace"], now(), parent["id"], start, end,
                 parent.get("fps"))).lastrowid)

    def media_update(self, media_id: int, **fields) -> None:
        cols = [c for c in fields if c in self._MEDIA_COLS and c not in ("id", "path")]
        if not cols:
            return
        with self._lock:
            self._conn.execute(f"UPDATE media SET {', '.join(c + '=?' for c in cols)} WHERE id=?",
                               (*[fields[c] for c in cols], media_id))

    def media_undated(self) -> list[dict]:
        """Ready originals no date could be read for and not looked at by
        media_library.date_from_file_times yet (date_source NULL)."""
        return self._media_rows("WHERE status='ready' AND parent_id IS NULL AND taken IS NULL "
                                "AND date_fix IS NULL AND date_source IS NULL")

    def media_rows_by_source(self, date_source: str) -> list[dict]:
        return self._media_rows("WHERE status='ready' AND parent_id IS NULL AND date_fix IS NULL AND date_source=?",
                                (date_source,))

    def media_album_rows(self, album: str) -> list[dict]:
        return self._media_rows("WHERE album=? AND parent_id IS NULL", (album,))

    def media_children(self, parent_id: int) -> list[dict]:
        return self._media_rows("WHERE parent_id=? ORDER BY clip_start", (parent_id,))

    # ---- films about a person (memories.py) ------------------------------------
    _MEM_COLS = ("id", "workspace", "person_id", "created_at", "updated_at", "status", "progress", "title",
                 "params", "plan", "music", "path", "size", "duration", "error")
    _MEM_JSON = ("params", "plan", "music")

    def _mem(self, r) -> dict:
        d = dict(zip(self._MEM_COLS, r))
        for k in self._MEM_JSON:
            d[k] = json.loads(d[k]) if d[k] else None
        return d

    def memory_add(self, workspace: str, person_id: int, title: str, params: dict) -> int:
        with self._lock:
            return int(self._conn.execute(
                "INSERT INTO memories (workspace, person_id, created_at, updated_at, status, progress, title, params) "
                "VALUES (?,?,?,?,'QUEUED',0,?,?)",
                (workspace, person_id, now(), now(), title, json.dumps(params, ensure_ascii=False))).lastrowid)

    def memory_update(self, memory_id: int, **fields) -> None:
        cols = [c for c in fields if c in self._MEM_COLS and c not in ("id", "person_id", "workspace")]
        if not cols:
            return
        vals = [json.dumps(fields[c], ensure_ascii=False) if c in self._MEM_JSON and fields[c] is not None
                else fields[c] for c in cols]
        with self._lock:
            self._conn.execute(f"UPDATE memories SET {', '.join(c + '=?' for c in cols)}, updated_at=? WHERE id=?",
                               (*vals, now(), memory_id))

    def memory_get(self, memory_id: int) -> dict | None:
        with self._lock:
            r = self._conn.execute(f"SELECT {', '.join(self._MEM_COLS)} FROM memories WHERE id=?",
                                   (memory_id,)).fetchone()
        return self._mem(r) if r else None

    def memories_of(self, person_id: int) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(f"SELECT {', '.join(self._MEM_COLS)} FROM memories WHERE person_id=? "
                                      "ORDER BY id DESC", (person_id,)).fetchall()
        return [self._mem(r) for r in rows]

    def memories_move(self, src: int, dst: int) -> None:
        with self._lock:
            self._conn.execute("UPDATE memories SET person_id=? WHERE person_id=?", (dst, src))

    def memory_delete(self, memory_id: int) -> dict | None:
        m = self.memory_get(memory_id)
        if m:
            with self._lock:
                self._conn.execute("DELETE FROM memories WHERE id=?", (memory_id,))
        return m

    def media_ready_originals(self) -> list[dict]:
        return self._media_rows("WHERE status='ready' AND parent_id IS NULL ORDER BY id")

    def media_needing_fps(self, limit: int = 200) -> list[dict]:
        """Playable videos whose frame rate isn't recorded yet (rows from before it was)."""
        return self._media_rows("WHERE status='ready' AND kind='video' AND fps IS NULL "
                                "AND (playable=1 OR proxy=1) ORDER BY id LIMIT ?", (limit,))

    def media_reproxy_pending(self) -> list[dict]:
        return self._media_rows("WHERE reproxy=1 AND status='ready' AND parent_id IS NULL ORDER BY id")

    def face_shift_times(self, tape_id: str, scene_id: str, delta: float, duration: float | None) -> int:
        """Move every face time of one scene by ``delta`` seconds (its video file now
        starts that much later/earlier in the film); times stay whole seconds inside
        the new file. Returns how many tracks changed."""
        changed = 0
        with self._lock, self._immediate():
            for tid, times in self._conn.execute("SELECT id, times FROM face_tracks WHERE tape_id=? AND scene_id=?",
                                                 (tape_id, scene_id)).fetchall():
                old = json.loads(times)
                top = int(duration) if duration else None
                new = sorted({min(top, max(0, round(t + delta))) if top is not None else max(0, round(t + delta))
                              for t in old})
                if new != old:
                    self._conn.execute("UPDATE face_tracks SET times=? WHERE id=?", (json.dumps(new), tid))
                    changed += 1
        return changed

    def media_set_album_date(self, workspace: str, album: str, date_fix: str) -> int:
        """Date every undated item of an album (and the parts of its split films);
        anything that already has a date keeps it. Returns how many were set."""
        with self._lock, self._immediate():
            ids = [r[0] for r in self._conn.execute(
                "SELECT id FROM media WHERE workspace=? AND album=? AND parent_id IS NULL AND taken IS NULL "
                "AND date_fix IS NULL", (workspace, album))]
            for i in ids:
                self._conn.execute("UPDATE media SET date_fix=? WHERE id=? OR parent_id=?", (date_fix, i, i))
        return len(ids)

    def media_set_family_date(self, parent_id: int, date_fix: str | None) -> None:
        with self._lock:
            self._conn.execute("UPDATE media SET date_fix=? WHERE id=? OR parent_id=?", (date_fix, parent_id, parent_id))

    def media_get(self, media_id: int) -> dict | None:
        rows = self._media_rows("WHERE id=?", (media_id,))
        return rows[0] if rows else None

    def media_ready(self) -> list[dict]:
        return self._media_rows(f"WHERE status='ready' AND {_VISIBLE} ORDER BY id")

    def media_items(self, workspace: str, album: str | None = None) -> list[dict]:
        if album is None:
            return self._media_rows(f"WHERE workspace=? AND status='ready' AND {_VISIBLE}", (workspace,))
        return self._media_rows(f"WHERE workspace=? AND album=? AND status='ready' AND {_VISIBLE}", (workspace, album))

    def media_counts(self) -> dict:
        with self._lock:
            rows = self._conn.execute(f"SELECT status, kind, count(*) FROM media WHERE {_VISIBLE} "
                                      "GROUP BY status, kind").fetchall()
            proxies = self._conn.execute("SELECT count(*) FROM media WHERE status='ready' AND kind='video' "
                                         "AND playable=0 AND proxy=0").fetchone()[0]
        out = {"total": 0, "ready": 0, "new": 0, "error": 0, "photos": 0, "videos": 0, "proxies_pending": proxies}
        for status, kind, n in rows:
            out["total"] += n
            out[status] = out.get(status, 0) + n
            if status == "ready":
                out["photos" if kind == "photo" else "videos"] += n
        return out

    def face_dissolve(self, person_ids: list[int]) -> list[int]:
        """Forget these groupings (a cluster of different people): every face becomes
        its own unnamed person and the "same"/"confirm" rules that glued them are
        dropped ("diff" rules stay). Re-clustering then decides afresh. Returns the
        new person ids."""
        new = []
        with self._lock, self._immediate():
            for pid in person_ids:
                p = self._conn.execute("SELECT workspace FROM face_persons WHERE id=?", (pid,)).fetchone()
                if not p:
                    continue
                tids = [r[0] for r in self._conn.execute("SELECT id FROM face_tracks WHERE person_id=?", (pid,))]
                marks = ",".join("?" * len(tids))
                if tids:
                    self._conn.execute(f"DELETE FROM face_feedback WHERE kind IN ('same','confirm') AND "
                                       f"(a IN ({marks}) OR b IN ({marks}))", (*tids, *tids))
                for t in tids:
                    np_id = int(self._conn.execute("INSERT INTO face_persons (workspace, created_at) VALUES (?,?)",
                                                   (p[0], now())).lastrowid)
                    self._conn.execute("UPDATE face_tracks SET person_id=? WHERE id=?", (np_id, t))
                    new.append(np_id)
                self._conn.execute("DELETE FROM face_persons WHERE id=?", (pid,))
            self._bump_faces_gen()
        return new

    def face_delete_persons(self, person_ids: list[int]) -> list[int]:
        """Not faces at all (statues, posters, blur): drop the persons and their
        faces for good — the scenes stay marked scanned, so they don't come back.
        Returns the deleted track ids (for their thumbnails)."""
        gone = []
        with self._lock, self._immediate():
            for pid in person_ids:
                gone += [r[0] for r in self._conn.execute("SELECT id FROM face_tracks WHERE person_id=?", (pid,))]
                self._conn.execute("DELETE FROM face_tracks WHERE person_id=?", (pid,))
                self._conn.execute("DELETE FROM face_persons WHERE id=?", (pid,))
            self._bump_faces_gen()
        return gone

    def face_prune_empty(self) -> None:
        """Unnamed persons that lost every track (split/merge leftovers) go away."""
        with self._lock:
            self._conn.execute("DELETE FROM face_persons WHERE name IS NULL AND id NOT IN "
                               "(SELECT DISTINCT person_id FROM face_tracks)")

    # ---- camera transport requests (api -> grabber) --------------------
    def enqueue_cam_command(self, name: str) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO cam_commands (name, status, created_at) VALUES (?, 'QUEUED', ?)", (name, now()))
            return int(cur.lastrowid)

    def claim_cam_command(self) -> dict | None:
        with self._lock, self._immediate():
            row = self._conn.execute(
                "SELECT id, name FROM cam_commands WHERE status='QUEUED' ORDER BY id LIMIT 1").fetchone()
            if not row:
                return None
            self._conn.execute("UPDATE cam_commands SET status='RUNNING' WHERE id=?", (row[0],))
            return {"id": row[0], "name": row[1]}

    def finish_cam_command(self, cmd_id: int, ok: bool, result) -> None:
        with self._lock:
            self._conn.execute("UPDATE cam_commands SET status=?, result=? WHERE id=?",
                               ("DONE" if ok else "ERROR", json.dumps(result), cmd_id))

    def cam_command_result(self, cmd_id: int) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT status, result FROM cam_commands WHERE id=?", (cmd_id,)).fetchone()
        if not row:
            return None
        return {"status": row[0], "result": json.loads(row[1]) if row[1] else None}

    def wait_cam_command(self, cmd_id: int, timeout: float = 6.0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            res = self.cam_command_result(cmd_id)
            if res and res["status"] in ("DONE", "ERROR"):
                return res
            time.sleep(0.2)
        return {"status": "TIMEOUT", "result": None}

    # ---- migration + restart reconciliation --------------------------
    def import_legacy(self, json_path: Path) -> bool:
        """One-shot: pull an old state/jobs.json into the db, then set it aside."""
        with self._lock:
            done = self._conn.execute("SELECT v FROM meta WHERE k='legacy_imported'").fetchone()
            has_rows = self._conn.execute("SELECT 1 FROM jobs LIMIT 1").fetchone()
        if done or has_rows or not json_path.exists():
            return False
        try:
            legacy = json.loads(json_path.read_text())
        except (OSError, ValueError):
            return False
        if isinstance(legacy, dict):
            for tape_id, job in legacy.items():
                self.put_job({**job, "tape_id": tape_id})
        with self._lock:
            self._conn.execute("INSERT OR REPLACE INTO meta (k, v) VALUES ('legacy_imported', ?)", (now(),))
        try:
            json_path.rename(json_path.with_suffix(".json.imported"))
        except OSError:
            pass
        return True

    def reconcile(self, config, stages: tuple[str, ...] = ("capture", "process")) -> None:
        """No capture survives a restart; resume processing where the raw DV is still
        on disk. Same three cases as the old Engine._load_jobs.

        `stages` scopes this to the job stages *this process* actually owns: a capture
        job is only really orphaned if the grabber restarted, a process job only if the
        converter restarted. A restarting api (or any role that doesn't own that stage)
        must pass an empty/narrower tuple — otherwise it would rmtree a tapes/<id> dir a
        sibling converter/grabber process is actively writing to, mid-job, and crash it."""
        for job in self.list_jobs():
            if job.get("status") in TERMINAL or job.get("stage") not in stages:
                continue
            tape_id = job["tape_id"]
            tape_dir = config.tapes / tape_id
            dv = config.working / tape_id / "capture001.dv"
            if (tape_dir / "tape.json").exists():
                job["stage"], job["status"] = "process", "COMPLETED"
                _rmtree(config.working / tape_id)
            elif dv.exists() and dv.stat().st_size > 0:
                job["stage"], job["status"] = "process", "QUEUED"
                _rmtree(tape_dir)
            else:
                job["status"] = "ERROR"
                job["error"] = "interrupted by a restart"
                _rmtree(config.working / tape_id)
            job["updated_at"] = now()
            self.put_job(job)
        self.prune()


def _rmtree(path: Path) -> None:
    import shutil
    shutil.rmtree(path, ignore_errors=True)
