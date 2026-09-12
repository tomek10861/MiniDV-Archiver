# MiniDV Archiver

**English** · [Polski](README.pl.md)

Lossless capture and archival of MiniDV / DV tapes over FireWire (IEEE 1394), with a
web dashboard for reviewing what you captured.

The **master is always the raw DV bitstream** — captured with `dvgrab`, split into
scenes on the DV timecode/recording-date breaks, stored as `zstd`-compressed `.dv`
and verified byte-for-byte after compression. Everything else (H.264 proxies, the
whole-tape review file, thumbnails, JSON metadata) is a derivative you can regenerate.

**The dashboard**

![The dashboard — capture panel and status cards](docs/dashboard.png)

**The timeline** — year → month → scene, like iPhone Photos

![The timeline view](docs/timeline.png)

> UI ships in English and Polish (switch it in the header). The API and the
> `docs/` deep-dives are English. The dashboard is a
> [TailAdmin](https://github.com/TailAdmin/tailadmin-free-tailwind-dashboard-template)
> layout (MIT) — vendored under `frontend/vendor/`, so **no build step is needed to
> run**. Everything else is configurable via environment variables.

---

## What it does

- **Capture** raw DV from any AV/C MiniDV camcorder or deck (`dvgrab -noavc`), with
  automatic transport control (PLAY/STOP/REW over AV/C) or a fully manual mode.
- **End-of-tape detection** that tolerates blank stretches: keeps waiting (and
  relaunches `dvgrab` if it quits on signal loss) through gaps, stops shortly after
  the recorded material actually ends, and concatenates the segments losslessly.
- **Scene split** on DV discontinuities (`dvgrab -autosplit`).
- **Verified archival**: `sha256` of the raw scene, `zstd` compress, `zstd -t`,
  stream-decompress and compare the hash — the original `.dv` is only deleted once
  the `.dv.zst` reproduces it exactly.
- **Derivatives**: per-scene H.264/AAC MP4 proxy, one continuous `tape.mp4` (stream
  copy of the proxies), thumbnails, and rich `*.json` (timecode, recording date from
  VAUX, interlacing, audio layout, dropped-frame markers). Container metadata
  (`creation_time`, title, comment) is stamped from the tape so a downloaded file
  still knows where it came from.
- **Concurrent pipeline**: capture holds the FireWire slot exclusively; splitting +
  compression + encoding run in a background queue, so you can start the next tape
  while the previous one is still processing.
- **Runs as one process or four containers**: `grabber` (FireWire), `converter`
  (background processing / re-encodes), `api` (HTTP/JSON) and an `nginx` `ui`
  coordinate through a small SQLite job store (`state/jobs.db`), so you can restart
  the api or move the converter to another box without interrupting a capture. The
  same code also runs as a single process. See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).
- **Dashboard**: live capture status, background task list (captures + queued
  re-encodes / scans) with logs, a tape browser with per-scene thumbnails, in-page
  video preview, whole-tape playback with click-to-seek chapters, and downloads
  (master `.dv.zst`, proxy, JSON). English / Polish, switchable in the header.
- **Timeline & metadata**: a per-tape/scene index (refreshed in the background)
  powers an iPhone-Photos-style year → month → scene browser; rename tapes and edit
  labels / recording dates from the UI.
- **Duplicate detection**: an optional quality scan records per-scene decode-error
  counts and a content fingerprint; the *Duplicates* view groups the same recording
  across separate captures of one tape and marks the version with fewer errors so
  you can keep the cleanest and delete the rest.
- **Share export**: an on-demand "⬇ FB" button re-encodes a scene (or the whole
  tape) to a ~90 MB, same-resolution MP4 for Messenger/Facebook, then hands you the
  file. Temporary, one at a time, auto-pruned.
- **Restore variant**: an optional "🧹 Restore" button builds a denoise/repair MP4
  **decoded from the DV master** (not the proxy) — `bwdif` deinterlace with the
  probed field parity, then `atadenoise` + `deblock` (whole chain configurable via
  `MINIDV_RESTORE_FILTERS`). Master and normal proxy are never touched.

Never sends a `RECORD` opcode. The tape is treated as read-only.

## Requirements

The host is always **Linux with the `firewire_ohci` / `firewire_core` kernel stack**
and a working OHCI 1394 controller (`/dev/fw*`), plus a MiniDV camcorder or deck
with a DV/i.LINK port, in **PLAYER / VCR** mode.

- **Docker** (recommended): Docker Engine + Compose. `dvgrab`, `ffmpeg`, `zstd`,
  `linux-firewire-utils` all ship in the image — nothing else to install.
