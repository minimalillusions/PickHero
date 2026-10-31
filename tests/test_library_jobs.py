"""Nonblocking library transitions, track identity, and bounded worker transport.

Actual child cancellation is covered by scripts/test-import-worker.py; pytest
keeps its global ban on external processes and physical audio intact.
"""
import os
from dataclasses import asdict
from pathlib import Path
from unittest.mock import Mock

import pygame
import pytest

from pickhero import instruments, library
from pickhero.config import Config
from pickhero.import_worker import decode_song, encode_song, perform
from pickhero.progress import ProgressTracker, song_key
from pickhero.tabs.loader import load_gp_file
from pickhero.ui.app import App
from pickhero.ui.colors import set_look
from pickhero.ui.import_menu import ImportScreen
from pickhero.ui.menu import MenuScreen


@pytest.fixture
def graphics():
    pygame.font.init()
    yield
    set_look("studio")


class FakeJob:
    def __init__(self, operation, path, instrument, **kwargs):
        self.operation = operation
        self.path = path
        self.options = kwargs
        self.ready = False
        self.result = None
        self.error = None
        self.status = {"current": 2, "total": 10}
        self.cancelled = False

    def poll(self):
        return self.ready

    def cancel(self):
        self.cancelled = True


def fixture_song():
    return Path(__file__).parent / "fixtures" / "Demo_v5.gp5"


def test_transport_preserves_notes_beats_tempo_and_tuning():
    original = load_gp_file(fixture_song(), instrument=instruments.BASS_UKULELE)
    import json
    decoded, backing = decode_song(json.loads(json.dumps(encode_song(original))))
    assert decoded.notes == original.notes
    assert decoded.measures == original.measures
    assert decoded.metadata == original.metadata
    assert decoded._beats == original._beats
    assert decoded._tempos == original._tempos
    assert len(backing) == 0


def test_content_identity_follows_move_but_separates_same_names(tmp_path):
    left = tmp_path / "one.gp5"
    left.write_bytes(b"first score")
    identity = library.content_identity(left)
    right = tmp_path / "renamed.gp5"
    left.rename(right)
    assert library.content_identity(right) == identity
    left.write_bytes(b"another score")
    assert library.content_identity(left) != identity
    assert song_key(left, "guitar", 0, content_id=identity) == song_key(right, "guitar", 0, content_id=identity)
    assert song_key(right, "guitar", 0, content_id=identity) != song_key(right, "guitar", 1, content_id=identity)


def test_worker_inspection_disables_guitar_for_four_string_bass():
    result = perform({"operation": "inspect", "path": str(fixture_song()),
                      "instrument": "bass_ukulele"}, lambda _status: None)
    choices = {row["name"]: row for row in result["tracks"]}
    assert choices["Bass"]["compatible"]
    assert not choices["Rhythm Guitar"]["compatible"]
    assert len(result["content_id"]) == 64


def test_worker_rejects_changed_score_before_parsing():
    with pytest.raises(ValueError, match="changed"):
        perform({"operation": "load", "path": str(fixture_song()),
                 "instrument": "bass_ukulele", "content_id": "0"*64}, lambda _status: None)


def test_menu_background_scan_does_not_call_parser_or_wait(monkeypatch, tmp_path):
    monkeypatch.setattr("pickhero.import_jobs.ImportJob", FakeJob)
    monkeypatch.setattr(library, "scan", Mock(side_effect=AssertionError("UI parsed file")))
    menu = MenuScreen(tmp_path, Config(instrument="bass_ukulele"), ProgressTracker(), background=True)
    job = menu._scan_job
    assert menu._songs == [] and not job.ready
    menu.update()
    assert menu._scan_job is job
    menu._search_active = True
    menu._search_text = "ready"
    job.result = {"songs": [asdict(library.SongInfo(str(tmp_path/"ready.gp5"), title="Ready"))]}
    job.ready = True
    menu.update()
    assert menu._scan_job is None and menu._rows[0].title == "Ready"
    assert menu._search_active and menu._search_text == "ready"
    menu._search_active = False
    menu.scan_files()
    second = menu._scan_job
    menu.handle_event(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_ESCAPE))
    assert second.cancelled and menu._scan_job is None
    assert menu._rows[0].title == "Ready"  # cancelled refresh retains usable rows


def test_rescan_terminates_prior_worker_and_close_terminates_new(monkeypatch, tmp_path):
    monkeypatch.setattr("pickhero.import_jobs.ImportJob", FakeJob)
    menu = MenuScreen(tmp_path, Config(), background=True)
    first = menu._scan_job
    menu.scan_files()
    second = menu._scan_job
    assert first.cancelled and not second.cancelled
    menu.close()
    assert second.cancelled


