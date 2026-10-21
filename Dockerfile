# One image, three roles. grabber / converter / api all run `python -m
# minidv_archiver.<role>` from here; compose picks the command per service.
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        dvgrab ffmpeg zstd linux-firewire-utils util-linux \
        mesa-vulkan-drivers \
    && rm -rf /var/lib/apt/lists/*
# mesa-vulkan-drivers: lets ffmpeg's libplacebo filter (the "Napraw" upscale option)
# drive a real GPU over Vulkan. Needs /dev/dri passed into the container (converter
# service, see compose.yaml) — without a device, libplacebo just fails to init and
# that one build errors, everything else in this image is unaffected.

# Face recognition ("Osoby"): OpenCV's YuNet detector + SFace embedder for the fast
# scan, ArcFace (onnxruntime) for the refinement pass, all CPU only. The three ONNX
# models (~213 MB) are baked in and sha256-checked here, so the server never
# downloads anything at runtime (faces.ensure_models only fetches if missing).
RUN pip install --no-cache-dir opencv-python-headless==5.0.0.93 numpy==2.5.4 onnxruntime==1.31.0
COPY minidv_archiver/face_models.py /tmp/face_models.py
RUN python /tmp/face_models.py /opt/face-models && rm /tmp/face_models.py
ENV MINIDV_FACES_MODELS=/opt/face-models

WORKDIR /app
COPY minidv_archiver/ ./minidv_archiver/
COPY frontend/ ./frontend/
COPY pyproject.toml ./

ENV PYTHONUNBUFFERED=1 \
    MINIDV_STORAGE=/srv/minidv

# default; every compose service overrides this
CMD ["python", "-m", "minidv_archiver.server"]
