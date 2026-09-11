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
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class JobStore:
    def __init__(self, path: Path | str):
        self.path = str(path)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, timeout=15, isolation_level=None,
                                     check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=15000")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        with self._lock:
            self._conn.executescript(_SCHEMA)

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
        build = dict(build)
        build["updated_at"] = now()
        with self._lock:
            self._conn.execute(
                "INSERT INTO builds (key, tape_id, status, updated_at, payload) VALUES (?,?,?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET tape_id=excluded.tape_id, status=excluded.status, "
                "updated_at=excluded.updated_at, payload=excluded.payload",
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
            self._conn.execute("DELETE FROM builds WHERE status IN ('READY','ERROR') AND updated_at < ?",
                               (cutoff,))

    def claim_next_build(self) -> dict | None:
        with self._lock, self._immediate():
            row = self._conn.execute(
                "SELECT payload FROM builds WHERE status='QUEUED' ORDER BY updated_at LIMIT 1").fetchone()
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
