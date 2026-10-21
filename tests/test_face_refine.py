import sqlite3

import pytest

np = pytest.importorskip("numpy")

from minidv_archiver.face_refine import avg_linkage, frontal_score, plan_clusters  # noqa: E402
from minidv_archiver.store import JobStore  # noqa: E402

KW = dict(core_frontal=0.4, cluster_t=0.16, attach_t=0.25, attach_margin=0.05)


def _v(seed, noise=0.0, base=None):
    """512-d unit vector; same ``base`` + small noise = same person."""
    rng = np.random.default_rng(1000 + seed)       # never collides with the people's own seeds
    v = (base if base is not None else rng.normal(size=512)) + noise * rng.normal(size=512)
    return (v / np.linalg.norm(v)).astype(np.float32)


def _track(tid, vec, *, person=1, scene="S1", times=(0,), frontal=0.9, tape="T"):
    return {"id": tid, "person_id": person, "tape_id": tape, "scene_id": scene, "times": list(times),
            "arc": vec.tobytes() if vec is not None else None, "frontal": frontal, "rq": 50.0}


def _groups(groups):
    return sorted(sorted(g["tracks"]) for g in groups)


def test_avg_linkage_matches_people_and_honours_cannot_links():
    a, b = np.random.default_rng(0).normal(size=(2, 512))
    vs = np.stack([_v(i, 0.6, a) for i in range(4)] + [_v(10 + i, 0.6, b) for i in range(4)])
    lab = avg_linkage(vs @ vs.T, 0.16)
    assert len(set(lab[:4])) == 1 and len(set(lab[4:])) == 1 and lab[0] != lab[4]
    cannot = np.zeros((8, 8), bool)
    cannot[0, 1] = cannot[1, 0] = True
    lab = avg_linkage(vs @ vs.T, 0.16, cannot=cannot)
    assert lab[0] != lab[1]


def test_two_people_in_one_frame_are_never_merged():
    base = np.random.default_rng(1).normal(size=512)
    tracks = [_track(1, _v(1, 0.3, base), times=(3, 4)), _track(2, _v(2, 0.3, base), times=(4, 5))]
    assert _groups(plan_clusters(tracks, [], [], **KW)) == [[1], [2]]
    tracks[1]["times"] = [7]             # not on screen together any more -> same person
    assert _groups(plan_clusters(tracks, [], [], **KW)) == [[1, 2]]


def test_operator_feedback_is_a_hard_rule():
    base = np.random.default_rng(2).normal(size=512)
    tracks = [_track(1, _v(1, 0.3, base), scene="A"), _track(2, _v(2, 0.3, base), scene="B"),
              _track(3, _v(3), scene="C")]
    assert _groups(plan_clusters(tracks, [("diff", 1, 2)], [], **KW)) == [[1], [2], [3]]
    assert _groups(plan_clusters(tracks, [("same", 1, 3)], [], **KW)) == [[1, 2, 3]]


def test_names_follow_their_anchor_and_weak_anchors_are_replaced():
    pa, pb = np.random.default_rng(3).normal(size=(2, 512))
    tracks = [_track(1, _v(1, 0.3, pa), person=10, scene="A"), _track(2, _v(2, 0.3, pa), person=10, scene="B"),
              _track(3, _v(3, 0.3, pb), person=10, scene="C"),          # mixed into 10 by the fast scan
              _track(4, _v(4, 0.3, pa), person=10, scene="D", frontal=0.1)]   # profile view
    touched = [{"id": 10, "name": "Ola", "hidden": False, "anchor": 4}]
    groups = plan_clusters(tracks, [], touched, **KW)
    ola = next(g for g in groups if g["person_id"] == 10)
    assert {1, 2} <= set(ola["tracks"]) and 3 not in ola["tracks"]   # the stranger is split off


def test_weak_faces_join_only_a_clearly_closest_group():
    pa, pb = np.random.default_rng(4).normal(size=(2, 512))
    tracks = [_track(1, _v(1, 0.3, pa), scene="A"), _track(2, _v(2, 0.3, pb), scene="B"),
              _track(3, _v(3, 0.4, pa), scene="C", frontal=0.1),               # weak, clearly A
              _track(4, _v(4), scene="D", frontal=0.1)]                        # weak, nobody
    groups = _groups(plan_clusters(tracks, [], [], **KW))
    assert [1, 3] in groups and [4] in groups and [2] in groups


def test_tracks_without_a_refined_view_keep_their_person():
    tracks = [_track(1, None, person=7), _track(2, None, person=7, scene="B")]
    groups = plan_clusters(tracks, [], [], **KW)
    assert groups == [{"person_id": 7, "tracks": [1, 2], "kept": True}]


