"""Utilities return to the same practice session; failed imports stay usable."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pygame
import pytest

from pickhero.config import Config
from pickhero.tabs.timeline import NoteEvent, SongMetadata, Timeline
from pickhero.ui import app as module
from pickhero.ui.app import App
from pickhero.ui.colors import get_look, set_look
from pickhero.ui.scrolling import PlayingScreen


@pytest.fixture(autouse=True)
def graphics(monkeypatch):
    pygame.display.init()
    pygame.font.init()
    pygame.display.set_mode((876, 706))
    previous = get_look().key
    monkeypatch.setattr(PlayingScreen, "_init_midi_player", lambda *args: None)
    monkeypatch.setattr(module, "log_error", lambda *args: None)
    yield
    set_look(previous)
    pygame.display.quit()


@pytest.fixture
def application(tmp_path):
    app = App(Config(instrument="bass_ukulele", songs_dir=str(tmp_path)))
    app._menu = SimpleNamespace(is_searching=False, refresh_device_name=Mock(),
                                render=Mock(), scan_files=Mock(), handle_event=Mock(),
                                catalogue=SimpleNamespace(stop=Mock()))
    return app


def key(value):
    return pygame.event.Event(pygame.KEYDOWN, key=value, mod=0, unicode="")


@pytest.mark.parametrize("value", [pygame.K_d, pygame.K_c, pygame.K_g, pygame.K_l])
def test_resume_details_receive_keys_before_library_shortcuts(application, monkeypatch, value):
    application._menu._resume_panel = object()
    application._menu.handle_event.return_value = None
    utility = Mock()
    language = Mock()
    monkeypatch.setattr(application, "_open_utility", utility)
    monkeypatch.setattr(application, "_cycle_language", language)
    event = key(value)
    application._handle_menu_event(event)
    application._menu.handle_event.assert_called_once_with(event)
    utility.assert_not_called()
    language.assert_not_called()


@pytest.mark.parametrize("first", ["device", "check", "calibrate"])
def test_setup_hops_preserve_song_and_return_origin(application, monkeypatch, first):
    app = application
    tuning = {1: 43, 2: 38, 3: 33, 4: 26}
    timeline = Timeline([NoteEvent(i * 500, 400, 26, 4, 0) for i in range(16)],
                        SongMetadata(tempo=120, tuning=tuning))
    screen = PlayingScreen(timeline, config=app._config)
    screen._playback_ms = 1750
    screen._tempo_factor = .65
    screen._loop_start_ms, screen._loop_end_ms, screen._loop_enabled = 1000, 4000, True
    screen._begin_attempt(1000)
    matcher = screen._matcher
    app._playing_screen = screen
    app._state = "playing"
    stream_events = []
    opened = []

    class Utility:
        def __init__(self, config, *, instrument=None):
            assert not screen._playing
            assert screen._audio_capture is None
            stream_events.append("open")
            self.instrument = instrument
            self.action = "back"
            opened.append(self)

        def close(self):
            stream_events.append("close")

        def handle_event(self, event):
            return self.action

    for name in ("DeviceMenuScreen", "CheckScreen", "CalibrationMenuScreen"):
        monkeypatch.setattr(module, name, Utility)
    app._open_utility(first)
    for action in ("check", "calibrate", "device"):
        app._open_utility(action)
    app._config.audio.device_index = 7
    app._config.audio.noise_gate_db = -51
    app._handle_device_event(key(pygame.K_ESCAPE))
    assert app._state == "playing"
    assert app._playing_screen is screen and screen._matcher is matcher
    assert screen._playback_ms == 1750
    assert screen._tempo_factor == .65
    assert (screen._loop_start_ms, screen._loop_end_ms, screen._loop_enabled) == (1000, 4000, True)
    assert screen._instrument.tuning == tuning
    assert screen._noise_gate_db == -51
    assert not screen._playing
    assert stream_events == ["open", "close"] * 4
    assert opened[1].instrument.tuning == tuning
    assert opened[2].instrument.tuning == tuning


def test_setup_from_library_returns_to_its_existing_selection(application, monkeypatch):
    app = application
    app._menu._selected = 23
    utility = Mock()
    utility.handle_event.return_value = "back"
    monkeypatch.setattr(module, "CheckScreen", lambda config: utility)
    app._handle_menu_event(key(pygame.K_c))
    assert app._state == "check"
    app._handle_check_event(key(pygame.K_ESCAPE))
    assert app._state == "menu" and app._menu._selected == 23
    utility.close.assert_called_once()
    app._menu.scan_files.assert_not_called()


@pytest.mark.parametrize("look", ["studio", "stage", "hifi"])
@pytest.mark.parametrize("size", [(1280, 720), (640, 480), (320, 240)])
def test_failed_import_has_visible_keyboard_and_mouse_escape(application, monkeypatch, look, size):
    app = application
    set_look(look)
    monkeypatch.setattr(module, "load_gp_file", Mock(side_effect=ValueError("invalid input")))
    app._load_song(Path("song with a very long name " * 5 + ".gp"))
    assert app._state == "menu" and app._load_error is not None
    surface = pygame.Surface(size)
    app._render(surface)
    assert len(app._error_buttons) == 2
    assert all(surface.get_rect().contains(rect) for rect in app._error_buttons)
    assert not app._error_buttons[0].colliderect(app._error_buttons[1])
    event = pygame.event.Event(pygame.MOUSEBUTTONDOWN, button=1,
                              pos=app._error_buttons[0].center)
    monkeypatch.setattr(pygame.event, "get", lambda: [event])
    app._process_events(surface)
    assert app._load_error is None
    app._menu.handle_event.assert_not_called()
    assert app._state == "menu"


def test_retry_can_open_the_same_file_after_it_is_fixed(application, monkeypatch):
    app = application
    timeline = Timeline([NoteEvent(0, 500, 28, 4, 0)], SongMetadata())
    load = Mock(side_effect=[ValueError("invalid input"), timeline])
    monkeypatch.setattr(module, "load_gp_file", load)
    monkeypatch.setattr(module, "extract_backing_track", lambda *a, **k: None)
    app._load_song(Path("changed.gp5"))
    assert app._load_error is not None
    app._handle_load_error(key(pygame.K_TAB))
    app._handle_load_error(key(pygame.K_RETURN))
    assert app._load_error is None and app._state == "playing"
    assert load.call_count == 2


def test_empty_import_explains_why_it_cannot_start(application, monkeypatch):
    monkeypatch.setattr(module, "load_gp_file", lambda *a, **k: Timeline([]))
    application._load_song(Path("empty.gp5"))
    assert application._load_error == (Path("empty.gp5"), True)
    application._handle_load_error(key(pygame.K_ESCAPE))
    assert application._state == "menu" and application._load_error is None


@pytest.mark.parametrize("look", ["studio", "stage", "hifi"])
@pytest.mark.parametrize("size", [(640, 480), (1280, 720)])
def test_real_mouse_setup_flow_releases_input_and_keeps_drop_tuning(application, monkeypatch, look, size):
    """Exercise the actual screens through App, including their drawn buttons."""
    from pickhero.audio import input as audio_input
    from pickhero.ui import calibration_menu, check_menu

    active = []
    opened = []

    class Capture:
        def __init__(self, config, *, instrument=None):
            self.instrument = instrument or config.get_instrument()
            self.detector = SimpleNamespace(set_noise_gate_db=Mock(), reset=Mock(),
                                           expected_delay_ms=100, noise_gate_db=-45)
            self.sample_rate = 44100
            opened.append(self)

        def start(self):
            assert not active, "Two screens must never hold the input at once"
            active.append(self)

        def stop(self):
            if self in active:
                active.remove(self)

        def get_signal_db(self): return -30.0
        def get_tuner_data(self): return (36.7, .99)
        def get_notes(self): return []
        def capture_elapsed_ms(self): return 0.0

    for owner in (audio_input, calibration_menu, check_menu):
        monkeypatch.setattr(owner, "AudioCapture", Capture)
    app = application
    set_look(look)
    tuning = {1: 43, 2: 38, 3: 33, 4: 26}
    song = Timeline([NoteEvent(i * 500, 400, 26, 4, 0) for i in range(16)],
                    SongMetadata(tempo=120, tuning=tuning))
    screen = PlayingScreen(song, config=app._config)
    screen._playback_ms = 1750
    screen._tempo_factor = .65
    screen._loop_start_ms, screen._loop_end_ms, screen._loop_enabled = 1000, 4000, True
    matcher = screen._matcher
    app._state, app._playing_screen = "playing", screen
    canvas = pygame.Surface(size)

    def click(rect):
        event = pygame.event.Event(pygame.MOUSEBUTTONDOWN, button=1, pos=rect.center)
        monkeypatch.setattr(pygame.event, "get", lambda: [event])
        app._process_events(canvas)

    app._render(canvas)
    click(screen._button_rects["setup"][0])
    app._render(canvas)
    click(screen._panel.hit["check"])
    assert app._state == "check" and len(active) == 1
    assert app._check_menu._instrument.tuning == tuning
    app._render(canvas)
    click(app._check_menu._ui.buttons["gate_down"])
    expected_gate = app._config.audio.noise_gate_db
    assert active[0].detector.set_noise_gate_db.call_args.args == (expected_gate,)
    click(app._check_menu._ui.buttons["calibrate"])
    assert app._state == "calibration" and not active
    app._render(canvas)
    click(app._calibration_menu._ui.buttons["start"])
    assert len(active) == 1 and active[0].instrument.tuning == tuning
    app._render(canvas)
    click(app._calibration_menu._ui.buttons["back"])
    assert app._state == "playing" and not active
    assert app._playing_screen is screen and screen._matcher is matcher
    assert not screen._playing and screen._playback_ms == 1750
    assert screen._tempo_factor == .65
    assert (screen._loop_start_ms, screen._loop_end_ms, screen._loop_enabled) == (1000, 4000, True)
    assert screen._noise_gate_db == expected_gate
    app._handle_playing_event(key(pygame.K_SPACE))
    assert screen._playing and len(active) == 1
    assert len(opened) == 3 and active[0].instrument.tuning == tuning
    screen.stop_audio()
    assert not active
