"""Converter service entrypoint.

No hardware. Constructs an Engine in the ``converter`` role, which starts the
processing worker (raw DV -> scene split -> zstd master + byte verification ->
H.264 proxies -> tape.json / tape.sha256) and the share/concat re-encode worker.
Both poll ``state/jobs.db`` for queued work, so this can run on a different host
than the grabber as long as it sees the same storage.
"""
from __future__ import annotations

import signal
import threading

from .config import Config
from .engine import Engine


def main():
    engine = Engine(Config(), role="converter")
    print(f"MiniDV converter draining {engine.config.state / 'jobs.db'}")
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    stop.wait()


if __name__ == "__main__":
    main()
