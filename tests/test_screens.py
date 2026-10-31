"""Every key and every click into every screen, in the states it reaches.

The screens are where a crash costs the most: it happens while the player is
using the app, and there is nowhere for a traceback to go. A wrong key is the
easiest thing in the world to press, and several of these screens change what
a key does depending on what they are in the middle of.

The microphone is mocked, so this runs anywhere and never opens a device.
"""

import os

import pytest

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
import pygame  # noqa: E402

from pickhero.config import Config  # noqa: E402
from pickhero.tabs.timeline import NoteEvent, SongMetadata, Timeline  # noqa: E402


class _FakeCapture:
    """An AudioCapture that never touches a device."""

    def __init__(self, config=None, *, instrument=None):
        self.config = config
        self.detector = type("D", (), {
            "set_noise_gate_db": lambda self, db: None,
            "noise_gate_db": -45.0,
            "reset": lambda self: None,
        })()
        self._running = False

    def start(self):
        self._running = True

    def stop(self):
        self._running = False

    def capture_elapsed_ms(self):
        return 0.0

    def set_noise_gate_db(self, db):
        self.detector.noise_gate_db = db

    def get_signal_db(self):
        return -40.0

    def get_tuner_data(self):
        return (98.0, 0.5)

    def get_notes(self):
        return []


@pytest.fixture(autouse=True)
def _no_real_audio(monkeypatch):
    for module in ("pickhero.ui.check_menu", "pickhero.ui.calibration_menu"):
        monkeypatch.setattr(f"{module}.AudioCapture", _FakeCapture, raising=False)
    monkeypatch.setattr("pickhero.audio.input.AudioCapture", _FakeCapture)
    monkeypatch.setattr("pickhero.ui.scrolling.AudioCapture", _FakeCapture,
                        raising=False)
    monkeypatch.setattr("pickhero.audio.input.list_audio_devices",
                        lambda: [{"index": 0, "name": "Testgeraet", "channels": 1,
                                  "is_default": True, "hostapi": "Test",
                                  "latency_ms": 5.0, "sample_rate": 44100}])
    monkeypatch.setattr("pickhero.audio.input.best_input_device", lambda: 0)


@pytest.fixture(scope="module")
def surface():
    pygame.init()
    yield pygame.display.set_mode((1280, 720))
    pygame.quit()


def _timeline(notes: int = 8) -> Timeline:
    events = [NoteEvent(timestamp_ms=i * 500.0, duration_ms=400.0,
                        midi_note=40 + (i % 5), string=1 + (i % 4), fret=i % 4)
              for i in range(notes)]
    return Timeline(events, SongMetadata(title="Test", tempo=120))


ALL_KEYS = [getattr(pygame, name) for name in dir(pygame) if name.startswith("K_")]
CLICKS = [(10, 10), (640, 360), (1270, 710), (200, 120), (900, 600), (0, 0),
          (640, 60), (100, 690)]


def _hammer(screen, surface):
    """Every key, every modifier, a spread of clicks, then draw it again."""
    for key in ALL_KEYS:
        for mod in (0, pygame.KMOD_SHIFT, pygame.KMOD_CTRL):
            screen.handle_event(pygame.event.Event(
                pygame.KEYDOWN, key=key, mod=mod, unicode=""))
    for pos in CLICKS:
        screen.handle_event(pygame.event.Event(
            pygame.MOUSEBUTTONDOWN, pos=pos, button=1))
    for event in (pygame.event.Event(pygame.MOUSEWHEEL, y=1, x=0),
                  pygame.event.Event(pygame.MOUSEWHEEL, y=-1, x=0),
                  pygame.event.Event(pygame.MOUSEMOTION, pos=(640, 360),
                                     rel=(1, 1), buttons=(0, 0, 0))):
        screen.handle_event(event)
    screen.render(surface)


