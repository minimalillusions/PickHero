"""Explicit session consent survives pauses and songs, never app restarts."""
import json
from types import SimpleNamespace
from unittest.mock import Mock
import wave

import numpy as np
import pygame
import pytest

from pickhero import config as config_module, i18n
from pickhero.audio.input import AudioCapture
from pickhero.config import Config
from pickhero.ui.app import App
from pickhero.ui.colors import set_look
from pickhero.ui.recording_series import BANNER_HEIGHT
from tests.test_recording_navigation import app, key  # noqa: F401


@pytest.fixture(autouse=True)
def fake_input(monkeypatch):
    def start(capture):
        capture._start_time = 100.
        capture._stream = SimpleNamespace(active=True, stop=Mock(), close=Mock())
    monkeypatch.setattr(AudioCapture, 'start', start)
    monkeypatch.setattr('pickhero.audio.input.time.perf_counter', lambda: 100.)


def start_series(app, label='acoustic'):
    app._handle_menu_event(key(pygame.K_u))
    app._handle_playing_event(key(pygame.K_t))
    panel = app._playing_screen._panel
    assert panel.actions[0].name == 'record:series:acoustic'
    assert not (config_module.CONFIG_DIR/'recordings').exists()
    assert app._playing_screen._test_recording.take is None
    app._playing_screen._panel_action('record:series:'+label)
    assert app._recording_series.label == label
    assert app._playing_screen._test_recording._attached


def audio(app, values=(.001, -.002, .003)):
    rec = app._playing_screen._test_recording
    rec.take.audio(np.asarray(values, np.float32), 20.)
    return rec.take


def send(app, monkeypatch, event):
    monkeypatch.setattr(pygame.event, 'get', lambda: [event])
    app._process_events(pygame.Surface((1280, 720)))


@pytest.mark.parametrize('label', ['acoustic', 'amplified', 'direct'])
def test_pause_in_count_in_then_resume_and_change_piece_keep_recording(app, label):
    start_series(app, label)
    first = audio(app)
    series_id = app._recording_series.identifier
    app._handle_playing_event(key(pygame.K_SPACE))
    assert first.done.is_set() and app._recording_series.label == label
    assert app._recording_series.recorder is None
    report = json.loads((first.folder/'session.json').read_text())
    assert report['finish']['reason'] == 'pause'
    assert report['finish']['ended_during_count_in']
    assert app._playing_screen._test_recording._count_in_only
    app._handle_playing_event(key(pygame.K_SPACE))
    second = audio(app)
    assert second is not first
    app._playing_screen._playback_ms = 1000.
    app._handle_playing_event(key(pygame.K_ESCAPE))
    assert app._state == 'menu' and app._recording_series.recorder is None
    assert json.loads((second.folder/'session.json').read_text())['finish']['reason'] == 'leave_song'
    assert not json.loads((second.folder/'session.json').read_text())['finish']['ended_during_count_in']
    for _ in range(2):
        app._handle_menu_event(key(pygame.K_DOWN))
    app._handle_menu_event(key(pygame.K_RETURN))
    assert app._playing_screen._test_recording.take is None
    app._handle_playing_event(key(pygame.K_SPACE))
    third = audio(app)
    app._handle_playing_event(key(pygame.K_SPACE))
    for number, take in enumerate((first, second, third), 1):
        assert take.metadata['label'] == label and take.metadata['series_id'] == series_id
        assert take.metadata['series_segment'] == number
        with wave.open(str(take.folder/'input.wav')) as wav:
            data = np.frombuffer(wav.readframes(wav.getnframes()), '<i4') / 2**31
        assert data == pytest.approx([.001, -.002, .003], abs=1e-9)
    assert third.metadata['title'] != first.metadata['title']
    assert len(list((config_module.CONFIG_DIR/'recordings').iterdir())) == 3


