"""API service entrypoint.

The HTTP/JSON layer only reads the filesystem and the shared job store; it never
touches FireWire. Mutating requests (capture start/stop, compress, rename, meta,
delete) become store rows the grabber / converter pick up, or fast filesystem
operations guarded by a store "tape busy" check.

In split deployments an nginx in front serves ``frontend/`` and proxies ``/api``
here; set ``MINIDV_BIND=127.0.0.1`` in the unit so only nginx can reach it.
"""
from __future__ import annotations

from .server import run


def main():
    run("api")


if __name__ == "__main__":
    main()