def test_every_track_lands_in_exactly_one_group():
    rng = np.random.default_rng(5)
    people = rng.normal(size=(5, 512))
    tracks = [_track(i, _v(i, 0.5, people[i % 5]), person=1 + i % 3, scene=f"S{i // 4}", times=(i % 4,),
                     frontal=0.9 if i % 3 else 0.2) for i in range(40)]
    groups = plan_clusters(tracks, [("same", 0, 5), ("diff", 1, 6)], [], **KW)
    ids = [t for g in groups for t in g["tracks"]]
    assert sorted(ids) == list(range(40))
    assert len({g["person_id"] for g in groups if g["person_id"] is not None}) == \
        sum(1 for g in groups if g["person_id"] is not None)


def test_frontal_score():
    lm = np.array([[40, 50], [70, 50], [55, 65], [45, 80], [65, 80]], np.float32)
    assert frontal_score(lm, 72) > 0.9
    side = np.array([[50, 50], [56, 50], [66, 65], [52, 80], [58, 80]], np.float32)
    assert frontal_score(side, 72) < 0.2


def test_old_database_gets_the_new_columns(tmp_path):
    db = tmp_path / "jobs.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE face_tracks (id INTEGER PRIMARY KEY, person_id INTEGER NOT NULL, workspace TEXT NOT NULL, "
              "tape_id TEXT NOT NULL, scene_id TEXT NOT NULL, embedding BLOB NOT NULL, quality REAL, times TEXT)")
    c.execute("INSERT INTO face_tracks VALUES (1, 1, 'default', 'T', 'S', x'00', 5, '[1]')")
    c.commit()
    c.close()
    s = JobStore(db)
    cols = {r[1] for r in s._conn.execute("PRAGMA table_info(face_tracks)")}
    assert {"arc", "rq", "frontal", "refined"} <= cols
    assert s._conn.execute("SELECT quality FROM face_tracks WHERE id=1").fetchone()[0] == 5


def test_merge_and_move_record_feedback_and_anchors(tmp_path):
    s = JobStore(tmp_path / "jobs.db")
    a, b = s.face_person_create("default"), s.face_person_create("default")
    ta, = s.face_save_scene("T", "1", "m", [{"person_id": a, "workspace": "default", "embedding": b"\0" * 4,
                                             "quality": 9, "times": [1]}])
    tb, tb2 = s.face_save_scene("T", "2", "m", [
        {"person_id": b, "workspace": "default", "embedding": b"\0" * 4, "quality": 8, "times": [1]},
        {"person_id": b, "workspace": "default", "embedding": b"\0" * 4, "quality": 1, "times": [2]}])
    s.face_person_update(a, name="Ola")
    assert s.face_touched_persons("default") == [{"id": a, "name": "Ola", "hidden": False, "anchor": ta}]
    s.face_merge(b, a)
    assert ("same", tb, ta) in s.face_feedback()
    s.face_move_track(tb2, None)
    assert ("diff", tb2, ta) in s.face_feedback()


def test_apply_clusters_refuses_after_an_operator_edit(tmp_path):
    s = JobStore(tmp_path / "jobs.db")
    a = s.face_person_create("default")
    t, = s.face_save_scene("T", "1", "m", [{"person_id": a, "workspace": "default", "embedding": b"\0" * 4,
                                            "quality": 1, "times": [1]}])
    gen = s.faces_gen()
    s.face_move_track(t, None)                      # operator acts while clustering runs
    assert not s.face_apply_clusters("default", gen, [{"person_id": None, "tracks": [t]}])
    gen = s.faces_gen()
    groups = [{"person_id": None, "tracks": [t]}]
    assert s.face_apply_clusters("default", gen, groups)
    assert s.face_track(t)["person_id"] == groups[0]["person_id"]


def test_reconstructed_rules_import_once_and_skip_missing_tracks(tmp_path):
    import json
    from minidv_archiver.config import Config
    from minidv_archiver.face_refine import import_rules
    cfg = Config(storage=tmp_path)
    cfg.ensure_dirs()
    s = JobStore(cfg.state / "jobs.db")
    p = s.face_person_create("default")
    a, b = s.face_save_scene("T", "1", "m", [
        {"person_id": p, "workspace": "default", "embedding": b"\0" * 4, "quality": 1, "times": [1]},
        {"person_id": p, "workspace": "default", "embedding": b"\0" * 4, "quality": 1, "times": [2]}])
    (cfg.state / "face_rules-20261021.json").write_text(json.dumps({
        "groups": {str(p): {"tracks": [a, b]}},
        "feedback_pairs": [["same", a, b], ["diff", b, 999], ["bogus", a, b]]}))
    assert import_rules(cfg, s) == 1
    assert import_rules(cfg, s) == 0                       # once per file
    assert s.face_feedback() == [("same", a, b)]
    assert s.face_touched_persons("default") == []         # an anchor alone doesn't name anyone
    assert s._conn.execute("SELECT anchor FROM face_persons WHERE id=?", (p,)).fetchone()[0] == a