- **Bare-metal**: Python **3.11+** (standard library only, no pip deps) and those
  tools on `PATH`:

  ```bash
  sudo apt install dvgrab ffmpeg zstd linux-firewire-utils util-linux python3
  ```

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
| `converter` | — | scene split, zstd + byte-verify, proxies, re-encodes, quality scans |
| `api` | `expose: 8080` (not published) | HTTP/JSON; reads the filesystem + job store |
| `ui` | `nginx`, publishes `:8088` | serves `frontend/`, proxies `/api` → `api:8080` |

Restart any one without touching the others (`docker compose restart api`). Only
`ui` publishes a port — there is **no authentication by default**, so firewall
`:8088` to a trusted LAN, or turn on HTTP basic auth for an internet-facing
deployment (e.g. behind a Cloudflare Tunnel) — see [`docker/.htpasswd.example`](docker/.htpasswd.example);
off by default, nothing to configure otherwise. `grabber` is `privileged` because
the camera's `/dev/fw*` node is hot-plugged; it and the api/converter also mount
host `/sys` read-only so `camera.info()` can resolve the FireWire node.

### Other ways to run

```bash
sudo ./scripts/install.sh            # systemd, single process on :8080
sudo ./scripts/install.sh --split    # systemd, bare-metal grabber + converter + api + nginx
MINIDV_STORAGE=$HOME/minidv python3 -m minidv_archiver.server   # no install, single process
```

