"""Recording is reachable from selection without recording before consent."""
from types import SimpleNamespace
from unittest.mock import Mock

import pygame
import pytest

from pickhero import config as config_module, i18n
from pickhero.config import Config
from pickhero.exercises import ensure_exercises
from pickhero.ui import app as app_module
from pickhero.ui.app import App
from pickhero.ui.colors import set_look
from pickhero.ui.practice_panels import PracticePanel
from pickhero.ui.test_recording import RecordingRequest


def key(code, text=''):
    return pygame.event.Event(pygame.KEYDOWN, key=code, unicode=text, mod=0)


def click(rect):
    return pygame.event.Event(pygame.MOUSEBUTTONDOWN, button=1, pos=rect.center)


@pytest.fixture
def app(tmp_path):
    pygame.font.init()
    i18n.set_language('de')
    application = App(Config(instrument='bass_ukulele', songs_dir=str(tmp_path)))
    ensure_exercises(application._config.get_instrument(), tmp_path, application._progress)
    application._build_menu()
    application._menu.select_category('technique')
    yield application
    application._close_screens()
    i18n.set_language('de')
    set_look('studio')


def assert_chooser(app):
    assert app._state == 'playing'
    screen = app._playing_screen
    assert screen._panel.kind == 'test_recording'
    assert not screen._playing and screen._audio_capture is None
    assert screen._test_recording.pending is None and screen._test_recording.take is None
    assert not (config_module.CONFIG_DIR/'recordings').exists()


@pytest.mark.parametrize('look', ['studio', 'stage', 'hifi'])
@pytest.mark.parametrize('source', ['keyboard', 'mouse'])
def test_library_opens_chooser_for_selected_song(app, look, source):
    set_look(look)
    app._menu.handle_event(key(pygame.K_DOWN))
    path = app._menu._selected_path()
    surface = pygame.Surface((1280, 720))
    app._menu.render(surface)
    rect = app._menu._button_rects['record'][0]
    assert surface.get_rect().contains(rect)
    event = key(pygame.K_u) if source == 'keyboard' else click(rect)
    app._handle_menu_event(event)
    assert_chooser(app)
    assert app._playing_screen._song_path == str(path)


def test_search_and_resume_dialog_keep_their_keyboard_scope(app):
    app._handle_menu_event(key(pygame.K_f))
    app._handle_menu_event(key(pygame.K_u, 'u'))
    assert app._state == 'menu' and app._menu._search_text == 'u'
    app._handle_menu_event(key(pygame.K_ESCAPE))
    app._menu._resume_panel = PracticePanel('resume', 'Resume', '', [], [])
    app._handle_menu_event(key(pygame.K_u))
    assert app._state == 'menu' and app._playing_screen is None


def test_empty_selection_offers_music_instead_of_ignoring_u(app):
    app._menu._rows = []
    app._handle_menu_event(key(pygame.K_u))
    assert app._state == 'menu' and app._menu.catalogue.category == 'music'
    assert not (config_module.CONFIG_DIR/'recordings').exists()


@pytest.mark.parametrize('look', ['studio', 'stage', 'hifi'])
@pytest.mark.parametrize('size', [(320, 240), (640, 480), (1280, 720)])
@pytest.mark.parametrize('language', ['de', 'en'])
def test_music_button_and_key_open_same_piece_and_version(app, monkeypatch, look, size, language):
    i18n.set_language(language)
    set_look(look)
    app._open_lessons()
    music = app._menu
    music._move(2)
    music.handle_event(key(pygame.K_v))
    surface = pygame.Surface(size)
    music.render(surface)
    rect = music._button_rects['record'][0]
    assert surface.get_rect().contains(rect)
    assert all(not rect.colliderect(row) for row, _ in music._row_rects)
    keyboard = music.handle_event(key(pygame.K_u))
    mouse = music.handle_event(click(rect))
    assert keyboard == mouse == 'record'
    practice = music.practice_for(music._selected_path())
    assert practice is not None
    monkeypatch.setattr(pygame.event, 'get', lambda: [key(pygame.K_u)])
    app._process_events(surface)
    assert_chooser(app)
    assert app._lesson_return == practice


