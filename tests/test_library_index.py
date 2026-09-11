import json

from minidv_archiver import library_index as li
from minidv_archiver.config import Config
from minidv_archiver.store import JobStore


def _cfg(tmp_path):
    c = Config(storage=tmp_path)
    c.ensure_dirs()
    return c


def _tape(cfg, tape_id, scenes, *, label=None, recording_date=None):
    """scenes: list of (scene_id, vaux_datetime_or_None, valid_bool)"""
    d = cfg.tapes / tape_id
    (d / "thumbnails").mkdir(parents=True)
    tape = {"schema_version": 1, "tape_id": tape_id, "scene_count": len(scenes),
            "scenes": [{"scene_index": i + 1, "scene_id": s[0], "frame_count": 100}
                       for i, s in enumerate(scenes)]}
    if label:
        tape["label"] = label
    if recording_date:
        tape["recording_date"] = recording_date
    (d / "tape.json").write_text(json.dumps(tape))
    for i, (sid, dt, valid) in enumerate(scenes, 1):
        (d / f"{sid}.json").write_text(json.dumps({
            "scene_index": i, "scene_id": sid, "tape_id": tape_id,
            "recording": {"datetime": dt, "datetime_source": "DV_VAUX", "datetime_valid": valid},
            "timecode": {"start": "00:00:00:00", "end": "00:00:04:00"},
            "video": {"standard": "PAL"}, "frame_count": 100,
            "files": {"archive": {"size_compressed": 1000}}}))
    return d


def test_scene_date_priority(tmp_path):
    tape = {"recording_date": "2005-06-07"}
    # override wins even over a valid VAUX date
    assert li._scene_date(tape, {"recording": {"datetime": "2004-01-02T00:00:00", "datetime_valid": True},
                                "scene_id": "0001_2004-01-02_00-00-00"}) == "2005-06-07"
    # no override -> valid VAUX
    assert li._scene_date({}, {"recording": {"datetime": "2004-01-02T10:11:12", "datetime_valid": True},
                               "scene_id": "0001_2099-01-01_00-00-00"}) == "2004-01-02"
    # invalid VAUX (bad clock) -> fall back to a sane scene_id date
    assert li._scene_date({}, {"recording": {"datetime": "2067-02-15T00:00:00", "datetime_valid": True},
                               "scene_id": "0014_2004-07-11_17-42-44"}) == "2004-07-11"
    # nothing usable
    assert li._scene_date({}, {"recording": {}, "scene_id": "0001_UNKNOWN-DATE"}) is None


def test_reindex_is_incremental_and_prunes(tmp_path):
    cfg = _cfg(tmp_path)
    s = JobStore(cfg.state / "jobs.db")
    _tape(cfg, "TAPE-0001", [("0001_2004-07-11_10-00-00", "2004-07-11T10:00:00", True)])
    assert li.reindex(cfg, s) == {"tapes": 1, "rebuilt": 1}
    assert li.reindex(cfg, s)["rebuilt"] == 0          # unchanged -> skipped
    _tape(cfg, "TAPE-0002", [("0001_2005-01-02_09-00-00", "2005-01-02T09:00:00", True)])
    assert li.reindex(cfg, s) == {"tapes": 2, "rebuilt": 1}
    import shutil
    shutil.rmtree(cfg.tapes / "TAPE-0001")
    li.reindex(cfg, s)
    assert s.index_tape_ids() == {"TAPE-0002"}


def test_tapes_list_hides_index_internals_and_keeps_tape_json_shape(tmp_path):
    cfg = _cfg(tmp_path)
    s = JobStore(cfg.state / "jobs.db")
    _tape(cfg, "TAPE-0001", [("0001_2004-07-11_10-00-00", "2004-07-11T10:00:00", True)], label="Wesele")
    tapes = li.tapes_list(cfg, s)
    assert tapes[0]["tape_id"] == "TAPE-0001" and tapes[0]["label"] == "Wesele"
    assert tapes[0]["scenes"][0]["scene_id"] == "0001_2004-07-11_10-00-00"
    assert not any(k.startswith("_") for k in tapes[0])