def test_all_nine_exercises_have_separate_files_after_one_explicit_session_start(app):
    start_series(app)
    takes = []
    for index in range(9):
        if index:
            app._handle_menu_event(key(pygame.K_DOWN))
            app._handle_menu_event(key(pygame.K_RETURN))
            app._handle_playing_event(key(pygame.K_SPACE))
        takes.append(audio(app))
        app._playing_screen._playback_ms = 1000.
        app._handle_playing_event(key(pygame.K_ESCAPE))
    assert len({take.metadata['title'] for take in takes}) == 9
    assert len({take.metadata['series_id'] for take in takes}) == 1
    assert all(take.done.is_set() for take in takes)
    assert app._recording_series.files == 9


@pytest.mark.parametrize('stop', ['key', 'mouse'])
def test_explicit_stop_ends_recording_without_stopping_playback(app, monkeypatch, stop):
    start_series(app)
    take = audio(app)
    surface = pygame.Surface((1280, 720))
    app._render(surface)
    event = key(pygame.K_u) if stop == 'key' else pygame.event.Event(
        pygame.MOUSEBUTTONDOWN, button=1, pos=app._recording_series.stop_button.center)
    send(app, monkeypatch, event)
    assert take.done.is_set() and app._playing_screen._playing
    assert not app._recording_series.visible
    assert app._playing_screen._audio_capture.test_recording is None
    app._handle_playing_event(key(pygame.K_SPACE))
    app._handle_playing_event(key(pygame.K_SPACE))
    assert app._playing_screen._test_recording.take is take
    assert not app._playing_screen._test_recording._attached


def test_quit_and_new_app_never_restore_recording_consent(app):
    start_series(app)
    take = audio(app)
    app._close_screens()
    assert take.done.is_set() and not app._recording_series.visible
    assert not App(Config())._recording_series.visible


def test_direct_playing_shortcut_also_ends_series(app):
    start_series(app)
    take = audio(app)
    app._handle_playing_event(key(pygame.K_u))
    assert take.done.is_set() and app._playing_screen._playing
    assert not app._recording_series.visible


def test_tempo_change_keeps_one_file_and_logs_new_clock_mapping(app):
    start_series(app)
    take = audio(app)
    app._playing_screen.set_tempo_factor(.5)
    assert app._playing_screen._test_recording.take is take
    assert app._playing_screen._test_recording._attached
    app._handle_playing_event(key(pygame.K_SPACE))
    events = [json.loads(line) for line in (take.folder/'events.jsonl').read_text().splitlines()]
    assert any(event['kind'] == 'clock_mapping' and event['tempo_factor'] == .5 for event in events)


def test_search_u_is_text_and_banner_can_still_stop_session(app, monkeypatch):
    start_series(app)
    audio(app)
    app._handle_playing_event(key(pygame.K_ESCAPE))
    app._handle_menu_event(key(pygame.K_f))
    send(app, monkeypatch, key(pygame.K_u, 'u'))
    assert app._recording_series.label == 'acoustic' and app._menu._search_text == 'u'
    app._render(pygame.Surface((640, 480)))
    send(app, monkeypatch, pygame.event.Event(pygame.MOUSEBUTTONDOWN, button=1,
                                             pos=app._recording_series.stop_button.center))
    assert not app._recording_series.visible


@pytest.mark.parametrize('failure', ['file_limit', 'queue_full', 'writer', 'input_off', 'expired'])
def test_limits_and_failures_end_series_visibly_and_never_rearm(app, monkeypatch, failure):
    start_series(app)
    take = audio(app)
    rec = app._playing_screen._test_recording
    if failure in ('file_limit', 'queue_full', 'writer'):
        take.problem = 'duration_limit' if failure == 'file_limit' else 'queue_full'
        if failure == 'writer':
            take.error = 'disk full'
        take.accepting = False
    elif failure == 'input_off':
        app._playing_screen._audio_enabled = False
    else:
        monkeypatch.setattr('pickhero.ui.recording_series.time.monotonic',
                            lambda: app._recording_series.deadline + 1)
    rec.observe()
    assert take.done.is_set() and not rec._attached
    assert app._recording_series.label is None and app._recording_series.notice
    assert app._recording_series.visible
    rec.started()
    assert not rec._attached and rec.take is take


