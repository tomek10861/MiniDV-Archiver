"""The two ONNX models face recognition uses, and a sha256-checked fetch.

Standalone on purpose (stdlib only, no package imports): the Dockerfile copies
just this file into an early layer and runs it to bake the models into the image,
so the layer stays cached across code changes.

    python face_models.py /opt/face-models
"""
from __future__ import annotations

import hashlib
import shutil
import sys
import urllib.request
from pathlib import Path

TAG = "yunet-2023mar+sface-2021dec"          # fast scan
REFINE_TAG = "arcface-w600k-r50/1"         # refinement pass
_ZOO = "https://github.com/opencv/opencv_zoo/raw/main/models"
MODELS = {
    "detector": ("face_detection_yunet_2023mar.onnx",
                 f"{_ZOO}/face_detection_yunet/face_detection_yunet_2023mar.onnx",
                 "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4"),
    "recognizer": ("face_recognition_sface_2021dec.onnx",
                   f"{_ZOO}/face_recognition_sface/face_recognition_sface_2021dec.onnx",
                   "0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79"),
    # ArcFace R50 (insightface buffalo_l, WebFace600K) for the refinement pass —
    # much better than SFace on low-res DV faces. The insightface model weights are
    # licensed for non-commercial research use only (fine for a family archive).
    "arcface": ("w600k_r50.onnx",
                "https://huggingface.co/public-data/insightface/resolve/main/models/buffalo_l/w600k_r50.onnx",
                "4c06341c33c2ca1f86781dab0e829f88ad5b64be9fba56e56bc9ebdefc619e43"),
}


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch(dest: Path, log=lambda m: None) -> dict[str, Path]:
    """Ensure both models are in ``dest`` with the right checksum; download if not."""
    dest.mkdir(parents=True, exist_ok=True)
    out = {}
    for role, (name, url, digest) in MODELS.items():
        p = dest / name
        if not p.exists() or _sha256(p) != digest:
            log(f"pobieram model {name}")
            tmp = p.with_suffix(".part")
            with urllib.request.urlopen(url, timeout=300) as r, tmp.open("wb") as f:
                shutil.copyfileobj(r, f)
            if _sha256(tmp) != digest:
                tmp.unlink(missing_ok=True)
                raise RuntimeError(f"model {name}: niezgodna suma sha256")
            tmp.replace(p)
        out[role] = p
    return out


if __name__ == "__main__":
    for path in fetch(Path(sys.argv[1]), log=print).values():
        print(path)
