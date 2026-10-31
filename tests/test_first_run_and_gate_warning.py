"""The input guide opens itself, and a high gate is said where the player is playing.

Both of these existed and neither reached them. The guide walks instrument,
device, level, tuning and a test phrase -- the exact route past a microphone
that is too quiet -- but nothing opened it, so it had to be found. And the
check screen has always warned about a gate above HIGH_GATE_DB, on a screen
that also had to be found.

Measured on a real take: a gate of -26 dB against plucks peaking between -8
and -32 dB lost a third of the low E strikes, and the score on the same piece
fell by more than half. The play screen said nothing, because
_quiet_warning resets the moment the signal clears the gate and their loud notes
cleared it easily.
"""

from types import SimpleNamespace
from unittest.mock import Mock

import pygame
import pytest

from pickhero import i18n
from pickhero.config import Config, _validated_settings
from pickhero.tabs.timeline import NoteEvent, SongMetadata, Timeline
from pickhero.ui.app import App
from pickhero.ui.check_menu import HIGH_GATE_DB
from pickhero.ui.scrolling import PlayingScreen


@pytest.fixture
def quiet_app(monkeypatch):
    """An app whose loop quits at once, so run() only does its setup."""
    from pickhero.ui import app as module
    monkeypatch.setattr(module, "validate_device_index", lambda index: True)
    monkeypatch.setattr(App, "_build_menu", lambda self: None)
    monkeypatch.setattr(pygame.event, "get",
                        lambda: [pygame.event.Event(pygame.QUIT)])

    def make(**settings):
        config = Config(**settings)
        config.save = lambda: None
        return App(config)
    return make


@pytest.fixture
def play_screen():
    def make(gate, *, capture=True, enabled=True):
        config = Config(instrument="bass_ukulele")
        config.save = lambda: None
        notes = [NoteEvent(index * 500, 100, (28, 33, 38, 43)[index % 4],
                           4 - index % 4, 0) for index in range(8)]
        screen = PlayingScreen(Timeline(notes, SongMetadata(tempo=120)),
                               config=config)
        screen._audio_enabled = enabled
        screen._audio_capture = SimpleNamespace() if capture else None
        screen._noise_gate_db = gate
        return screen
    return make


# ── the guide opens itself once ──────────────────────────────────────────

def test_a_fresh_install_opens_the_input_guide(quiet_app):
    app = quiet_app(setup_done=False)
    app.run()
    assert app._state == "wizard"


def test_an_install_that_has_seen_it_goes_straight_to_the_menu(quiet_app):
    app = quiet_app(setup_done=True)
    app.run()
    assert app._state == "menu"


def _leave_guide(app, reports):
    saved = []
    app._config.save = lambda: saved.append(app._config.setup_done)
    app._setup_guide = SimpleNamespace(handle_event=lambda event: "back",
                                       reports=reports)
    app._guide_origin = "menu"
    app._handle_guide_event(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_ESCAPE))
    return saved


def test_running_a_check_in_the_guide_records_that_it_was_used(quiet_app):
    app = quiet_app(setup_done=False)
    saved = _leave_guide(app, {"level": {"db": -18}})
    assert app._config.setup_done is True
    assert saved == [True], "the flag has to reach the file, not only the object"
    assert app._setup_guide is None


def test_blazing_through_the_guide_certifies_nothing(quiet_app):
    """Arrow keys through five stages measure nothing, so nothing is recorded.

    Otherwise the guide would hide itself from the one person it exists for.
    tests/test_setup_guide.py states the same rule as "no automatic setting
    changes", and this is the half of it that keeps the guide coming back.
    """
    app = quiet_app(setup_done=False)
    saved = _leave_guide(app, {})
    assert app._config.setup_done is False
    assert saved == []
    assert app._setup_guide is None


