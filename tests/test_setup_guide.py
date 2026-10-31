"""Guided setup observes input honestly and returns to the paused song."""
from types import SimpleNamespace
from unittest.mock import Mock
import os
from pathlib import Path

import pygame
import pytest

from pickhero import i18n
from pickhero.audio.detector import DetectedNote
from pickhero.audio.input import TimestampedNote
from pickhero.audio.note_utils import midi_to_freq
from pickhero.config import Config
from pickhero.instruments import BASS_UKULELE, GUITAR, with_tuning
from pickhero.ui.app import App
from pickhero.ui.check_menu import CheckScreen, FRAMES_TO_CONFIRM
from pickhero.ui.colors import set_look
from pickhero.ui.setup_guide import SetupGuideScreen


def key(code):
    return pygame.event.Event(pygame.KEYDOWN, key=code, mod=0)


class Input:
    def __init__(self):
        self.notes = []
        self.freq = 0
        self.confidence = 0
        self.level = -120
        self.issue = None
        self.stopped = False

    def start(self):
        pass

    def stop(self):
        self.stopped = True

    def get_notes(self):
        result, self.notes = self.notes, []
        return result

    def get_signal_db(self):
        return self.level

    def get_tuner_data(self):
        return self.freq, self.confidence

    def health_issue(self):
        return self.issue


@pytest.fixture
def input_factory(monkeypatch):
    created = []

    def factory(*args, **kwargs):
        capture = Input()
        created.append(capture)
        return capture

    monkeypatch.setattr("pickhero.ui.check_menu.AudioCapture", factory)
    return created


@pytest.fixture(autouse=True)
def ui_state():
    previous = i18n.current_language()
    pygame.font.init()
    yield
    i18n.set_language(previous)
    set_look("studio")


def note(midi, timestamp, confidence=.9, onset=True):
    return TimestampedNote(DetectedNote(midi, midi_to_freq(midi), confidence, "note", onset), timestamp)


def test_opening_and_skipping_guide_does_not_measure_or_certify_anything(tmp_path, monkeypatch):
    config = Config(songs_dir=str(tmp_path))
    config.save = Mock(side_effect=AssertionError("No automatic setting changes"))
    app = App(config)
    app._open_guide()
    assert app._state == "wizard" and app._setup_guide.reports == {}
    for _ in range(5):
        app._handle_guide_event(key(pygame.K_RIGHT))
    assert app._state == "menu" and app._setup_guide is None
    assert app._progress._data == {}
    config.save.assert_not_called()


def test_instrument_change_is_explicit_and_rebuilds_library_on_return(tmp_path):
    config = Config(instrument="guitar", songs_dir=str(tmp_path))
    app = App(config)
    app._build_menu = Mock()
    app._open_guide()
    app._setup_guide.reports["phrase"] = {"target_hits": 3}
    app._handle_guide_event(key(pygame.K_i))
    assert config.instrument == "bass_ukulele"
    assert config.active_strings == [True] * 4
    assert app._setup_guide.reports == {}
    assert len(list((tmp_path / "Uebungen").glob("*.gp5"))) == 10
    app._handle_guide_event(key(pygame.K_ESCAPE))
    app._build_menu.assert_called_once()


def test_live_check_returns_observation_before_releasing_stream(input_factory):
    app = App(Config(instrument="bass_ukulele"))
    app._open_guide()
    app._setup_guide.stage = 4
    app._setup_guide.refresh()
    app._handle_guide_event(key(pygame.K_o))
    capture = input_factory[-1]
    capture.level = -25
    capture.notes = [note(28, ts) for ts in (0, 2000, 4000)]
    app._check_menu.update()
    app._handle_check_event(key(pygame.K_ESCAPE))
    assert app._state == "wizard" and capture.stopped
    report = app._setup_guide.reports["phrase"]
    assert report["target_hits"] == 3 and report["reliable"]
    assert app._check_menu is None
    assert app._progress._data == {}


def test_song_tuning_and_position_are_preserved_across_guide(input_factory):
    config = Config(instrument="bass_ukulele")
    app = App(config)
    tuned = with_tuning(BASS_UKULELE, {1: 43, 2: 38, 3: 33, 4: 26})
    playing = SimpleNamespace(_instrument=tuned, _playback_ms=8123,
                              suspend_for_setup=Mock(), resume_from_setup=Mock())
    app._playing_screen, app._state = playing, "playing"
    app._open_guide()
    playing.suspend_for_setup.assert_called_once()
    app._handle_guide_event(key(pygame.K_i))
    assert config.instrument == "bass_ukulele"
    assert not app._setup_guide.can_change_instrument
    app._setup_guide.stage = 3
    app._setup_guide.refresh()
    app._handle_guide_event(key(pygame.K_o))
    assert app._check_menu._instrument == tuned
    assert app._check_menu._purpose == "tune"
    app._handle_check_event(key(pygame.K_ESCAPE))
    assert app._state == "wizard"
    app._handle_guide_event(key(pygame.K_ESCAPE))
    assert app._playing_screen is playing and playing._playback_ms == 8123
    assert app._state == "playing"
    playing.resume_from_setup.assert_called_once()


