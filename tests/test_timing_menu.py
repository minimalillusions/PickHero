"""Timing diagnostics never silently start audio or adjust the player's score."""
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pygame
import pytest

from pickhero.config import Config
from pickhero.i18n import set_language
from pickhero.timing_diagnostics import TimingDiagnostics
from pickhero.ui import app as app_module, timing_menu
from pickhero.ui.colors import set_look
from pickhero.ui.scrolling import PlayingScreen
from pickhero.tabs.timeline import NoteEvent, SongMetadata, Timeline


class Probe:
    def __init__(self, device, sample_rate):
        self.device = device
        self.is_running = False
        self.safe_to_resume = True
        self.status = "idle"
        self.result = None
        self.starts = 0
        self.closed = False

    def start(self):
        self.starts += 1
        self.is_running = True
        self.safe_to_resume = False
        self.status = "measuring"
        return True

    def stop(self):
        self.status = "cancelling"

    def close(self):
        self.closed = True

    def update(self):
        return {"status": self.status}

    def poll(self):
        return self.result


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    pygame.font.init()
    monkeypatch.setattr(timing_menu, "LatencyProbe", Probe)
    monkeypatch.setattr(PlayingScreen, "_init_midi_player", lambda *args: None)
    yield
    set_look("studio")
    set_language("de")


def event(key):
    return pygame.event.Event(pygame.KEYDOWN, key=key, mod=0, unicode="")


def report():
    audio = TimingDiagnostics()
    audio.record("note_report_lag_ms", 170)
    audio.record("callback_processing_ms", 1.5)
    return {"audio": audio.snapshot({"device_index": 4, "sample_rate": 48000,
            "hop_size": 512, "clock_source": "adc", "input_latency_ms": 10,
            "dropped_blocks": 0})}


@pytest.mark.parametrize("look", ["studio", "stage", "hifi"])
@pytest.mark.parametrize("size", [(320, 240), (640, 480), (876, 706), (1280, 720)])
@pytest.mark.parametrize("language", ["de", "en"])
def test_all_designs_scroll_and_retain_usable_actions(look, size, language):
    set_look(look)
    set_language(language)
    screen = timing_menu.TimingScreen(Config(), report=report())
    surface = pygame.Surface(size)
    screen.render(surface)
    assert screen._probe.starts == 0
    assert screen._probe.device == 4
    for action in ("start", "back", "export"):
        assert surface.get_rect().contains(screen._panel.hit[action])
    screen.handle_event(event(pygame.K_END))
    assert screen._panel.scroll == screen._panel.max_scroll
    screen.render(surface)
    assert screen.handle_event(event(pygame.K_ESCAPE)) == "back"
    assert screen._probe.starts == 0


def test_start_cancel_and_return_wait_for_stream_release():
    screen = timing_menu.TimingScreen(Config())
    screen.handle_event(event(pygame.K_m))
    assert screen._probe.starts == 1
    screen.update()
    assert screen.handle_event(event(pygame.K_ESCAPE)) is None
    assert screen.update() is None
    screen.handle_event(event(pygame.K_m))  # ignored while leaving
    assert screen._probe.starts == 1
    screen._probe.is_running = False
    screen._probe.safe_to_resume = True
    screen._probe.status = "cancelled"
    assert screen.update() == "back"


def test_export_is_explicit_unique_local_and_does_not_change_configuration(tmp_path, monkeypatch):
    monkeypatch.setattr(timing_menu.config_module, "CONFIG_DIR", tmp_path)
    config = Config()
    before = repr(config)
    screen = timing_menu.TimingScreen(config, report=report())
    assert not list(tmp_path.iterdir())
    for _ in range(2):
        screen.handle_event(event(pygame.K_e))
    paths = list((tmp_path / "diagnostics").glob("*.json"))
    assert len(paths) == 2
    data = json.loads(paths[0].read_text(encoding="utf-8"))
    assert data["roundtrip"] is None
    assert data["software"]["audio"]["metrics"]["note_report_lag_ms"]["median"] == 170
    assert "samples" not in data
    assert repr(config) == before
    assert screen._probe.starts == 0


def test_playing_result_snapshot_taken_before_suspend_and_return_is_paused(tmp_path):
    app = app_module.App(Config(songs_dir=str(tmp_path)))
    timeline = Timeline([NoteEvent(0, 500, 40, 6, 0)], SongMetadata(tempo=120))
    screen = PlayingScreen(timeline, config=app._config)
    capture = SimpleNamespace(diagnostic_snapshot=lambda: report()["audio"], stop=Mock())
    screen._audio_capture = capture
    app._playing_screen = screen
    app._state = "playing"
    screen._open_help()
    assert "timing" in [action.name for action in screen._panel.actions]
    assert screen._panel_action("timing") == "timing"
    assert screen._audio_capture is capture
    app._open_utility("timing")
    assert app._state == "timing"
    assert app._timing_menu._probe.device == 4
    assert app._timing_menu._report["audio"] == report()["audio"]
    capture.stop.assert_called_once()
    app._return_from_utility()
    assert app._state == "playing"
    assert app._playing_screen is screen
    assert not screen._playing


def test_check_snapshot_precedes_close_and_stale_capture_is_not_reused(tmp_path):
    app = app_module.App(Config(songs_dir=str(tmp_path)))
    capture = SimpleNamespace(diagnostic_snapshot=lambda: report()["audio"])
    app._check_menu = SimpleNamespace(_capture=capture, close=Mock())
    app._state = "check"
    app._open_utility("timing")
    assert app._timing_menu._report["audio"] == report()["audio"]
    app._return_from_utility()
    app._open_utility("timing")
    assert app._timing_menu._report["audio"] is None


def test_missing_device_failure_is_readable_and_does_not_start_again():
    screen = timing_menu.TimingScreen(Config())
    screen._probe.status = "failed"
    screen._probe.result = {"status": "failed", "reason": "no_compatible_output"}
    screen.update()
    assert all("timing.reason." not in str(section) for section in screen._panel.sections)
    screen.render(pygame.Surface((640, 480)))
    assert screen._probe.starts == 0


def test_failed_cleanup_cannot_resume_or_start_a_second_stream():
    screen = timing_menu.TimingScreen(Config())
    screen._probe.safe_to_resume = False
    screen._probe.status = "failed"
    screen._probe.result = {"status": "failed", "reason": "cleanup_failed",
                            "stream_closed": False}
    screen.update()
    assert screen._panel.actions[0].name == "cancel"
    screen._activate("start")
    assert screen._probe.starts == 0
    assert screen.handle_event(event(pygame.K_ESCAPE)) is None
    assert screen.update() is None
    assert not screen._leaving  # show retry after an unsuccessful close
    screen._probe.safe_to_resume = True
    assert screen.handle_event(event(pygame.K_ESCAPE)) == "back"
