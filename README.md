# MiniDV Archiver

**English** · [Polski](README.pl.md)

[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)
![Docker Compose](https://img.shields.io/badge/docker-compose-2496ED)
![Linux · FireWire](https://img.shields.io/badge/linux-FireWire%20%2F%20IEEE%201394-lightgrey)
![UI: English · Polski](https://img.shields.io/badge/UI-English%20%C2%B7%20Polski-orange)

**Rescue your family's MiniDV tapes, without losing a single bit, and then actually find
things in them.**

Plug in a camcorder over FireWire and press *Start*. Each tape becomes a verified,
byte-exact archive split into scenes. You then browse it like the Photos app on a phone:
a year → month timeline, a People view that recognises faces, and one-click exports for
sharing or restoring old footage. Everything runs on your own Linux box, with no cloud
involved.

![The dashboard: capture panel and status cards](docs/dashboard.png)

<table>
<tr>
<td width="50%"><img src="docs/timeline.png" alt="Timeline: year → month → scene"></td>
<td>

**What you get**

- 🎞️ **Lossless capture**: the raw DV bitstream is kept, compressed with zstd, and
  verified byte for byte
- ✂️ **Automatic scene split** on recording breaks, with date and time read from the tape
- 🗓️ **Timeline** by recording date, across all tapes
- 🧑‍🤝‍🧑 **People**: on-device face recognition, so naming a face finds every recording
  of that person
- 🧹 **Restore**: deinterlace, denoise, deblock, plus optional stabilisation and upscaling
- ⬇️ **Share**: a ~90 MB Messenger-ready MP4, a single video joined from many tapes, or a ZIP
- 🐳 **One `docker compose up`**: four small services, bilingual web UI (EN / PL)

</td>
</tr>
</table>

---

**Contents:** [Features](#features) · [Quick start](#quick-start-docker) ·
[Requirements](#requirements) · [Configuration](#configuration) · [Usage](#usage) ·
[API](#api) · [FireWire notes](#camera-compatibility--firewire-notes) ·
[Development](#development)

## Features

### 🎞️ Capture: set it running and walk away

- **Works with any AV/C MiniDV camcorder or deck** through `dvgrab`. Transport is
  automatic (rewind, PLAY and STOP over AV/C), or fully manual for cameras whose AV/C
  stack misbehaves.
- **Smart end-of-tape detection.** It rides through blank stretches and signal loss,
  relaunching `dvgrab` and stitching the pieces back together losslessly. It stops
  shortly after the recorded material really ends.
- **Live preview**, with elapsed tape time and bytes captured while it runs.
- **Pipelined work.** Capture owns the FireWire port while splitting, compression and
  encoding run in a background queue, so you can start the next tape straight away.
  Processing shows per-scene progress and an ETA.
- **Camera clock timezone, per capture.** The DV recording date has no timezone, so you
  pick the zone the camera's clock was set to (for a holiday abroad, for example) and
  every `creation_time` comes out as the correct real moment.
- **Workspaces.** Keep several people's or families' tapes apart: each workspace has its
  own tapes, timeline, duplicates and people.

### 🔒 Archive: a master you can trust in 30 years

- **The raw DV is the master.** For each scene: `sha256` → `zstd` → `zstd -t` →
  stream-decompress → compare. The original `.dv` is deleted only after the `.dv.zst`
  reproduces it exactly.
- **Derivatives you can always regenerate**:
  - per-scene H.264/AAC proxies;
  - one continuous `tape.mp4`;
  - thumbnails;
  - rich per-scene JSON (timecode, VAUX recording date, interlacing, audio layout,
    dropped frames).
- **Files that remember where they came from.** MP4s carry `creation_time`, a title and
  a comment taken from the tape. *📅 Fix recording date* backfills these into older files.
- **`sha256sum -c tape.sha256`** verifies a whole tape folder end to end. The format is
  documented in [`docs/ARCHIVE_FORMAT.md`](docs/ARCHIVE_FORMAT.md).
- **The tape is treated as read-only.** The `RECORD` opcode is never sent.

### 🗓️ Browse: Photos-style, not folder-style

- **Timeline**: year → month → scene. Scenes from different tapes shot on the same day
  are interleaved by the actual time of recording.
- **Tapes**: an album grid, per-scene cards, in-page preview, and whole-tape playback
  with click-to-seek chapters.
- **List or grid view.** Tapping a tile opens a full-screen viewer with swipe and arrow
  keys.
- **Housekeeping from the UI**: rename tapes, set labels and recording dates, delete
  scenes or tapes. Slow operations run as background tasks.
- **Duplicates.** A quality scan fingerprints every scene and counts its decode errors.
  The *Duplicates* view groups the same recording across separate captures of one tape,
  marks the cleanest version, and offers *✓ Keep best in every group*. False matches can
  be dismissed with *✕ Not a duplicate*.

### 🧑‍🤝‍🧑 People: face recognition that stays on your machine

- **Name a face once, and that person's page lists every recording they appear in.**
  Buttons like **▶ 0:12–0:20** start playback at the exact moment they're on screen.
- **Edited the way you'd expect from a phone**:
  - naming someone with an existing name merges the two;
  - *⇄ Same person as…* merges by picking from a list;
  - **✕** on a face means "not this person";
  - you can hide strangers.
- **Two passes.**
  1. A fast scan of new scenes (OpenCV YuNet + SFace). For each second of video it
     analyses the first frame that isn't blurred.
  2. A detailed pass goes back to every face it found, scores each view (frontal, sharp,
     large) and embeds the best few with ArcFace. It then regroups everyone from scratch:
     - two faces in the same frame are never the same person;
     - profile or blurry faces join someone only when it's a clear match;
     - **your merges, removals and names are hard rules** that regrouping never breaks.
- **Polite to the machine it runs on.**
  - The scan runs nightly at 03:00 or on demand, one scan at a time.
  - It appears in *Tasks* with a progress bar and an ETA, and can be stopped.
  - It pauses while a capture or conversion runs, uses one core at idle priority, and
    resumes where it stopped.
  - On a 2017 quad-core i7 the fast pass runs at about 10× real time.
- **Scoped by workspace.** Faces from different workspaces are never matched against each
  other.

### 🧹 Restore & ⬇️ share

- **🧹 Restore** builds a cleaned-up MP4 **decoded from the DV master**, not the proxy:
  - `bwdif` deinterlace using the probed field order;
  - `atadenoise` and `deblock` (the whole chain is configurable);
  - optional **stabilisation** (2-pass `libvidstab`);
  - optional **higher resolution**: ffmpeg's `libplacebo` upscaler over Vulkan, which
    needs a GPU at `/dev/dri`.
- **⬇ FB** re-encodes a scene, a selection or a whole tape to a ~90 MB same-resolution MP4
  for Messenger or Facebook.
- **🎬 Build one video** from scenes picked across several tapes, straight from the
  timeline, either as a lossless join or restored from the masters.
- **📦 ZIP** downloads a selection as separate files.
- **The master and the proxies are never modified.** Builds are temporary and pruned
  automatically.

### 🐳 Operations

- **Four containers, or one process.**
  - **`grabber`** handles FireWire.
  - **`converter`** handles processing, re-encodes and the face scan.
  - **`api`** serves HTTP/JSON.
  - **`ui`** is an nginx front end.

  They coordinate through a small SQLite job store, so you can restart the api or move
  the converter to another box mid-capture. See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).
- **Crash-safe.** On restart, half-finished conversions are re-queued from the raw DV.
- **Dashboard**: server load (CPU, memory, swap), free space with an estimate of how many
  hours of DV still fit, and a camera status card.
- **Optional HTTP basic auth** for internet-facing setups. It's off by default, so a fresh
  clone just works on a LAN.

## Quick start (Docker)

```bash
git clone https://github.com/tomek10861/MiniDV-Archiver
cd MiniDV-Archiver
cp .env.example .env          # optional; e.g. MINIDV_ALLOW_FCP=0 for a Sony DCR-PC2E
sudo mkdir -p /srv/minidv
docker compose up -d --build
# UI on http://<host>:8088
```

Four containers share `/srv/minidv` (and its `state/jobs.db`):

| service | container | does |
|---|---|---|
| `grabber` | `privileged`, host `/dev` + `/sys` | owns FireWire, runs `dvgrab` |
| `converter` | — | scene split, zstd + byte-verify, proxies, re-encodes, quality scans, face recognition |
| `api` | `expose: 8080` (not published) | HTTP/JSON; reads the filesystem + job store |
| `ui` | `nginx`, publishes `:8088` | serves `frontend/`, proxies `/api` → `api:8080` |

You can restart any one service without touching the others
(`docker compose restart api`).

Only `ui` publishes a port, and there is **no authentication by default**. Either keep
`:8088` behind a firewall on a trusted LAN, or turn on HTTP basic auth for an
internet-facing deployment (for example behind a Cloudflare Tunnel); see
[`docker/.htpasswd.example`](docker/.htpasswd.example).

`grabber` is `privileged` because the camera's `/dev/fw*` node is hot-plugged. It, the
api and the converter also mount the host's `/sys` read-only so `camera.info()` can
resolve the FireWire node.

### Other ways to run

```bash
sudo ./scripts/install.sh            # systemd, single process on :8080
sudo ./scripts/install.sh --split    # systemd, bare-metal grabber + converter + api + nginx
MINIDV_STORAGE=$HOME/minidv python3 -m minidv_archiver.server   # no install, single process
```

`scripts/install.sh` does the following:
- copies the app to `/opt/minidv-archive`;
- installs the udev rule (`systemd/99-minidv-firewire.rules`) and the systemd unit(s);
- starts them.

Configuration goes in `/etc/minidv-archive.env`. The systemd split units and the
bare-metal nginx config (`deploy/nginx-minidv.conf`) are the no-Docker equivalent of the
four containers.

## Requirements

The host is always **Linux with the `firewire_ohci` / `firewire_core` kernel stack** and
a working OHCI 1394 controller (`/dev/fw*`). You also need a MiniDV camcorder or deck
with a DV/i.LINK port, switched to **PLAYER / VCR** mode.

- **Docker** (recommended): Docker Engine + Compose. Everything ships in the image:
  - `dvgrab`, `ffmpeg`, `zstd`, `linux-firewire-utils`;
  - OpenCV, onnxruntime and the face models (~210 MB, checksum-verified at build time).
- **Bare-metal**: Python **3.11+** and these tools on `PATH`:

  ```bash
  sudo apt install dvgrab ffmpeg zstd linux-firewire-utils util-linux python3
  pip install ".[faces]"       # optional: People / face recognition
  ```

  The core uses only the Python standard library. Without the `faces` extra, the People
  view simply reports that face recognition isn't available.

## Configuration

Everything is set through environment variables: in `.env` with Docker, or in
`/etc/minidv-archive.env` with systemd. [`.env.example`](.env.example) has the full list
with defaults. The ones you're most likely to touch:

| Variable | Default | Meaning |
|---|---|---|
| `MINIDV_STORAGE` | `/srv/minidv` | archive root (bind-mounted into every container) |
| `MINIDV_CAMERA_GUID` | *(auto)* | empty = first AV/C tape device on the bus; set a GUID if several are connected |
| `MINIDV_ALLOW_FCP` | `1` | `1` = drive transport over AV/C; `0` = manual PLAY/STOP on the camera |
| `MINIDV_MP4_PRESET` | `medium` | x264 preset for proxies (`veryfast` is ~4–6× quicker, larger files) |
| `MINIDV_DEFAULT_RECORDING_TZ` | `Europe/Warsaw` | the zone the camera's clock was set to, for turning the DV date into a real UTC `creation_time`; overridable per capture |
| `MINIDV_SHARE_MAX_MB` | `90` | target size for the "share" re-encode |
| `MINIDV_RESTORE_FILTERS` | `bwdif…,atadenoise,deblock…` | ffmpeg `-vf` chain for "restore" |
| `MINIDV_STABILIZE_SHAKINESS` / `_SMOOTHING` | `8` / `20` | vidstab strength for the *Stabilization* checkbox |
| `MINIDV_UPSCALE_FACTOR` / `_SCALER` | `2` / `ewa_lanczos4sharpest` | the *Higher resolution* checkbox |
| `MINIDV_FACES` | `1` | `0` turns face recognition off |
| `MINIDV_FACES_SCAN_AT` | `03:00` | daily scan of new scenes (local time); empty = manual only |
| `MINIDV_FACES_THREADS` | `1` | CPU threads for the face scan (`2` is about twice as fast) |
| `MINIDV_FACES_CLUSTER` | `0.24` | how readily faces are grouped (lower = fewer split people, more mixed ones) |
| `MINIDV_INDEX_INTERVAL` | `300` | seconds between background refreshes of the tape/scene index |
| `MINIDV_BIND` / `MINIDV_PORT` | `0.0.0.0` / `8080` | api HTTP listener (inside its container) |

## Usage

1. Insert the tape, switch the camera to **PLAYER / VCR**, and connect FireWire (mind the
   [cabling warning](#camera-compatibility--firewire-notes)).
2. Open the dashboard (`:8088` with Docker, `:8080` single-process). Optionally pick a
   **workspace** and the **camera clock's timezone**, then **New capture** → *Start*. The
   tape id is assigned automatically, for example `TAPE-0001`.
3. **Auto mode** (`MINIDV_ALLOW_FCP=1`): it rewinds and presses PLAY for you.
   **Manual mode**: wait for `WAITING FOR PLAY`, then press **PLAY** on the camera. When
   the material ends, press **STOP**, or let it stop itself after the blank-tail timeout.
4. The capture hands off to the background pipeline, so you can start the next tape
   whenever you like. Browse the results under **Tapes**, **Timeline** and **People**.
   Background work is listed under **Tasks**.

### CLI

```bash
python3 -m minidv_archiver.cli camera status        # AV/C transport + timecode
python3 -m minidv_archiver.cli camera play|stop     # only if MINIDV_ALLOW_FCP=1
python3 -m minidv_archiver.cli process take.dv --tape-id TAPE-0007   # ingest an existing .dv
python3 -m minidv_archiver.cli serve                # single process (capture + converter + api)
python3 -m minidv_archiver.cli grabber              # split: FireWire capture only
python3 -m minidv_archiver.cli converter            # split: background processing / re-encodes
python3 -m minidv_archiver.cli api                  # split: HTTP/JSON only
```

### Verifying / restoring a master

```bash
zstd -t 0001_*.dv.zst                       # integrity of the archive
zstd -dc 0001_*.dv.zst > recovered.dv       # exact raw DV back
sha256sum -c tape.sha256                    # every file in the tape folder
```

[`examples/demo-tape/`](examples/demo-tape/) holds a complete sample archive: one real
scene of about 6 s, ~20 MB. Copy it into `$MINIDV_STORAGE/tapes/` to see it in the
dashboard.

## API

JSON over HTTP, served by the `api` process (`nginx` proxies `/api` to it). Every list
endpoint accepts `?workspace=<name>` and defaults to `"default"`. Long jobs
(re-encodes, scene deletion, scans) return `202` and are polled by repeating the same
call until they report `QUEUED` → `RUNNING` → `READY`.

<details>
<summary><b>Endpoint reference</b></summary>

**`GET`**
`/api/status` · `/api/storage` · `/api/sysload` · `/api/jobs` · `/api/jobs/{id}` ·
`/api/workspaces` (`"default"` always first) ·
`/api/tapes` (from the index) · `/api/tapes/{id}` · `/api/tapes/{id}/scenes` ·
`/api/tapes/{id}/scenes/{scene_id}` ·
`/api/timeline` · `/api/timeline/{year}/{month}` · `/api/duplicates` ·
`/api/tapes/{id}/files/{name}` (archive files; `Range` for MP4; `?dl=1` forces download) ·
`/api/tapes/{id}/compressed/{token}.mp4|.zip` serves an on-demand build. `token` is
`TAPE`, `<scene_id>`, `<scene_id>-RES[-STAB][-UP]` (restore, +stabilize, +upscale), or
`SEL-<hash>[-FB|-RES[-STAB][-UP]|-ZIP]` ·
`/api/playlist/build/{token}.mp4|.zip` serves a cross-tape build ·
`/api/faces/status` (scan progress, scanned/total scenes) · `/api/persons` ·
`/api/persons/{id}` (faces + recordings with on-screen time ranges) ·
`/api/faces/thumb/{face_id}.jpg`.

**`POST`**
`/api/capture/start` `{tape_id?, rewind?, duration?, manual_transport?, timezone?, workspace?}`:
`timezone` is an IANA zone for a tape filmed somewhere other than
`MINIDV_DEFAULT_RECORDING_TZ` ·
`/api/capture/stop` `{tape_id?}` ·
`/api/tape/{play,stop,rewind}` (409 unless `MINIDV_ALLOW_FCP=1`) ·
`/api/workspaces` `{name}` ·
`/api/tapes/{id}/scenes/{scene}/compress` `{restore?, stabilize?, upscale?}` ·
`/api/tapes/{id}/compress` `{scenes?, share?, restore?, stabilize?, upscale?, archive?}`:
no body means the whole tape; `archive: true` zips each selected scene separately ·
`/api/playlist/build` `{items: [{tape_id, scene_id}, …], title?, restore?, stabilize?, upscale?, archive?}`
joins scenes from several tapes, in order ·
`/api/tapes/{id}/rename` `{new_id}` ·
`/api/tapes/{id}/meta` `{label?, recording_date?}` ·
`/api/tapes/{id}/reprobe` `{force?}` (quality scan / fingerprint) ·
`/api/tapes/{id}/backfill-meta` `{force?}` (write `creation_time`/`date` onto MP4s) ·
`/api/tapes/{id}/scenes/delete` `{scenes: […]}` (runs in the background) ·
`/api/duplicates/dismiss` `{members: [{tape_id, scene_id}, …]}` ·
`/api/faces/scan` (409 if one is already running) · `/api/faces/scan/cancel` ·
`/api/persons/{id}` `{name?, hidden?}` (a name that already exists merges) ·
`/api/persons/{id}/merge` `{into}` ·
`/api/faces/tracks/{face_id}/move` `{person_id | null}` (`null` = "not this person").

**`DELETE`**
`/api/tapes/{id}` · `/api/jobs/{tape_id}` (clears a finished or errored job row; refuses
a running or already-archived one).

</details>

## Camera compatibility & FireWire notes

> **⚠ Cabling: a 6-pin ↔ 4-pin adapter can kill a port.** The 6-pin FireWire connector
> carries bus power (~8–30 V) and the 4-pin i.LINK one does not. A cheap or miswired
> adapter, or hot-plugging one, can push power into a port that was never meant to take
> it and **fry the camera's or the PC's FireWire connector**. That has happened to an old
> camcorder here.
>
> - **PC off** → plug the **6-pin** end into the PC first.
> - **Camera off** → plug the **4-pin** end into the camera.
> - Only then power both on. **Never hot-plug either end.**
> - Prefer a plain **4-pin ↔ 4-pin** cable when the PC card has a 4-pin port, or a
>   known-good adapter.

Most DV decks and camcorders work fine over AV/C. Some older i.LINK PHYs are marginal:
they drop off the bus, or reset it on FCP transactions. If you hit that:

- Set `MINIDV_ALLOW_FCP=0` and use manual PLAY/STOP.
- Power-cycle the camera (unplug its adapter for 30–60 s) to recover a stuck PHY. This
  can't be done in software.
- Keep the FireWire controller and any PCIe-to-PCI bridge out of runtime power
  management (`ATTR{power/control}="on"`). `systemd/99-minidv-firewire.rules` has an
  example for VIA VT6306 + ASMedia ASM1083.

The **Sony DCR-PC2E** is a worked example of all of the above; see
[`docs/FIREWIRE.md`](docs/FIREWIRE.md).

More docs: [architecture](docs/ARCHITECTURE.md) · [archive format](docs/ARCHIVE_FORMAT.md) ·
[DV metadata](docs/DV_METADATA.md) · [storage](docs/STORAGE.md).

## Development

```bash
python3 -m pip install -e ".[dev,faces]"
python3 -m pytest -q
```

The app drives `dvgrab`, `ffmpeg` and `zstd` as subprocesses. Face recognition is the
only part with Python dependencies (OpenCV, numpy, onnxruntime), and its tests skip
cleanly without them. After a code change, rebuild the image with
`docker compose up -d --build`.

The front end is plain JavaScript with no build step: `frontend/index.html`,
`frontend/app.js` and `frontend/i18n.js` (English/Polish), styled with vendored
TailAdmin CSS. After editing markup or classes, rebuild the CSS:

```bash
scripts/build-css.sh          # needs Node + npm; rewrites frontend/vendor/tailadmin.css
```

Third-party front-end assets and their licenses are listed in
[`frontend/vendor/README.md`](frontend/vendor/README.md).

## License

MIT; see [LICENSE](LICENSE).

Bundled and downloaded third-party pieces keep their own licenses:
- TailAdmin and Alpine.js: MIT;
- the OpenCV Zoo models YuNet and SFace: MIT / Apache-2.0;
- the ArcFace weights (insightface `buffalo_l`): **non-commercial research use only**.
  This is fine for a family archive; check it before any commercial use.
