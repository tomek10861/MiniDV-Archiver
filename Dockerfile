# One image, three roles. grabber / converter / api all run `python -m
# minidv_archiver.<role>` from here; compose picks the command per service.
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        dvgrab ffmpeg zstd linux-firewire-utils util-linux \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY minidv_archiver/ ./minidv_archiver/
COPY frontend/ ./frontend/
COPY pyproject.toml ./

ENV PYTHONUNBUFFERED=1 \
    MINIDV_STORAGE=/srv/minidv

# default; every compose service overrides this
CMD ["python", "-m", "minidv_archiver.server"]
