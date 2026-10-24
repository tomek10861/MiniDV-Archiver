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
    assert s.face_touched_persons("default") == [{"id": a, "name": "Ola", "hidden": False, "group": None, "anchor": ta}]
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


def test_confirmed_faces_return_home_unless_they_clearly_dont_fit():
    pa, pb = np.random.default_rng(6).normal(size=(2, 512))
    home = [_track(i, _v(i, 0.3, pa), person=10, scene=f"A{i}") for i in (1, 2, 3)]
    stray_ok = _track(4, _v(4, 1.6, pa), person=10, scene="B")          # same person, poor view: own group
    stranger = _track(5, _v(5, 0.3, pb), person=10, scene="C")          # frontal, someone else
    profile = _track(6, _v(6), person=10, scene="D", frontal=0.1)       # can't judge a profile
    tracks = home + [stray_ok, stranger, profile]
    fb = [("confirm", m, 1) for m in (2, 3, 4, 5, 6)]
    touched = [{"id": 10, "name": "Ola", "hidden": False, "anchor": 1}]
    groups = plan_clusters(tracks, fb, touched, **KW)
    ola = set(next(g for g in groups if g["person_id"] == 10)["tracks"])
    assert {1, 2, 3, 4, 6} <= ola and 5 not in ola


def test_a_person_split_by_the_model_is_rejoined_by_confirmations():
    rng = np.random.default_rng(7)
    young, old = rng.normal(size=(2, 512))          # the model sees two people...
    tracks = ([_track(i, _v(i, 0.3, young), person=10, scene=f"Y{i}") for i in (1, 2, 3)]
              + [_track(i, _v(i, 0.3, old), person=10, scene=f"O{i}") for i in (4, 5, 6)])
    fb = [("confirm", m, 1) for m in (2, 3, 4, 5, 6)]  # ...the operator merged them into one
    touched = [{"id": 10, "name": "Ola", "hidden": False, "anchor": 1}]
    assert _groups(plan_clusters(tracks, fb, touched, **KW)) == [[1, 2, 3, 4, 5, 6]]
    assert _groups(plan_clusters(tracks, fb + [("diff", 4, 1)], touched, **KW)) == [[1, 2, 3], [4, 5, 6]]


def test_suggestions_offer_the_clearly_closest_named_person_and_respect_no():
    from minidv_archiver.face_refine import suggest_names
    rng = np.random.default_rng(8)
    ola, ewa, x = rng.normal(size=(3, 512))
    tracks = [_track(1, _v(1, 0.3, ola), person=10), _track(2, _v(2, 0.3, ewa), person=20),
              _track(3, _v(3, 2.2, ola), person=30),          # weakly Ola: below clustering, above a hint
              _track(4, _v(4, 0.3, x), person=40)]             # nobody we know
    persons = [{"id": 10, "name": "Ola", "hidden": False, "cover": 1}, {"id": 20, "name": "Ewa", "hidden": False},
               {"id": 30, "name": None, "hidden": False}, {"id": 40, "name": None, "hidden": False},
               {"id": 50, "name": None, "hidden": True}]
    kw = dict(core_frontal=0.4, min_score=0.12, margin=0.03)
    sug = suggest_names(tracks, persons, [], **kw)
    assert set(sug) == {30} and sug[30]["id"] == 10 and sug[30]["name"] == "Ola"
    assert suggest_names(tracks, persons, [("diff", 3, 1)], **kw) == {}     # "no" sticks


def test_reject_pair_writes_a_diff_between_the_two_people(tmp_path):
    s = JobStore(tmp_path / "jobs.db")
    a, b = s.face_person_create("default"), s.face_person_create("default")
    ta, = s.face_save_scene("T", "1", "m", [{"person_id": a, "workspace": "default", "embedding": b"\0" * 4,
                                             "quality": 1, "times": [1]}])
    tb, = s.face_save_scene("T", "2", "m", [{"person_id": b, "workspace": "default", "embedding": b"\0" * 4,
                                             "quality": 1, "times": [1]}])
    sig = s.face_signature("default")
    s.face_reject_pair(a, b)
    assert ("diff", ta, tb) in s.face_feedback()
    assert s.face_signature("default") != sig            # suggestions cache sees the change