def test_phrase_needs_separate_confident_exact_pitch_attacks(input_factory):
    screen = CheckScreen(Config(instrument="bass_ukulele"), purpose="phrase")
    capture = input_factory[-1]
    capture.notes = [note(40, 0), note(28, 1000, onset=False), note(28, 2000, confidence=.1),
                     note(28, 3000), note(28, 3200), note(28, 5000), note(28, 7000)]
    screen.update()
    assert screen._target_hits == 3
    screen.update()
    assert screen._target_hits == 3
    capture.issue = "input_overflow"
    assert not screen.diagnostic_snapshot()["reliable"]
    assert screen.diagnostic_snapshot()["error"] == "input_overflow"
    screen._error = "disconnected"
    screen._activate("retry")
    assert screen._target_hits == 0 and not any(screen._confirmed.values())


@pytest.mark.parametrize("offset,octave,confidence,confirmed", [
    (0, 0, .9, True), (14, 0, .9, True), (25, 0, .9, False),
    (0, 12, .9, False), (0, 0, .1, False),
])
def test_tuning_requires_same_octave_and_fifteen_cent_window(
        input_factory, offset, octave, confidence, confirmed):
    screen = CheckScreen(Config(instrument="bass_ukulele"), purpose="tune")
    capture = input_factory[-1]
    capture.freq = midi_to_freq(28 + octave) * 2 ** (offset / 1200)
    capture.confidence = confidence
    for _ in range(FRAMES_TO_CONFIRM):
        screen.update()
    assert screen._confirmed[4] is confirmed


def test_changed_device_discards_prior_setup_observations():
    screen = SetupGuideScreen(Config())
    screen.reports = {"level": {"has_signal": True}, "tune": {"confirmed": 6},
                      "phrase": {"target_hits": 3}}
    screen.device_changed()
    assert screen.device_selected and screen.reports == {}


def test_adjusting_input_invalidates_earlier_observations_but_keeps_new_visit():
    config = Config()
    screen = SetupGuideScreen(config)
    screen.reports = {"level": {"has_signal": True}, "tune": {"confirmed": 6}}
    config.audio.noise_gate_db = -55
    current = {"target_hits": 3, "reliable": True}
    screen.record_check("phrase", current)
    assert screen.reports == {"phrase": current}
    config.audio.noise_gate_db = -45
    screen.refresh()
    assert screen.reports == {}


@pytest.mark.parametrize("language", ["de", "en"])
@pytest.mark.parametrize("look", ["studio", "stage", "hifi"])
@pytest.mark.parametrize("size", [(320, 240), (640, 480), (1280, 720)])
def test_all_setup_steps_have_reachable_controls_and_localized_text(language, look, size):
    i18n.set_language(language)
    set_look(look)
    screen = SetupGuideScreen(Config(), instrument=GUITAR)
    surface = pygame.Surface(size)
    for stage in range(5):
        screen.stage = stage
        screen.refresh()
        screen.render(surface)
        panel = screen._panel
        assert not panel.title.startswith("setup.")
        for heading, paragraphs in panel.sections:
            assert not heading.startswith("setup.")
            assert all(not text.startswith("setup.") and "{" not in text for text in paragraphs)
        boxes = [panel.hit[action.name] for action in panel.actions]
        assert all(surface.get_rect().contains(box) for box in boxes)
        assert all(not first.colliderect(other) for i, first in enumerate(boxes) for other in boxes[i + 1:])
        assert all(not panel.body.colliderect(box) for box in boxes)
        assert panel.body.height >= 20
        if panel.max_scroll:
            screen.handle_event(pygame.event.Event(pygame.MOUSEWHEEL, y=-1))
            assert panel.scroll > 0
        folder = os.environ.get("PICKHERO_SETUP_PREVIEW_DIR")
        if folder:
            path = Path(folder); path.mkdir(parents=True, exist_ok=True)
            pygame.image.save(surface, str(path / f"guide-{look}-{language}-{size[0]}-{stage}.png"))
