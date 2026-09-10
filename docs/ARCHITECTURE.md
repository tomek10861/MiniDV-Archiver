# Architecture

The app is one codebase that runs either as a single process or as four
independently deployable pieces (Docker Compose, or bare-metal systemd). They share
nothing but the storage directory and a small SQLite job store.

```
                       ┌──────────────┐
   browser ── nginx ──►│     api      │  HTTP/JSON. Reads the filesystem + jobs.db.
    (ui = frontend/)   │ (no hardware)│  Writes = enqueue a row, or a guarded fs op.
                       └──────┬───────┘
                              │  state/jobs.db   (SQLite, WAL)
                   ┌──────────┴───────────┐
                   ▼                      ▼
            ┌────────────┐         ┌──────────────┐
            │  grabber   │         │  converter   │
            │ owns /dev/ │         │  no hardware │
            │   fw*      │         │              │
            └─────┬──────┘         └──────┬───────┘
                  │ writes                 │ reads working/<id>/capture001.dv
                  ▼                        ▼ writes tapes/<id>/
        working/<id>/capture001.dv  ──►  tapes/<id>/  (zstd master + proxies + json)
```

## Components

| component | command | container | touches | responsibility |
|---|---|---|---|---|
| **grabber** | `python -m minidv_archiver.grabber` | `privileged`, host `/dev` + `/sys` | `/dev/fw*` | claims `CREATED` capture jobs, runs `dvgrab`, end-of-tape detection, writes raw DV to `working/<id>/`, hands the job to the converter queue. Holds `state/grabber.lock` (one grabber). Also serves AV/C transport requests the api enqueues. |
| **converter** | `python -m minidv_archiver.converter` | plain, `/srv/minidv` volume, host `/sys:ro` | CPU/disk | processing (`split → zstd master + byte verify → H.264 proxies → tape.json / tape.sha256`), the on-demand build queue (share / concat / restore re-encodes, quality-scan / fingerprint), and the periodic tape/scene index refresh. |
| **api** | `python -m minidv_archiver.api` | plain, `/srv/minidv` volume, host `/sys:ro`, `expose: 8080` | filesystem, `jobs.db` | HTTP/JSON **and** the static `frontend/` (fallback). Read endpoints hit the filesystem / index directly. `capture/start|stop` write a job row; `tape/{play,stop,rewind}` enqueue a camera command; `rename` / `meta` / `reprobe` / delete are fast filesystem ops guarded by a `tape_busy()` check. |
| **ui** | `nginx:1.29-alpine` | publishes `:8088` | — | serves the mounted `frontend/` and proxies `/api` to `http://api:8080` over the compose network. `frontend/` is plain HTML/JS + vendored TailAdmin — no build step. A bare-metal equivalent is [`../deploy/nginx-minidv.conf`](../deploy/nginx-minidv.conf). |

Bare-metal split: `systemd/minidv-{grabber,converter,api}.service` run the same three
commands as units; nginx (`deploy/nginx-minidv.conf`) fronts the api. `Conflicts=`
with `minidv-archive.service` keeps you from running the single process too.

`python -m minidv_archiver.server` (and `minidv-archive.service`) runs **all of the
above in one process** (`role="all"`): the api in the main thread, the capture and
converter loops as daemon threads, the camera driven in-process. Same storage, same
`jobs.db` — you can stop the single unit and start the three split units against the
same data, or vice versa (`Conflicts=` keeps you from running both).

## The job store — `state/jobs.db`

SQLite in WAL mode with `busy_timeout`, so several processes read and write
concurrently:

- **jobs** — one row per tape. The full job dict lives in a JSON `payload`; `status`,
  `stage`, `cancel`, `updated_at` are lifted out for querying. `claim_next_capture()`
  / `claim_next_process()` move a row out of its queue inside one `BEGIN IMMEDIATE`
  transaction, so two processes never take the same job.
- **builds** — on-demand jobs keyed `<tape>/<token>`: share / concat / restore
  re-encodes and quality scans. Finished rows older than 6 h are pruned.
- **library** — the precomputed tape/scene index (`/api/tapes`, timeline,
  duplicates), refreshed by the converter; keyed by tape, skipped when unchanged.
- **cam_commands** — AV/C transport requests from the api to the grabber, with the
  response written back.

**Restart / crash recovery** (`reconcile()`, runs on every start): a capture never
resumes; a `process` job whose `working/<id>/capture001.dv` still exists is
re-queued (its half-written `tapes/<id>/` is dropped and redone from the raw DV); if
`tapes/<id>/tape.json` already exists the job is marked `COMPLETED`; otherwise
`ERROR`.

**Migration**: on first start, an old `state/jobs.json` is imported once and renamed
`state/jobs.json.imported`. Nothing under `tapes/` is touched — the archive format
(`tape.json`, `<scene>.json`, `tape.sha256`, `thumbnails/`) and the `.dv.zst`
masters are byte-for-byte unchanged.

## Data-safety notes

- The archive under `tapes/` is written once, verified (`zstd -t` + stream-decompress
  + sha256 compare), then only ever read. Splitting the services changes only the
  orchestration, never a master.
- Every mutating library op (delete tape / delete scenes / rename / edit metadata)
  checks `store.tape_busy(tape_id)` first, so it cannot race a capture or a
  conversion running in another process.
- `sha256sum -c tape.sha256` inside any tape directory verifies it end to end,
  before and after an upgrade.