@pytest.mark.parametrize('failure', ['device', 'storage'])
def test_new_segment_cannot_silently_fail(app, monkeypatch, failure):
    start_series(app)
    audio(app)
    app._handle_playing_event(key(pygame.K_SPACE))
    if failure == 'device':
        monkeypatch.setattr(AudioCapture, 'start', Mock(side_effect=RuntimeError('unplugged')))
    else:
        monkeypatch.setattr('pickhero.ui.test_recording.TestRecording', Mock(side_effect=OSError('disk full')))
    app._handle_playing_event(key(pygame.K_SPACE))
    assert app._recording_series.label is None
    assert app._recording_series.notice == 'error'
    assert not app._playing_screen._test_recording._attached


def test_delayed_disk_error_is_visible_even_after_leaving_piece(app):
    start_series(app)
    take = audio(app)
    app._handle_playing_event(key(pygame.K_ESCAPE))
    assert app._recording_series.label == 'acoustic'
    take.error = 'delayed write failure'
    app._update()
    assert app._recording_series.label is None and app._recording_series.notice == 'error'
    app._handle_menu_event(key(pygame.K_RETURN))
    app._handle_playing_event(key(pygame.K_SPACE))
    assert app._playing_screen._test_recording.take is None


def test_previous_writer_must_finish_before_new_piece_records(app):
    start_series(app)
    take = audio(app)
    app._handle_playing_event(key(pygame.K_ESCAPE))
    # Simulate a writer that is still saving after the bounded stop wait.
    done = take.done
    take.done = SimpleNamespace(is_set=lambda: False)
    try:
        app._handle_menu_event(key(pygame.K_RETURN))
        app._handle_playing_event(key(pygame.K_SPACE))
        assert app._playing_screen._test_recording.take is None
        assert app._recording_series.label is None and app._recording_series.notice == 'error'
        assert not app._recording_series.start('amplified')
    finally:
        take.done = done


@pytest.mark.parametrize('look', ['studio', 'stage', 'hifi'])
@pytest.mark.parametrize('size', [(320, 240), (640, 480), (1280, 720)])
@pytest.mark.parametrize('language', ['de', 'en'])
def test_series_chooser_remains_explicit_and_readable(app, look, size, language):
    i18n.set_language(language)
    set_look(look)
    app._handle_menu_event(key(pygame.K_u))
    app._handle_playing_event(key(pygame.K_t))
    surface = pygame.Surface(size)
    app._render(surface)
    panel = app._playing_screen._panel
    controls = [panel.hit[action.name] for action in panel.actions]
    assert all(surface.get_rect().contains(rect) for rect in controls)
    assert all(not rect.colliderect(panel.body) for rect in controls)
    assert panel.body.height >= 20
    assert not app._recording_series.visible
    assert not (config_module.CONFIG_DIR/'recordings').exists()
    app._handle_playing_event(key(pygame.K_ESCAPE))
    app._handle_playing_event(key(pygame.K_SPACE))
    assert app._playing_screen._test_recording.take is None


@pytest.mark.parametrize('look', ['studio', 'stage', 'hifi'])
@pytest.mark.parametrize('size', [(640, 480), (1280, 720)])
@pytest.mark.parametrize('language', ['de', 'en'])
def test_series_badge_has_its_own_space_in_playback_and_library(app, look, size, language):
    i18n.set_language(language)
    set_look(look)
    start_series(app)
    audio(app)
    surface = pygame.Surface(size)
    app._render(surface)
    band = pygame.Rect(0, size[1]-BANNER_HEIGHT, size[0], BANNER_HEIGHT)
    assert band.contains(app._recording_series.stop_button)
    controls = [rect for rects in app._playing_screen._button_rects.values() for rect in rects]
    assert all(not band.colliderect(rect) for rect in controls)
    app._handle_playing_event(key(pygame.K_ESCAPE))
    app._render(surface)
    controls = [rect for value in app._menu._button_rects.values()
                for rect in ([value] if isinstance(value, pygame.Rect) else value)]
    assert all(not band.colliderect(rect) for rect in controls)
    assert app._recording_series.label == 'acoustic' and app._recording_series.recorder is None
