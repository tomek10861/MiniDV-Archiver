from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _default_recording_tz() -> str:
    """The DV VAUX recording date has no timezone of its own — it's whatever the
    camera's clock was set to, almost always local (wall-clock) time. MINIDV_TZ (or
    the generic TZ) names that zone; a container that only has bare UTC (the common
    Docker default, since nothing sets the host's real zone inside it) is never
    actually where the camera was, so that specific case falls back to Warsaw."""
    tz = os.getenv("MINIDV_DEFAULT_RECORDING_TZ") or os.getenv("TZ") or "Europe/Warsaw"
    return "Europe/Warsaw" if tz.upper() in ("UTC", "GMT", "ETC/UTC") else tz


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
    # Optional per-job additions to a restore build (both off unless the operator
    # checks a box): 2-pass libvidstab stabilization and a sharper GPU
    # (libplacebo/Vulkan) upscale — not a neural model, but a noticeably better
    # kernel than plain bicubic/lanczos, and fast on modest hardware. Needs
    # /dev/dri passed into the converter container.
    # shakiness: how hard pass 1 looks for shake (1-10, vidstab default 5).
    # smoothing: how strongly pass 2 flattens the camera path (vidstab default 15;
    # higher = steadier but crops in more / can warp on deliberate pans).
    restore_stabilize_shakiness: int = int(os.getenv("MINIDV_STABILIZE_SHAKINESS", "8"))
    restore_stabilize_smoothing: int = int(os.getenv("MINIDV_STABILIZE_SMOOTHING", "20"))
    restore_upscale_factor: int = int(os.getenv("MINIDV_UPSCALE_FACTOR", "2"))
    # ewa_lanczos4sharpest: libplacebo's sharpest built-in kernel (sharper than the
    # ewa_lanczossharp this defaulted to before) — more prone to ringing/haloing on
    # very noisy source than a softer kernel, but MiniDV footage is soft to begin
    # with and reads better sharpened.
    restore_upscale_scaler: str = os.getenv("MINIDV_UPSCALE_SCALER", "ewa_lanczos4sharpest")
    # Timezone the camera's clock was (almost certainly) set to, for turning a naive
    # DV VAUX recording date into a real UTC instant before it goes into an MP4's
    # creation_time — overridable per capture (operator knows a specific tape was
    # filmed abroad), see Engine.start()'s `timezone` param.
    default_recording_tz: str = _default_recording_tz()
    bind: str = os.getenv("MINIDV_BIND", "0.0.0.0")
    port: int = int(os.getenv("MINIDV_PORT", "8080"))
    # How often the background pass refreshes the tape/scene index (state/jobs.db);
    # unchanged tapes are skipped, so this stays cheap even with thousands of them.
    index_interval: int = int(os.getenv("MINIDV_INDEX_INTERVAL", "300"))
    # AV/C transport control (auto PLAY/STOP/REW). On by default — most decks and
    # camcorders handle it fine. Set MINIDV_ALLOW_FCP=0 for cameras whose AV/C stack
    # is flaky and resets the 1394 bus on FCP (e.g. Sony DCR-PC2E) -> manual PLAY/STOP.
    allow_fcp: bool = os.getenv("MINIDV_ALLOW_FCP", "1") == "1"
    # Face recognition ("Osoby"). Needs opencv-python-headless + numpy (in the
    # Docker image; `pip install .[faces]` on bare metal). The daily scan starts at
    # MINIDV_FACES_SCAN_AT local time (default_recording_tz) and only picks up
    # scenes not scanned yet; empty disables the schedule (manual scans still work).
    faces_enabled: bool = os.getenv("MINIDV_FACES", "1") == "1"
    faces_scan_at: str = os.getenv("MINIDV_FACES_SCAN_AT", "03:00")
    # where the two ONNX models live (downloaded + sha256-checked on first scan if
    # missing); empty -> <storage>/state/models
    faces_models: str = os.getenv("MINIDV_FACES_MODELS", "")
    faces_threads: int = int(os.getenv("MINIDV_FACES_THREADS", "1"))
    # per second of video the first frame whose sharpness (variance of the
    # Laplacian) reaches this is analysed; blurrier frames are skipped
    faces_blur: float = float(os.getenv("MINIDV_FACES_BLUR", "40"))
    faces_min_size: int = int(os.getenv("MINIDV_FACES_MIN_SIZE", "36"))   # px, smaller faces ignored
    faces_score: float = float(os.getenv("MINIDV_FACES_SCORE", "0.8"))   # detector confidence
    # cosine similarity for "same person" (SFace's published threshold is 0.363)
    faces_match: float = float(os.getenv("MINIDV_FACES_MATCH", "0.40"))
    # Refinement ("dokładna analiza") + re-clustering, ArcFace cosine. Tuned on real
    # DV footage against the operator's own corrections: only faces at least this
    # frontal (0..1, from the eye/nose landmarks) build groups; groups join while
    # their *average* similarity >= faces_cluster_t; weaker faces (profile/blur) only
    # join an existing group at >= faces_attach_t and a clear margin over the
    # runner-up. Lower = fewer split people but more mixed ones.
    faces_core_frontal: float = float(os.getenv("MINIDV_FACES_CORE_FRONTAL", "0.4"))
    faces_cluster_t: float = float(os.getenv("MINIDV_FACES_CLUSTER", "0.24"))
    faces_attach_t: float = float(os.getenv("MINIDV_FACES_ATTACH", "0.32"))
    faces_attach_margin: float = float(os.getenv("MINIDV_FACES_ATTACH_MARGIN", "0.05"))
    # a frontal face the operator left in a person is still dropped from it when its
    # average similarity to that person is below this ("doesn't fit")
    faces_reject_t: float = float(os.getenv("MINIDV_FACES_REJECT", "0.10"))

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
        for name in ("tapes", "working", "failed", "logs", "tmp", "state", "faces"):
            (self.storage / name).mkdir(parents=True, exist_ok=True)