def test_the_flag_is_not_written_again_once_it_is_set(quiet_app):
    app = quiet_app(setup_done=True)
    app._config.save = Mock()
    app._mark_setup_seen()
    app._config.save.assert_not_called()


def test_a_broken_flag_in_the_file_is_ignored_rather_than_believed():
    """Only a real bool counts, as with every other boolean setting.

    A truthy string would otherwise skip the guide for someone who has never
    seen it, which is the one case it exists for.
    """
    assert _validated_settings({"setup_done": True})["setup_done"] is True
    assert _validated_settings({"setup_done": False})["setup_done"] is False
    for rubbish in ("true", 1, None, [], {}):
        assert "setup_done" not in _validated_settings({"setup_done": rubbish})


def test_the_flag_survives_a_round_trip_through_the_file(tmp_path, monkeypatch):
    from pickhero import config as module
    monkeypatch.setattr(module, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(module, "CONFIG_FILE", tmp_path / "settings.json")
    written = Config(setup_done=True)
    written.save()
    assert Config.load().setup_done is True


# ── a high gate is said on the play screen too ───────────────────────────

def test_a_high_gate_is_said_while_she_plays(play_screen):
    screen = play_screen(-26)
    message = screen._high_gate_warning()
    assert message is not None
    assert "-26" in message or "−26" in message, message


@pytest.mark.parametrize("gate", [-36, -40, -45, -80])
def test_a_sensible_gate_says_nothing(play_screen, gate):
    assert play_screen(gate)._high_gate_warning() is None


def test_the_boundary_is_the_one_the_check_screen_uses(play_screen):
    """Imported, not copied: the two screens must not come to disagree."""
    assert play_screen(HIGH_GATE_DB)._high_gate_warning() is not None
    assert play_screen(HIGH_GATE_DB - 1)._high_gate_warning() is None


@pytest.mark.parametrize("missing", ["capture", "enabled"])
def test_nothing_is_said_without_a_running_input(play_screen, missing):
    screen = play_screen(-26, capture=missing != "capture",
                         enabled=missing != "enabled")
    assert screen._high_gate_warning() is None


def test_her_own_case_would_have_been_named(play_screen):
    """-26.1 dB is a value a real room measurement produced.

    It is above HIGH_GATE_DB, so the check screen flagged it from the first
    day. This is the assertion that it now also reaches the screen the player was
    actually looking at.
    """
    assert play_screen(-26.115131378173828)._high_gate_warning() is not None


@pytest.mark.parametrize("language", ["de", "en"])
def test_the_warning_names_the_value_and_the_key(play_screen, monkeypatch, language):
    monkeypatch.setattr(i18n, "_current", language)
    message = play_screen(-26)._high_gate_warning()
    assert message and not message.startswith("play.")
    assert "{" not in message, "an unfilled placeholder reached the banner"
    assert "X" in message, message


def test_an_acute_silence_still_wins_over_the_standing_gate_warning(play_screen):
    """Both can be true at once, and "I cannot hear you" is the urgent one.

    _quiet_warning already tells them to press X. The gate warning is the
    quieter, standing case: the level looks fine and the soft notes vanish.
    """
    screen = play_screen(-26)
    monkey = lambda: "ich hoere dich nicht"
    screen._quiet_warning = monkey
    screen._unplayable_warning = lambda: None
    screen._attempt_issues = []
    screen._tuning_warning = None
    screen._progress_tracker = SimpleNamespace(last_save_error=None)
    assert screen._current_warning() == "ich hoere dich nicht"


def test_the_gate_warning_reaches_the_banner_when_nothing_else_does(play_screen):
    screen = play_screen(-26)
    screen._quiet_warning = lambda: None
    screen._unplayable_warning = lambda: None
    screen._attempt_issues = []
    screen._tuning_warning = None
    screen._progress_tracker = SimpleNamespace(last_save_error=None)
    assert screen._current_warning() == screen._high_gate_warning()
    assert screen._current_warning() is not None
