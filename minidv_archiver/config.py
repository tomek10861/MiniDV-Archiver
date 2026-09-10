from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    storage: Path = Path(os.getenv("MINIDV_STORAGE", "/srv/minidv"))
    # Empty -> auto-detect the first AV/C tape unit (0x00a02d) on the 1394 bus.
    # Pin it to a GUID when more than one camera/deck may be connected.
    camera_guid: str = os.getenv("MINIDV_CAMERA_GUID", "").lower().removeprefix("0x")
    min_free_gib: int = int(os.getenv("MINIDV_MIN_FREE_GIB", "80"))
    rewind_timeout: int = int(os.getenv("MINIDV_REWIND_TIMEOUT", "420"))
    # End the capture only after this many seconds with no new DV bytes. Also the
    # window we keep waiting (relaunching dvgrab) through a blank stretch of tape.
    capture_idle_timeout: int = int(os.getenv("MINIDV_CAPTURE_IDLE_TIMEOUT", "300"))
    capture_max_seconds: int = int(os.getenv("MINIDV_CAPTURE_MAX_SECONDS", "9000"))
    play_wait_timeout: int = int(os.getenv("MINIDV_PLAY_WAIT_TIMEOUT", "300"))
    # End-of-tape detection: stop this many seconds after the DV signal stops entirely
    # (camera auto-stopped / physical end of reel)...
    no_signal_timeout: int = int(os.getenv("MINIDV_NO_SIGNAL_TIMEOUT", "45"))
    # ...or this many seconds of nothing-but-blank frames (recording ended, tape still
    # rolling). Real timecoded footage resets it, so gaps shorter than this survive.
    blank_tail_timeout: int = int(os.getenv("MINIDV_BLANK_TAIL_TIMEOUT", "120"))
    # A manual "Przerwij" that already holds at least this much DV is archived, not dropped.
    keep_partial_min_seconds: int = int(os.getenv("MINIDV_KEEP_PARTIAL_MIN_SECONDS", "20"))
    # dvgrab quits by itself when the DV signal drops (blank tape); relaunch it into a
    # new segment up to this many times, then concatenate. Bounded by capture_idle_timeout.
    max_dvgrab_restarts: int = int(os.getenv("MINIDV_MAX_DVGRAB_RESTARTS", "40"))
    # DCR-PC2E flaps on/off the bus; wait a bit for it before failing the job.
    camera_wait_timeout: int = int(os.getenv("MINIDV_CAMERA_WAIT_TIMEOUT", "20"))
    zstd_level: int = int(os.getenv("MINIDV_ZSTD_LEVEL", "6"))
    # Proxy encode preset (x264). "veryfast" ~4-6x faster than "medium" for a modest
    # size bump; the master is the byte-exact .dv.zst so proxy speed is a fair trade.
    mp4_preset: str = os.getenv("MINIDV_MP4_PRESET", "medium")
    # nice/ionice the processing pipeline so a concurrent capture never underruns.
    nice_processing: bool = os.getenv("MINIDV_NICE_PROCESSING", "1") == "1"
    # On-demand "share to Messenger/FB" re-encode: keep resolution, target this size.
    share_max_mb: int = int(os.getenv("MINIDV_SHARE_MAX_MB", "90"))
    share_crf: int = int(os.getenv("MINIDV_SHARE_CRF", "23"))
    share_preset: str = os.getenv("MINIDV_SHARE_PRESET", "veryfast")
    # Optional "restore" variant: a denoise/repair MP4 encoded from the DV master(s),
    # never the proxy. Tune the whole filter chain here (bwdif does the deinterlace).
    restore_filters: str = os.getenv(
        "MINIDV_RESTORE_FILTERS",
        "bwdif=mode=send_field:parity=bff:deint=all,atadenoise,deblock=filter=strong:block=8")
    restore_crf: int = int(os.getenv("MINIDV_RESTORE_CRF", "18"))
    restore_preset: str = os.getenv("MINIDV_RESTORE_PRESET", "medium")
    # mpdecimate: drop duplicated frames (dvgrab fills capture drops with repeats) —
    # off by default because it makes the stream VFR.
    restore_decimate: bool = os.getenv("MINIDV_RESTORE_DECIMATE", "0") == "1"
    bind: str = os.getenv("MINIDV_BIND", "0.0.0.0")
    port: int = int(os.getenv("MINIDV_PORT", "8080"))
    # How often the background pass refreshes the tape/scene index (state/jobs.db);
    # unchanged tapes are skipped, so this stays cheap even with thousands of them.
    index_interval: int = int(os.getenv("MINIDV_INDEX_INTERVAL", "300"))
    # AV/C transport control (auto PLAY/STOP/REW). On by default — most decks and
    # camcorders handle it fine. Set MINIDV_ALLOW_FCP=0 for cameras whose AV/C stack
    # is flaky and resets the 1394 bus on FCP (e.g. Sony DCR-PC2E) -> manual PLAY/STOP.
    allow_fcp: bool = os.getenv("MINIDV_ALLOW_FCP", "1") == "1"

    @property
    def tapes(self) -> Path:
        return self.storage / "tapes"

    @property
    def working(self) -> Path:
        return self.storage / "working"

    @property
    def state(self) -> Path:
        return self.storage / "state"

    def ensure_dirs(self) -> None:
        for name in ("tapes", "working", "failed", "logs", "tmp", "state"):
            (self.storage / name).mkdir(parents=True, exist_ok=True)

