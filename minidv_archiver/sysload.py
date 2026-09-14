"""Host system load for the dashboard footer — pure stdlib, no subprocess, no deps.

Containers see the *host* kernel's /proc/{loadavg,meminfo,stat} and CPU count on a
standard (non-pid-isolated) Docker setup — cgroups limit what a container can use,
but don't virtualize what these files report — so reading them from inside any of
the app's containers already reflects true host-wide load, exactly what an
operator watching this dashboard wants (not one container's slice of it).
"""
from __future__ import annotations

import os
import time
from pathlib import Path

PROC = Path("/proc")

# top's %Cpu(s) line folds these the same way: nice into user, irq/softirq/steal
# (their own share is usually ~0) into system.
_CPU_FIELDS = ["user", "nice", "system", "idle", "iowait", "irq", "softirq", "steal"]


def _parse_meminfo(text: str) -> dict[str, int]:
    """{key: bytes}; /proc/meminfo reports every value in KiB."""
    out = {}
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        parts = rest.strip().split()
        if key and parts:
            out[key] = int(parts[0]) * 1024
    return out


def memory() -> dict:
    m = _parse_meminfo((PROC / "meminfo").read_text())
    total = m.get("MemTotal", 0)
    available = m.get("MemAvailable", m.get("MemFree", 0))
    free = m.get("MemFree", 0)
    used = max(0, total - available)
    # "buff/cache" the way `free` shows it: reclaimable, so not counted as "used"
    buff_cache = max(0, total - free - used)
    swap_total = m.get("SwapTotal", 0)
    swap_used = max(0, swap_total - m.get("SwapFree", 0))
    return {"total": total, "used": used, "buff_cache": buff_cache, "free": free,
            "swap_total": swap_total, "swap_used": swap_used}


def _read_cpu_line() -> list[int]:
    with (PROC / "stat").open() as f:
        first = f.readline()
    return [int(x) for x in first.split()[1:1 + len(_CPU_FIELDS)]]


def cpu_percent(sample_seconds: float = 0.15) -> dict:
    """top's aggregate %Cpu(s) breakdown, sampled over a short window. Blocks the
    calling thread for sample_seconds — fine here, each HTTP request already runs
    on its own thread (ThreadingHTTPServer)."""
    a = _read_cpu_line()
    time.sleep(sample_seconds)
    b = _read_cpu_line()
    deltas = dict(zip(_CPU_FIELDS, (y - x for x, y in zip(a, b))))
    total = sum(deltas.values()) or 1
    pct = lambda v: round(100 * v / total, 1)
    return {"user": pct(deltas["user"] + deltas["nice"]),
            "system": pct(deltas["system"] + deltas["irq"] + deltas["softirq"] + deltas["steal"]),
            "iowait": pct(deltas["iowait"]), "idle": pct(deltas["idle"])}


def status() -> dict:
    load1, load5, load15 = os.getloadavg()
    return {"load": {"1m": round(load1, 2), "5m": round(load5, 2), "15m": round(load15, 2)},
            "cores": os.cpu_count() or 1, "cpu": cpu_percent(), "memory": memory()}
