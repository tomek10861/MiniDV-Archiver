from minidv_archiver.config import Config


def _server(tmp_path, monkeypatch):
    import minidv_archiver.server as srv
    monkeypatch.setattr(srv, "CONFIG", Config(storage=tmp_path))
    tape = tmp_path / "tapes" / "TAPE-0001"
    (tape / "thumbnails").mkdir(parents=True)
    (tape / "0001_x.mp4").write_bytes(b"mp4")
    (tape / "thumbnails" / "0001_x.jpg").write_bytes(b"jpg")
    (tmp_path / "tapes" / "secret.txt").write_text("nope")
    return srv


def test_archive_file_serves_files_inside_the_tape(tmp_path, monkeypatch):
    srv = _server(tmp_path, monkeypatch)
    assert srv.archive_file("TAPE-0001", "0001_x.mp4").read_bytes() == b"mp4"
    assert srv.archive_file("TAPE-0001", "thumbnails/0001_x.jpg").read_bytes() == b"jpg"


def test_archive_file_blocks_traversal_and_missing(tmp_path, monkeypatch):
    srv = _server(tmp_path, monkeypatch)
    assert srv.archive_file("TAPE-0001", "../secret.txt") is None
    assert srv.archive_file("TAPE-0001", "../../etc/passwd") is None
    assert srv.archive_file("TAPE-0001", "thumbnails/../../secret.txt") is None
    assert srv.archive_file("TAPE-0001/../TAPE-0001", "0001_x.mp4") is None
    assert srv.archive_file("..", "secret.txt") is None
    assert srv.archive_file("TAPE-0001", "/etc/passwd") is None
    assert srv.archive_file("TAPE-0001", "does-not-exist.mp4") is None
    assert srv.archive_file("MISSING", "0001_x.mp4") is None


def test_content_type_covers_archive_extensions(tmp_path, monkeypatch):
    srv = _server(tmp_path, monkeypatch)
    assert srv.content_type("0001_x.dv.zst") == "application/zstd"
    assert srv.content_type("tape.sha256") == "text/plain"
    assert srv.content_type("0001_x.mp4") == "video/mp4"