@pytest.mark.parametrize('mode', ['catalog', 'detail', 'steps'])
def test_lesson_recording_preserves_selected_lesson_and_step(app, monkeypatch, mode):
    app._open_lessons(music_first=False)
    lesson = app._lesson_screen
    lesson.handle_event(key(pygame.K_DOWN))
    if mode != 'catalog':
        lesson.handle_event(key(pygame.K_o))
    if mode == 'steps':
        lesson.handle_event(key(pygame.K_s))
        lesson.handle_event(key(pygame.K_RIGHT))
    expected = lesson.handle_event(key(pygame.K_u))
    assert expected.path == lesson.plan.lessons[lesson._selected].path
    assert (expected.practice is not None) == (mode == 'steps')
    monkeypatch.setattr(pygame.event, 'get', lambda: [key(pygame.K_u)])
    app._process_events(pygame.Surface((640, 480)))
    assert_chooser(app)
    assert app._lesson_return == expected.practice


@pytest.fixture
def imports(app, monkeypatch):
    path = app._menu._selected_path()
    timeline = app_module.load_gp_file(path, instrument=app._config.get_instrument())
    jobs = []
    def job(operation, path, instrument, **options):
        result = {'content_id': 'a'*64, 'selected': 0,
                  'tracks': [{'index': 0, 'name': 'Bass', 'strings': 4, 'compatible': True}]}
        value = SimpleNamespace(operation=operation, poll=lambda: True, status='', error=None,
                                result=result, prepared_song=(timeline, None), cancel=Mock())
        jobs.append(value)
        return value
    monkeypatch.setattr(app_module, 'ImportJob', job)
    app._background = True
    return jobs


def finish_import(app):
    app._poll_import()
    app._import_screen.render(pygame.Surface((1280, 720)))
    app._handle_import_event(key(pygame.K_RETURN))
    app._poll_import()


def test_background_track_import_keeps_recording_request(app, imports):
    app._handle_menu_event(key(pygame.K_u))
    assert app._state == 'import' and app._playing_screen is None
    finish_import(app)
    assert [job.operation for job in imports] == ['inspect', 'load']
    assert_chooser(app)


def test_cancel_import_then_normal_play_never_opens_recording(app, imports):
    app._handle_menu_event(key(pygame.K_u))
    app._handle_import_event(key(pygame.K_ESCAPE))
    assert app._state == 'menu' and not app._recording_requested
    imports[0].cancel.assert_called_once()
    app._handle_menu_event(key(pygame.K_RETURN))
    finish_import(app)
    assert app._state == 'playing' and app._playing_screen._panel is None
    assert app._playing_screen._test_recording.take is None


@pytest.mark.parametrize('retry', [True, False])
def test_import_failure_only_keeps_request_for_explicit_retry(app, imports, retry):
    app._handle_menu_event(key(pygame.K_u))
    imports[0].error = 'Unreadable file'
    app._poll_import()
    assert app._load_error is not None
    app._handle_load_error(key(pygame.K_r if retry else pygame.K_ESCAPE))
    if not retry:
        assert not app._recording_requested
        app._handle_menu_event(key(pygame.K_RETURN))
    finish_import(app)
    if retry:
        assert_chooser(app)
    else:
        assert app._playing_screen._panel is None


def test_chooser_escape_and_next_normal_song_do_not_rearm(app):
    app._handle_menu_event(key(pygame.K_u))
    assert_chooser(app)
    app._handle_playing_event(key(pygame.K_ESCAPE))
    assert app._playing_screen._panel is None
    app._handle_playing_event(key(pygame.K_ESCAPE))
    assert app._state == 'menu'
    app._handle_menu_event(key(pygame.K_RETURN))
    assert app._state == 'playing' and app._playing_screen._panel is None
    assert app._playing_screen._test_recording.take is None


def test_recording_chooser_closes_a_running_passage_preview(app):
    app._handle_menu_event(key(pygame.K_RETURN))
    preview = SimpleNamespace(close=Mock())
    app._playing_screen._panel = preview
    app._handle_playing_event(key(pygame.K_u))
    preview.close.assert_called_once()
    assert_chooser(app)
