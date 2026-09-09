from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path


SAFE_COMMANDS = {
    "play": "00 20 c3 75",
    "stop": "00 20 c4 60",
    "pause": "00 20 c3 7d",
    "rewind": "00 20 c4 65",
    "ff": "00 20 c4 75",
}

TRANSPORT = {
    ("c3", "75"): "PLAYING",
    ("c3", "7d"): "PAUSED",
    ("c4", "60"): "STOPPED",
    ("c4", "65"): "REWINDING",
    ("c4", "45"): "REWINDING",
    ("c4", "75"): "FAST_FORWARDING",
}


class CameraError(RuntimeError):
    pass


def _response_bytes(output: str) -> list[str]:
    match = re.search(r"response:\s+\d+:\s+((?:[0-9a-fA-F]{2}\s+)+)", output)
    return match.group(1).lower().split() if match else []


FW_DEVICES = Path("/sys/bus/firewire/devices")
AVC_TAPE_SPECIFIER = "0x00a02d"  # AV/C tape recorder/player subunit -> a MiniDV deck/camcorder


def _read(path: Path) -> str:
    try:
        return path.read_text().strip()
    except OSError:
        return ""


class Camera:
    """MiniDV deck/camcorder AV/C transport over IEEE 1394 (`firewire-request`).

    Works with any AV/C tape device; no recording opcode exists in this module by design.
    """

    def __init__(self, guid: str = ""):
        self.guid = (guid or "").lower().removeprefix("0x")

    def _match_node(self) -> Path | None:
        """The camera's sysfs node — by configured GUID, or the first firewire device
        that exposes an AV/C tape unit."""
        for node in sorted(FW_DEVICES.glob("fw[0-9]*")):
            guid = _read(node / "guid").lower().removeprefix("0x")
            if not guid:
                continue
            match = guid == self.guid if self.guid else AVC_TAPE_SPECIFIER in _read(node / "units")
            if match:
                return node
        return None

    def device(self) -> Path | None:
        """Resolve the camera's /dev/fwN node."""
        node = self._match_node()
        if not node:
            return None
        dev = Path("/dev") / node.name
        return dev if dev.exists() else None

    def info(self) -> dict:
        dev = self.device()
        if not dev:
            return {"connected": False, "guid": self.guid}
        sysdev = FW_DEVICES / dev.name
        return {"connected": True, "device": str(dev),
                "guid": _read(sysdev / "guid").lower().removeprefix("0x") or self.guid,
                "vendor": _read(sysdev / "vendor") or None, "model": _read(sysdev / "model") or None,
                "model_name": _read(sysdev / "model_name") or None}

    def _fcp(self, payload: str, timeout: int = 5) -> list[str]:
        dev = self.device()
        if not dev:
            raise CameraError("camera not connected")
        cp = subprocess.run(["firewire-request", str(dev), "fcp", payload], text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
        values = _response_bytes(cp.stdout)
        if cp.returncode or not values:
            raise CameraError(cp.stdout.strip() or "no AV/C response")
        return values

    def command(self, name: str) -> dict:
        if name not in SAFE_COMMANDS:
            raise CameraError(f"unsafe or unknown command: {name}")
        values = self._fcp(SAFE_COMMANDS[name])
        if values[0] not in {"09", "0b", "0c"}:  # accepted, in transition, stable
            raise CameraError(f"camera rejected {name}: {' '.join(values)}")
        return {"command": name, "response": values}

    def status(self) -> dict:
        info = self.info()
        if not info["connected"]:
            return {**info, "transport": "NO_CAMERA", "timecode": None}
        try:
            values = self._fcp("01 20 d0 7f")
            transport = TRANSPORT.get(tuple(values[2:4]), "UNKNOWN")
            tc_values = self._fcp("01 20 51 71 ff ff ff ff")
            timecode = None
            if len(tc_values) >= 8 and tc_values[0] in {"0c", "0d"}:
                # AV/C returns BCD as frame, second, minute, hour.
                timecode = f"{tc_values[7]}:{tc_values[6]}:{tc_values[5]}:{tc_values[4]}"
            return {**info, "transport": transport, "timecode": timecode,
                    "raw_transport": values}
        except (CameraError, subprocess.TimeoutExpired) as exc:
            return {**info, "transport": "ERROR", "timecode": None, "error": str(exc)}

    def rewind_to_start(self, timeout: int, cancelled=lambda: False) -> None:
        initial_timecode = self.status().get("timecode")
        self.command("rewind")
        deadline = time.monotonic() + timeout
        saw_rewind = False
        stopped_polls = 0
        while time.monotonic() < deadline:
            if cancelled():
                self.command("stop")
                raise CameraError("rewind cancelled")
            status = self.status()
            state = status["transport"]
            saw_rewind |= state == "REWINDING"
            if saw_rewind and state == "STOPPED":
                return
            # Some early Sony decks stop immediately at BOT without exposing
            # an intermediate rewind state.
            if not saw_rewind and state == "STOPPED" and status.get("timecode") == initial_timecode:
                stopped_polls += 1
                if stopped_polls >= 3:
                    return
            else:
                stopped_polls = 0
            time.sleep(2)
        self.command("stop")
        raise CameraError("rewind timeout")
