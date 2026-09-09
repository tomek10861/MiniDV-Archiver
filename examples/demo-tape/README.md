# Sample archive — `DEMO-MINIDV`

A complete, verified archive of **one real ~6-second scene** (149 frames) taken
straight from `TAPE-0001`, committed so you can see the on-disk format without
capturing anything.

```
tape.json                                  tape manifest (scene list, label, recording_date, proxy_full)
tape.sha256                                 checksums for every file below
0001_2067-02-15_22-26-25.dv.zst           the master — raw DV, zstd-compressed, byte-verified (copied verbatim from TAPE-0001)
0001_2067-02-15_22-26-25.mp4              H.264/AAC proxy for review (crf 28 here to keep the repo small)
0001_2067-02-15_22-26-25.json             per-scene metadata: timecode, VAUX date, video/audio, hashes, discontinuities
tape.mp4                                    whole-tape review file (one scene here, so identical to the proxy)
thumbnails/0001_2067-02-15_22-26-25.jpg
```

About this scene:

- Timecode `00:04:02:09 – 00:04:08:07`, PAL 720×576, 25/1, interlaced (BFF).
- The camera's clock was wrong when it was recorded, so the DV VAUX date reads
  **2067-02-15 22:26:25**. The dashboard shows that verbatim and flags it
  *"⚠ zegar kamery błędny"* (year outside 1990–2025) — a real, useful case to see
  in the UI. The container `creation_time` is deliberately **not** stamped from it.
- `1× nieciągłość` — the scene contains one DV discontinuity marker
  (`source_discontinuities` in the JSON), also surfaced in the UI.
- The tape carries a `recording_date` override of **2026-09-09** and the label
  *"Przykładowa taśma (demo)"* — those are what the tape tile/detail show; the
  scene itself keeps its own VAUX timecode/date.

Verify it:

```bash
sha256sum -c tape.sha256                  # manifest (all files)
zstd -t 0001_*.dv.zst                     # archive integrity
zstd -dc 0001_*.dv.zst | sha256sum        # == files.archive.sha256_uncompressed in the .json
```

Load it into a running instance:

```bash
cp -r examples/demo-tape "$MINIDV_STORAGE/tapes/DEMO-MINIDV"
```

> The `.dv.zst` master is the untouched capture from `TAPE-0001` (same bytes, same
> checksums), so it reproduces the DV exactly. Only the `.mp4` proxy here is
> re-encoded small (`crf 28`) instead of the `crf 16` a real capture produces.