def test_app_waits_for_explicit_track_before_loading(monkeypatch, tmp_path, graphics):
    monkeypatch.setattr("pickhero.ui.app.ImportJob", FakeJob)
    monkeypatch.setattr("pickhero.ui.app.load_gp_file", Mock(side_effect=AssertionError("UI parsed file")))
    app = App(Config(instrument="bass_ukulele"), background=True)
    app._load_song(fixture_song())
    job = app._import_job
    assert job.operation == "inspect" and app._state == "import"
    app._poll_import()
    assert app._import_job is job
    job.result = perform({"operation": "inspect", "path": str(fixture_song()),
                          "instrument": "bass_ukulele"}, lambda _status: None)
    job.ready = True
    app._poll_import()
    assert app._import_job is None and app._state == "import"
    assert app._playing_screen is None
    surface = pygame.Surface((640, 480))
    app._import_screen.render(surface)
    expected = app._import_screen.track_index
    app._handle_import_event(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_RETURN))
    assert app._import_job.operation == "load"
    assert app._import_job.options["track_index"] == expected
    assert app._import_job.options["content_id"] == job.result["content_id"]
    running = app._import_job
    app._handle_import_event(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_ESCAPE))
    assert running.cancelled and app._state == "menu" and app._playing_screen is None


@pytest.mark.parametrize("look", ["studio", "stage", "hifi"])
@pytest.mark.parametrize("size", [(1280, 720), (640, 480), (320, 240)])
def test_track_screen_mouse_keyboard_and_controls_stay_in_window(graphics, look, size):
    set_look(look)
    tracks = [{"index": i, "name": "Long track title " * 6, "strings": 4,
               "compatible": i % 2 == 0} for i in range(20)]
    screen = ImportScreen("song.gp5", tracks=tracks, selected=0, resume_available=True)
    surface = pygame.Surface(size)
    screen.render(surface)
    assert all(surface.get_rect().contains(rect) for rect in screen.view.buttons.values())
    assert screen.handle_event(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_DOWN)) == "selection"
    assert screen.track_index == 2
    screen.handle_event(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_END))
    assert screen.track_index == 18
    screen.render(surface)
    assert screen.view.scroll > 0
    button = screen.view.buttons["resume"]
    assert screen.handle_event(pygame.event.Event(pygame.MOUSEBUTTONDOWN, button=1, pos=button.center)) == "resume"
    assert screen.handle_event(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_ESCAPE)) == "cancel"


def test_progress_survives_content_move_and_tracks_remain_independent(tmp_path):
    path = tmp_path/"first.gp5"
    path.write_bytes(b"score")
    identity = library.content_identity(path)
    tracker = ProgressTracker()
    legacy = song_key(path, "guitar", 2)
    tracker.record_result(legacy, {"accuracy_percent": 91, "hits": 10, "total": 11})
    tracker.migrate_content_identity(path, "guitar", 2, identity)
    path = path.rename(tmp_path/"moved.gp5")
    record = tracker.get_best(song_key(path, "guitar", 2, content_id=identity))
    assert record.best_accuracy == 91
    assert tracker.get_best(song_key(path, "guitar", 1, content_id=identity)) is None


@pytest.mark.skipif(os.name != "nt", reason="Windows Job inheritance contract")
def test_windows_worker_cannot_execute_before_job_assignment(monkeypatch, tmp_path):
    from pickhero import import_jobs
    events = []
    class Process:
        pid = 123
        running = True
        def poll(self):
            return None if self.running else 1
        def kill(self):
            self.running = False
        def wait(self, timeout):
            self.running = False
    process = Process()
    def spawn(command, **options):
        assert options["creationflags"] & 0x4  # CREATE_SUSPENDED
        events.append("suspended")
        return process
    class Job:
        def __init__(self, child):
            assert child is process
            events.append("assigned")
        def close(self):
            events.append("job_closed")
    def resume(child):
        assert child is process and events == ["suspended", "assigned"]
        events.append("resumed")
    monkeypatch.setattr(import_jobs.subprocess, "Popen", spawn)
    monkeypatch.setattr(import_jobs, "_WindowsJob", Job)
    monkeypatch.setattr(import_jobs, "_resume_windows_process", resume)
    job = import_jobs.ImportJob("load", tmp_path/"score.gp5", "guitar")
    assert events == ["suspended", "assigned", "resumed"]
    job.cancel()
    assert events[-1] == "job_closed" and not process.running
    assert not job.directory.exists()
