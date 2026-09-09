from __future__ import annotations

import argparse
import json
from pathlib import Path

from .camera import Camera
from .config import Config
from .engine import Engine
from .server import main as serve


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("serve")
    control = sub.add_parser("camera")
    control.add_argument("action", choices=["status", "play", "stop", "pause", "rewind", "ff"])
    process = sub.add_parser("process")
    process.add_argument("input", type=Path)
    process.add_argument("--tape-id", required=True)
    args = parser.parse_args()
    config = Config()
    if args.command == "serve":
        serve()
    elif args.command == "camera":
        camera = Camera(config.camera_guid)
        print(json.dumps(camera.status() if args.action == "status" else camera.command(args.action), indent=2))
    else:
        print(json.dumps(Engine(config).process_existing(args.input, args.tape_id), indent=2))


if __name__ == "__main__":
    main()
