"""A pause during count-in must not silently discard the actual test run."""
import json
from unittest.mock import Mock

import pygame
import pytest

from pickhero import config as config_module, i18n
from pickhero.audio.input import AudioCapture
from pickhero.ui.colors import set_look
from pickhero.ui.recording_series import BANNER_HEIGHT, MAX_SINGLE_SECONDS
from tests.test_recording_navigation import app, key  # noqa: F401
from tests.test_recording_series import audio, fake_input, send  # noqa: F401


def start_single(app, label='acoustic'):
    app._handle_menu_event(key(pygame.K_u))
    app._playing_screen._panel_action('record:' + label)
    assert app._recording_series.single_piece
    assert app._playing_screen._test_recording._attached


@pytest.mark.parametrize('label', ['acoustic', 'amplified', 'direct'])
def test_count_in_pause_tempo_change_resume_complete_then_normal_play(app, label):
    start_single(app, label)
    screen = app._playing_screen
    first = audio(app)
    app._handle_playing_event(key(pygame.K_SPACE))
    session = json.loads((first.folder / 'session.json').read_text())
    assert session['finish']['reason'] == 'pause'
    assert session['finish']['ended_during_count_in']
    assert app._recording_series.label == label
    screen.set_tempo_factor(.5)
    app._handle_playing_event(key(pygame.K_SPACE))
    second = audio(app, (.05, -.02))
    assert second is not first and second.metadata['state']['tempo_factor'] == .5
    assert second.metadata['series_id'] == first.metadata['series_id']
    assert second.metadata['series_segment'] == 2 and second.metadata['recording_scope'] == 'piece'
    assert second.metadata['label'] == label
    screen._playback_ms = screen._end_ms
    screen._stop_audio(recording_complete=True)
    screen._test_recording.result({'hits': 1, 'total': 1})
    assert second.done.is_set() and not app._recording_series.visible
    assert (second.folder / 'result.json').exists()
    assert not (first.folder / 'result.json').exists()
    screen._start_practice(0, force_count_in=True)
    assert not screen._test_recording._attached
    assert len(list((config_module.CONFIG_DIR / 'recordings').iterdir())) == 2


@pytest.mark.parametrize('state', ['playing', 'paused', 'setup'])
def test_leaving_single_piece_never_records_next_piece(app, state):
    start_single(app)
    first = audio(app)
    screen = app._playing_screen
    if state in ('paused', 'setup'):
        app._handle_playing_event(key(pygame.K_SPACE))
    if state == 'setup':
        screen.suspend_for_setup()
        assert screen._audio_capture is None
    app._handle_playing_event(key(pygame.K_ESCAPE))
    assert app._state == 'menu'
    assert first.done.is_set() and not app._recording_series.visible
    app._handle_menu_event(key(pygame.K_DOWN))
    app._handle_menu_event(key(pygame.K_RETURN))
    app._handle_playing_event(key(pygame.K_SPACE))
    assert app._playing_screen._test_recording.take is None


@pytest.mark.parametrize('state', ['playing', 'paused'])
def test_u_ends_single_session_without_silently_resuming_it(app, monkeypatch, state):
    start_single(app)
    take = audio(app)
    if state == 'paused':
        app._handle_playing_event(key(pygame.K_SPACE))
    send(app, monkeypatch, key(pygame.K_u))
    assert take.done.is_set() and not app._recording_series.visible
    app._playing_screen._start_practice(0, force_count_in=True)
    assert app._playing_screen._test_recording.take is take
    assert not app._playing_screen._test_recording._attached


@pytest.mark.parametrize('failure', ['expired', 'device', 'storage', 'late_writer'])
def test_pause_cannot_reset_single_limit_or_hide_failure(app, monkeypatch, failure):
    start_single(app)
    take = audio(app)
    deadline = app._recording_series.deadline
    app._handle_playing_event(key(pygame.K_SPACE))
    assert app._recording_series.deadline == deadline
    if failure == 'expired':
        import time
        assert 0 < deadline - time.monotonic() <= MAX_SINGLE_SECONDS
        monkeypatch.setattr('pickhero.ui.recording_series.time.monotonic', lambda: deadline + 1)
    elif failure == 'device':
        monkeypatch.setattr(AudioCapture, 'start', Mock(side_effect=RuntimeError('unplugged')))
    elif failure == 'storage':
        monkeypatch.setattr('pickhero.ui.test_recording.TestRecording', Mock(side_effect=OSError('disk full')))
    else:
        take.error = 'delayed writer error'
    app._handle_playing_event(key(pygame.K_SPACE))
    assert not app._playing_screen._test_recording._attached
    assert app._recording_series.label is None and app._recording_series.notice
    assert len(list((config_module.CONFIG_DIR / 'recordings').iterdir())) == 1


@pytest.mark.parametrize('look', ['studio', 'stage', 'hifi'])
@pytest.mark.parametrize('language', ['de', 'en'])
@pytest.mark.parametrize('size', [(320, 240), (1280, 720)])
def test_paused_single_has_visible_status_in_each_design(app, look, language, size):
    i18n.set_language(language)
    set_look(look)
    start_single(app)
    audio(app)
    app._handle_playing_event(key(pygame.K_SPACE))
    surface = pygame.Surface(size)
    app._render(surface)
    band = pygame.Rect(0, size[1] - BANNER_HEIGHT, size[0], BANNER_HEIGHT)
    assert band.contains(app._recording_series.stop_button)
    assert app._recording_series.single_piece and app._recording_series.label == 'acoustic'
    assert app._playing_screen._test_recording.badge_rect is not None


def test_version_button_names_destination_and_subtitle_names_current_version(app):
    app._open_lessons()
    music = app._menu
    song = music._sync_catalogue()
    assert music.catalogue.metadata(song, music._instrument())['version'] == 'start'
    music.render(pygame.Surface((1280, 720)))
    assert music._button_rects['version.start'] and music._button_rects['version.groove']
    music.handle_event(key(pygame.K_v))
    song = music._sync_catalogue()
    assert music.catalogue.metadata(song, music._instrument())['version'] == 'groove'