def test_timeline_groups_by_year_and_month(tmp_path):
    cfg = _cfg(tmp_path)
    s = JobStore(cfg.state / "jobs.db")
    _tape(cfg, "A", [("0001_2004-07-11_10-00-00", "2004-07-11T10:00:00", True),
                     ("0002_2004-07-20_11-00-00", "2004-07-20T11:00:00", True),
                     ("0003_2004-09-01_12-00-00", "2004-09-01T12:00:00", True)])
    _tape(cfg, "B", [("0001_2006-01-05_08-00-00", "2006-01-05T08:00:00", True),
                     ("0002_1970-01-01_00-00-00", "1970-01-01T00:00:00", True)])   # undated (bad year)
    tl = li.timeline(cfg, s)
    assert [y["year"] for y in tl["years"]] == [2006, 2004]      # newest first
    y2004 = next(y for y in tl["years"] if y["year"] == 2004)
    assert y2004["count"] == 3
    assert [(m["month"], m["count"]) for m in y2004["months"]] == [(9, 1), (7, 2)]
    assert tl["undated"] == 1
    assert y2004["thumb"]["tape_id"] == "A"

    july = li.month_scenes(cfg, s, 2004, 7)
    assert [x["scene_id"] for x in july] == ["0001_2004-07-11_10-00-00", "0002_2004-07-20_11-00-00"]
    assert july[0]["tc"]["end"] == "00:00:04:00"


def test_scene_time_from_vaux_or_scene_id(tmp_path):
    # valid VAUX -> its time-of-day
    assert li._scene_time({"recording": {"datetime": "2004-01-02T10:11:12", "datetime_valid": True},
                           "scene_id": "0001_2099-01-01_00-00-00"}) == "10:11:12"
    # no VAUX -> fall back to the (same-source) scene_id stamp
    assert li._scene_time({"recording": {}, "scene_id": "0014_2004-07-11_17-42-44"}) == "17:42:44"
    # nothing usable
    assert li._scene_time({"recording": {}, "scene_id": "0001_UNKNOWN-DATE"}) is None


def test_month_scenes_interleave_by_time_across_tapes(tmp_path):
    """The point: two different tapes recorded on the same day should not just be
    grouped tape-by-tape — scenes should interleave in actual recording time order."""
    cfg = _cfg(tmp_path)
    s = JobStore(cfg.state / "jobs.db")
    _tape(cfg, "TAPE-B", [("0001_2004-07-11_15-00-00", "2004-07-11T15:00:00", True)])
    _tape(cfg, "TAPE-A", [("0001_2004-07-11_09-00-00", "2004-07-11T09:00:00", True),
                          ("0002_2004-07-11_20-00-00", "2004-07-11T20:00:00", True)])
    july = li.month_scenes(cfg, s, 2004, 7)
    assert [(x["tape_id"], x["time"]) for x in july] == [
        ("TAPE-A", "09:00:00"), ("TAPE-B", "15:00:00"), ("TAPE-A", "20:00:00"),
    ]


def _tape_with_fingerprint(cfg, tape_id, scene_hashes: dict[str, list[str]]):
    """scene_hashes: {scene_id: [hash0, hash1, hash2]} -- builds one tape with those scenes,
    all sharing an (unmatched) recording datetime so only frame_hashes drive matching."""
    d = cfg.tapes / tape_id
    (d / "thumbnails").mkdir(parents=True)
    scenes = [{"scene_index": i + 1, "scene_id": sid, "frame_count": 500}
              for i, sid in enumerate(scene_hashes)]
    (d / "tape.json").write_text(json.dumps({"tape_id": tape_id, "scene_count": len(scenes), "scenes": scenes}))
    for i, (sid, hashes) in enumerate(scene_hashes.items(), 1):
        (d / f"{sid}.json").write_text(json.dumps({
            "scene_index": i, "scene_id": sid, "tape_id": tape_id, "frame_count": 500,
            "timecode": {"start": "00:05:00:00", "end": "00:05:20:00"},
            "recording": {"datetime": None},
            "capture": {"dropped_frames": 0, "source_discontinuities": [], "decode_errors": 0, "error_score": 0},
            "fingerprint": {"datetime": None, "tc_start": None, "tc_end": None, "frame_count": 500,
                            "frame_hashes": hashes}}))


def test_duplicates_matches_near_identical_hashes_not_just_byte_exact(tmp_path):
    """Two separate captures of the same footage never land on byte-identical DV
    frames (decode rounding, a scene boundary off by a frame) -- an exact hash
    match essentially never fires in practice. Both valid hash positions here
    differ by 1 bit (tolerated, not byte-identical); the third position is a
    filtered-out near-uniform frame ("") on both sides, so it's simply not
    counted rather than blocking the match."""
    cfg = _cfg(tmp_path)
    s = JobStore(cfg.state / "jobs.db")
    _tape_with_fingerprint(cfg, "A", {"0001_s": ["00000000000000ff", "1111111111111110", ""]})
    _tape_with_fingerprint(cfg, "B", {"0001_s": ["00000000000000fe", "1111111111111111", ""]})
    li.reindex(cfg, s, force=True)

    dup = li.duplicates(cfg, s)
    assert len(dup["groups"]) == 1
    assert {m["tape_id"] for m in dup["groups"][0]["members"]} == {"A", "B"}