def test_a_group_alone_keeps_an_unnamed_person_through_regrouping(tmp_path):
    s = JobStore(tmp_path / "jobs.db")
    a, b = s.face_person_create("default"), s.face_person_create("default")
    ta, = s.face_save_scene("T", "1", "m", [{"person_id": a, "workspace": "default", "embedding": b"\0" * 4,
                                             "quality": 1, "times": [1]}])
    s.face_save_scene("T", "2", "m", [{"person_id": b, "workspace": "default", "embedding": b"\0" * 4,
                                       "quality": 1, "times": [1]}])
    s.face_person_update(a, group="Sąsiedzi")                   # "the neighbour", name unknown
    touched = s.face_touched_persons("default")
    assert touched == [{"id": a, "name": None, "hidden": False, "group": "Sąsiedzi", "anchor": ta}]
    base = np.random.default_rng(9).normal(size=512)
    tracks = [_track(ta, _v(1, 0.3, base), person=b)]           # the scan moved the face elsewhere
    assert plan_clusters(tracks, [], touched, **KW)[0]["person_id"] == a
    s.face_merge(a, b)                                          # merging hands the group over
    assert s.face_person(b)["group"] == "Sąsiedzi"


def test_dissolve_frees_every_face_and_forgets_the_glue_but_not_the_nos(tmp_path):
    s = JobStore(tmp_path / "jobs.db")
    p, other = s.face_person_create("default"), s.face_person_create("default")
    a, b = s.face_save_scene("T", "1", "m", [
        {"person_id": p, "workspace": "default", "embedding": b"\0" * 4, "quality": 1, "times": [1]},
        {"person_id": p, "workspace": "default", "embedding": b"\0" * 4, "quality": 1, "times": [2]}])
    c, = s.face_save_scene("T", "2", "m", [{"person_id": other, "workspace": "default", "embedding": b"\0" * 4,
                                            "quality": 1, "times": [1]}])
    s.face_person_update(p, name="Jacek Albiniak", group="Znajomi")
    s.face_import_rules("x", [("confirm", b, a), ("same", a, c), ("diff", b, c)], {})
    new = s.face_dissolve([p])
    assert len(new) == 2 and s.face_person(p) is None
    assert {s.face_track(a)["person_id"], s.face_track(b)["person_id"]} == set(new)
    assert s.face_feedback() == [("diff", b, c)]
    assert s.face_touched_persons("default") == []           # the name and group went with it


def test_delete_persons_drops_their_faces_for_good(tmp_path):
    s = JobStore(tmp_path / "jobs.db")
    p = s.face_person_create("default")
    t, = s.face_save_scene("T", "1", "m", [{"person_id": p, "workspace": "default", "embedding": b"\0" * 4,
                                            "quality": 1, "times": [1]}])
    assert s.face_delete_persons([p]) == [t]
    assert s.face_track(t) is None and s.face_person(p) is None
    assert ("T", "1") in s.face_scanned_keys("m")            # stays scanned: the statue won't come back


@pytest.mark.parametrize("rate", [25, 50])
def test_refine_samples_the_same_moments_whatever_the_frame_rate(tmp_path, rate):
    import shutil
    import subprocess
    if not shutil.which("ffmpeg"):
        pytest.skip("needs ffmpeg")
    from minidv_archiver.face_refine import _frames
    mp4 = tmp_path / "v.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"testsrc=size=64x48:rate={rate}", "-t", "3",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(mp4)], check=True)
    secs = [s for s, _ in _frames(mp4, {0, 2}, float(rate), 64, 48, np)]
    # ~6 frames a second (every 4th at 25 fps, every 8th at 50p) — not twice as many at 50p
    assert secs.count(0) in (6, 7) and secs.count(2) in (6, 7) and set(secs) == {0, 2}