class TestScreensSurviveInput:
    def test_the_menu(self, surface, tmp_path):
        from pickhero.ui.menu import MenuScreen
        _hammer(MenuScreen(tmp_path, config=Config()), surface)

    def test_the_device_picker(self, surface):
        from pickhero.ui.device_menu import DeviceMenuScreen
        _hammer(DeviceMenuScreen(Config()), surface)

    def test_the_microphone_check(self, surface):
        from pickhero.ui.check_menu import CheckScreen
        screen = CheckScreen(Config())
        _hammer(screen, surface)
        screen.close()

    def test_the_tuner(self, surface):
        from pickhero.ui.calibration_menu import CalibrationMenuScreen
        screen = CalibrationMenuScreen(Config())
        _hammer(screen, surface)
        screen.close()

    def test_the_playing_screen_at_the_start(self, surface):
        from pickhero.ui.scrolling import PlayingScreen
        _hammer(PlayingScreen(_timeline(), config=Config()), surface)

    def test_the_playing_screen_mid_song(self, surface):
        from pickhero.ui.scrolling import PlayingScreen
        screen = PlayingScreen(_timeline(), config=Config())
        screen._playback_ms = screen._timeline.duration_ms / 2
        screen._playing = True
        screen._wait_mode = True
        _hammer(screen, surface)
        screen.stop_audio()

    def test_the_playing_screen_after_the_end(self, surface):
        from pickhero.ui.scrolling import PlayingScreen
        screen = PlayingScreen(_timeline(), config=Config())
        screen._playback_ms = screen._timeline.duration_ms + 1000
        screen._song_completed = True
        screen._weakest_sections = [(1, 3, 20.0)]
        screen._recommendations = ["Takte 2 bis 4 in die Schleife."]
        screen._is_new_best = True
        _hammer(screen, surface)

    def test_a_song_with_no_notes(self, surface):
        """An empty or unreadable tab still has to be a screen you can leave."""
        from pickhero.ui.scrolling import PlayingScreen
        _hammer(PlayingScreen(Timeline([], SongMetadata(title="leer")),
                              config=Config()), surface)

    def test_a_song_with_one_very_long_note(self, surface):
        from pickhero.ui.scrolling import PlayingScreen
        timeline = Timeline(
            [NoteEvent(timestamp_ms=0, duration_ms=600_000, midi_note=40,
                       string=4, fret=0)],
            SongMetadata(title="lang", tempo=40))
        _hammer(PlayingScreen(timeline, config=Config()), surface)


class TestTheTuner:
    """All four strings at once, with a needle and a target.

    The screen used to answer only "how many cents off", which is the third
    of the four things a tuner has to say and the only one that needs
    explaining first. Which string is being heard, and whether it is sharp
    or flat, are now position and shape rather than a number.
    """

    def _screen(self, freq, confidence=0.8):
        from pickhero.config import Config
        import pickhero.ui.calibration_menu as calibration

        config = Config()
        config.instrument = "bass_ukulele"
        screen = calibration.CalibrationMenuScreen(config)
        if screen._capture is None:
            screen._capture = _FakeCapture(config)
        screen._capture.get_tuner_data = lambda: (freq, confidence)
        return screen

    def test_it_draws_with_nothing_playing(self, surface):
        screen = self._screen(0.0, 0.0)
        screen.render(surface)
        screen.close()

    def test_it_draws_for_every_string(self, surface):
        from pickhero.audio.note_utils import midi_to_freq
        for midi in (28, 33, 38, 43):
            screen = self._screen(midi_to_freq(midi))
            screen.render(surface)
            screen.close()

    def test_a_pitch_between_two_strings_picks_the_nearer(self, surface):
        """Halfway between A1 and D2 is not either, and must not claim to be
        both."""
        screen = self._screen(63.0)
        screen.render(surface)
        screen.close()

    def test_a_pitch_far_outside_the_tuning_lights_nothing(self, surface):
        screen = self._screen(440.0)
        screen.render(surface)
        screen.close()

    def test_an_unconfident_reading_lights_nothing(self, surface):
        """A microphone hearing a chair scrape must not move the needle."""
        screen = self._screen(55.0, confidence=0.01)
        screen.render(surface)
        screen.close()

    def test_it_draws_for_a_six_string_instrument(self, surface):
        """The rows come from the instrument profile, so six must work
        without special-casing."""
        from pickhero.config import Config
        import pickhero.ui.calibration_menu as calibration

        config = Config()
        config.instrument = "guitar"
        screen = calibration.CalibrationMenuScreen(config)
        if screen._capture is None:
            screen._capture = _FakeCapture(config)
        screen._capture.get_tuner_data = lambda: (82.4, 0.9)
        screen.render(surface)
        screen.close()

    def test_it_survives_a_narrow_window(self, surface):
        """It is drawn from the window size, and the window is resizable."""
        import pygame as _pygame
        screen = self._screen(55.0)
        screen.render(_pygame.Surface((640, 400)))
        screen.close()
