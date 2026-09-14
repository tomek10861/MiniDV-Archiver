from pathlib import Path

import minidv_archiver.sysload as sl


def test_parse_meminfo_converts_kib_to_bytes():
    text = "MemTotal:       48133228 kB\nMemFree:         2367744 kB\nMemAvailable:   46324628 kB\n"
    m = sl._parse_meminfo(text)
    assert m["MemTotal"] == 48133228 * 1024
    assert m["MemFree"] == 2367744 * 1024


def test_memory_computes_used_and_buff_cache(tmp_path, monkeypatch):
    (tmp_path / "meminfo").write_text(
        "MemTotal:       1000000 kB\n"
        "MemFree:         100000 kB\n"
        "MemAvailable:    700000 kB\n"
        "SwapTotal:       200000 kB\n"
        "SwapFree:        150000 kB\n")
    monkeypatch.setattr(sl, "PROC", tmp_path)
    m = sl.memory()
    assert m["total"] == 1000000 * 1024
    assert m["used"] == 300000 * 1024          # total - available
    assert m["buff_cache"] == 600000 * 1024    # total - free - used
    assert m["free"] == 100000 * 1024
    assert m["swap_total"] == 200000 * 1024
    assert m["swap_used"] == 50000 * 1024


def test_cpu_percent_computes_top_style_breakdown(monkeypatch):
    # fields: user nice system idle iowait irq softirq steal
    snapshots = iter([
        [100, 0, 50, 800, 20, 0, 0, 0],
        [150, 0, 70, 900, 30, 0, 0, 0],   # deltas: user+50 sys+20 idle+100 iowait+10 -> total 180
    ])
    monkeypatch.setattr(sl, "_read_cpu_line", lambda: next(snapshots))
    monkeypatch.setattr(sl.time, "sleep", lambda s: None)
    pct = sl.cpu_percent()
    assert pct == {"user": round(100 * 50 / 180, 1), "system": round(100 * 20 / 180, 1),
                   "iowait": round(100 * 10 / 180, 1), "idle": round(100 * 100 / 180, 1)}
    assert round(sum(pct.values())) == 100


def test_status_assembles_load_cores_cpu_memory(monkeypatch):
    monkeypatch.setattr(sl.os, "getloadavg", lambda: (1.104, 1.267, 0.961))
    monkeypatch.setattr(sl.os, "cpu_count", lambda: 8)
    monkeypatch.setattr(sl, "cpu_percent", lambda: {"user": 5.0, "system": 2.0, "iowait": 0.0, "idle": 93.0})
    monkeypatch.setattr(sl, "memory", lambda: {"total": 1, "used": 1, "buff_cache": 0, "free": 0,
                                               "swap_total": 0, "swap_used": 0})
    s = sl.status()
    assert s["load"] == {"1m": 1.1, "5m": 1.27, "15m": 0.96}
    assert s["cores"] == 8
    assert s["cpu"]["idle"] == 93.0
    assert s["memory"]["total"] == 1
