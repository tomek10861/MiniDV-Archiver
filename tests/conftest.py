import pytest

from minidv_archiver.engine import Engine


@pytest.fixture(autouse=True)
def _no_face_scan_thread(monkeypatch):
    """The face-scan worker (converter role) wakes every few seconds and would run
    the nightly scan for real if a test happened to start inside its window; no test
    needs the thread itself — the scan logic is tested directly in test_faces."""
    monkeypatch.setattr(Engine, "_faces_worker", lambda self: None)


@pytest.fixture(autouse=True)
def _no_media_thread(monkeypatch):
    """Same for the photo/video indexer: it sleeps between passes."""
    monkeypatch.setattr(Engine, "_media_worker", lambda self: None)
