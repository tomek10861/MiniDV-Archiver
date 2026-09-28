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

WORKDIR /app
COPY minidv_archiver/ ./minidv_archiver/
COPY frontend/ ./frontend/
COPY pyproject.toml ./

ENV PYTHONUNBUFFERED=1 \
    MINIDV_STORAGE=/srv/minidv

# default; every compose service overrides this
CMD ["python", "-m", "minidv_archiver.server"]