def test_duplicates_requires_every_sampled_frame_to_match_not_just_some(tmp_path):
    """Matching only e.g. 2 of 3 hash positions sounds reasonable per-pair, but at a
    few thousand scenes the union-find grouping turns any nonzero per-pair
    false-positive rate into a giant transitively-chained cluster of scenes that
    merely look vaguely similar. Two close positions and one far one must NOT
    match -- every sampled frame has to agree, since real duplicate footage does."""
    cfg = _cfg(tmp_path)
    s = JobStore(cfg.state / "jobs.db")
    _tape_with_fingerprint(cfg, "A", {"0001_s": ["00000000000000ff", "1111111111111110", "aaaaaaaaaaaaaaaa"]})
    _tape_with_fingerprint(cfg, "B", {"0001_s": ["00000000000000fe", "1111111111111111", "5555555555555555"]})
    li.reindex(cfg, s, force=True)

    dup = li.duplicates(cfg, s)
    assert dup["groups"] == []


def test_duplicates_finds_same_tape_pairs_too(tmp_path):
    """A duplicated/over-recorded scene within a single tape's own capture must be
    surfaced too, not just the same footage recaptured under a different tape_id."""
    cfg = _cfg(tmp_path)
    s = JobStore(cfg.state / "jobs.db")
    hashes = ["00000000000000ff", "1111111111111110", "aaaaaaaaaaaaaaaa"]
    _tape_with_fingerprint(cfg, "TAPE-X", {"0001_s": hashes, "0002_s": hashes})
    li.reindex(cfg, s, force=True)

    dup = li.duplicates(cfg, s)
    assert len(dup["groups"]) == 1
    members = dup["groups"][0]["members"]
    assert {m["scene_id"] for m in members} == {"0001_s", "0002_s"}
    assert all(m["tape_id"] == "TAPE-X" for m in members)


def test_duplicates_best_prefers_longer_recording_on_tied_error_score(tmp_path):
    """Same error_score -> the longer capture (more of the moment actually caught)
    wins, not an arbitrary tape_id/scene_index ordering."""
    cfg = _cfg(tmp_path)
    s = JobStore(cfg.state / "jobs.db")
    hashes = ["1111111111111111", "2222222222222222", "3333333333333333"]

    def tape(tid, frame_count):
        d = cfg.tapes / tid
        (d / "thumbnails").mkdir(parents=True)
        (d / "tape.json").write_text(json.dumps({"tape_id": tid, "scene_count": 1,
            "scenes": [{"scene_index": 1, "scene_id": "0001_s", "frame_count": frame_count}]}))
        (d / "0001_s.json").write_text(json.dumps({
            "scene_index": 1, "scene_id": "0001_s", "tape_id": tid, "frame_count": frame_count,
            "timecode": {"start": "00:05:00:00", "end": "00:05:20:00"},
            "recording": {"datetime": None},
            "capture": {"dropped_frames": 0, "source_discontinuities": [], "decode_errors": 0, "error_score": 0},
            "fingerprint": {"datetime": None, "tc_start": None, "tc_end": None, "frame_count": frame_count,
                            "frame_hashes": hashes}}))

    tape("AAA", 400)   # alphabetically first, but shorter -- must NOT win on tape_id order
    tape("ZZZ", 900)   # longer -- should be picked as best despite sorting last by tape_id
    li.reindex(cfg, s, force=True)

    dup = li.duplicates(cfg, s)
    assert len(dup["groups"]) == 1
    best = next(m for m in dup["groups"][0]["members"] if m["best"])
    assert best["tape_id"] == "ZZZ" and best["frame_count"] == 900


def test_tape_recording_date_override_moves_all_scenes(tmp_path):
    cfg = _cfg(tmp_path)
    s = JobStore(cfg.state / "jobs.db")
    _tape(cfg, "DEMO", [("0001_2067-02-15_22-26-25", "2067-02-15T22:26:25", True)],
          recording_date="2026-09-09")
    tl = li.timeline(cfg, s)
    assert [y["year"] for y in tl["years"]] == [2026]
    assert tl["years"][0]["months"][0]["month"] == 9