`scripts/install.sh` copies the app to `/opt/minidv-archive`, installs the udev
rule (`systemd/99-minidv-firewire.rules`) and the unit(s), and starts them; edit
`/etc/minidv-archive.env` for configuration. The systemd split units and the
bare-metal nginx config (`deploy/nginx-minidv.conf`) are the no-Docker equivalent
of the four containers — see [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## Configuration

All via environment variables — see [`.env.example`](.env.example) for the full list
with defaults. The ones you are most likely to touch:

With Docker, put them in `.env` (compose reads it). With systemd they go in
`/etc/minidv-archive.env`.

| Variable | Default | Meaning |
|---|---|---|
| `MINIDV_STORAGE` | `/srv/minidv` | archive root (bind-mounted into every container) |
| `MINIDV_CAMERA_GUID` | *(auto)* | empty = first AV/C tape device on the bus; set a GUID if several are connected |
| `MINIDV_ALLOW_FCP` | `1` | `1` = drive transport over AV/C; `0` = manual PLAY/STOP on the camera |
| `MINIDV_MP4_PRESET` | `medium` | x264 preset for proxies (`veryfast` is ~4–6× quicker, larger files) |
| `MINIDV_SHARE_MAX_MB` | `90` | target size for the "share" re-encode |
| `MINIDV_RESTORE_FILTERS` | `bwdif…,atadenoise,deblock…` | ffmpeg `-vf` chain for the "restore" variant |
| `MINIDV_INDEX_INTERVAL` | `300` | seconds between background refreshes of the tape/scene index |
| `MINIDV_BIND` / `MINIDV_PORT` | `0.0.0.0` / `8080` | api HTTP listener (inside its container) |

## Usage

1. Put the tape in, switch the camera to **PLAYER / VCR**, connect FireWire.
2. Open the dashboard (`:8088` with Docker, `:8080` single-process) → **New
   capture** → *Start* (the tape id is auto-assigned, e.g. `TAPE-0001`).
3. **Auto mode** (`MINIDV_ALLOW_FCP=1`): it rewinds and presses PLAY for you.
   **Manual mode**: wait for `WAITING FOR PLAY`, press **PLAY** on the camera; when
   the material ends press **STOP** (or it stops itself after the blank-tail timeout).
4. The capture hands off to the background pipeline; start the next tape whenever
   you like. Browse results under **Tapes** / **Timeline**.

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

A complete sample archive (one real ~6 s scene, ~20 MB) lives in
[`examples/demo-tape/`](examples/demo-tape/) — copy it into
`$MINIDV_STORAGE/tapes/` to see it in the dashboard.

## API

JSON over HTTP (served by the `api` process; `nginx` proxies `/api` to it).

**`GET`**
`/api/status` · `/api/storage` · `/api/jobs` · `/api/jobs/{id}` ·
`/api/tapes` (from the index) · `/api/tapes/{id}` · `/api/tapes/{id}/scenes` ·
`/api/tapes/{id}/scenes/{scene_id}` ·
`/api/timeline` · `/api/timeline/{year}/{month}` · `/api/duplicates` ·
`/api/tapes/{id}/files/{name}` (archive files; `Range` for MP4; `?dl=1` forces
download) ·
`/api/tapes/{id}/compressed/{token}.mp4` — serves an on-demand build; `token` is
`TAPE`, `<scene_id>`, `<scene_id>-RES` (restore), or `SEL-<hash>[-FB|-RES]` ·
`/api/playlist/build/{token}.mp4` — serves a cross-tape build (see below).

**`POST`**
`/api/capture/start` `{tape_id?, rewind?, duration?, manual_transport?}` ·
`/api/capture/stop` `{tape_id?}` ·
`/api/tape/{play,stop,rewind}` (409 unless `MINIDV_ALLOW_FCP=1`) ·
`/api/tapes/{id}/scenes/{scene}/compress` `{restore?}` ·
`/api/tapes/{id}/compress` `{scenes?, share?, restore?}` (no body = whole tape) ·
`/api/tapes/{id}/rename` `{new_id}` ·
`/api/tapes/{id}/meta` `{label?, recording_date?}` ·
`/api/tapes/{id}/reprobe` `{force?}` (quality scan / fingerprint) ·
`/api/playlist/build` `{items: [{tape_id, scene_id}, ...], title?}` — join scenes
from one or several tapes (in that order) into a single MP4, e.g. a multi-select
from the timeline. Call it again with the same body to poll status
(`QUEUED`/`RUNNING`/`READY`), same pattern as the other on-demand builds above ·
`/api/tapes/{id}/scenes/delete` `{scenes: [...]}` — deleting scenes rebuilds the
whole-tape proxy, so this runs as a background build too (poll the same way)
instead of blocking the request, which used to time out on a tape with hundreds
of scenes.

**`DELETE`**
`/api/tapes/{id}` ·
`/api/jobs/{tape_id}` (clears a finished/errored job row; refuses a running or
already-archived one).

## Camera compatibility & FireWire notes

> **⚠ Cabling — the 6-pin ↔ 4-pin adapter can kill a port.** The 6-pin FireWire
> connector carries bus power (~8–30 V); the 4-pin i.LINK connector does not. A
> cheap or miswired 6→4 adapter cable, or hot-plugging one, can push power into a
> port that was never meant to receive it and **fry the camera's or the PC's
> FireWire connector** (this happened to an old camcorder here). Rules:
>
> - **PC off** → plug the **6-pin** end into the PC first.
> - **Camera off** → plug the **4-pin** end into the camera.
> - Only then power both on. **Never hot-plug either end.**
> - Prefer a plain **4-pin ↔ 4-pin** cable when the PC card has a 4-pin port, or a
>   known-good adapter; avoid the bargain-bin ones.

Most DV decks and camcorders drive fine over AV/C. Some older i.LINK PHYs are
marginal and drop off the bus, or reset it on FCP transactions. If you hit that:

- Set `MINIDV_ALLOW_FCP=0` and use manual PLAY/STOP.
- Power-cycle the camera (unplug its adapter ~30–60 s) to recover a stuck PHY —
  this cannot be done in software.
- Keep the FireWire controller and any PCIe-to-PCI bridge out of runtime PM
  (`ATTR{power/control}="on"`) — `systemd/99-minidv-firewire.rules` has an example
  keyed to VIA VT6306 + ASMedia ASM1083.

The **Sony DCR-PC2E** is a worked example of all of the above; see
[`docs/FIREWIRE.md`](docs/FIREWIRE.md).

More docs: [architecture](docs/ARCHITECTURE.md) · [archive format](docs/ARCHIVE_FORMAT.md) ·
[DV metadata](docs/DV_METADATA.md) · [storage](docs/STORAGE.md).

## Development

```bash
python3 -m pip install -e ".[dev]"
python3 -m pytest -q
```

Standard library only; the app talks to `dvgrab` / `ffmpeg` / `zstd` as subprocesses.
The container image is rebuilt with `docker compose up -d --build` after any code
change.

The front-end is `frontend/index.html` + `frontend/app.js` + `frontend/i18n.js`
(vanilla JS, English/Polish) styled with the vendored TailAdmin CSS. After editing
markup or classes, rebuild the CSS:

```bash
scripts/build-css.sh          # needs Node + npm; rewrites frontend/vendor/tailadmin.css
```

Third-party front-end assets and their licenses are listed in
[`frontend/vendor/README.md`](frontend/vendor/README.md).

## License

MIT — see [LICENSE](LICENSE).
