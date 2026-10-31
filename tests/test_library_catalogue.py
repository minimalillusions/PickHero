"""The actual library: grouping, versions, playback lifecycle and responsive clicks."""
from unittest.mock import Mock

import pygame
import pytest

from pickhero.config import Config
from pickhero.exercises import write_exercises
from pickhero.i18n import current_language, set_language
from pickhero.instruments import BASS_UKULELE
from pickhero.music import MUSIC_DIRNAME, PIECES, ensure_music, music_for_path
from pickhero.progress import ProgressTracker, song_key
from pickhero.ui import library_catalogue
from pickhero.ui.app import App
from pickhero.ui.colors import get_look, set_look
from pickhero.ui.menu import MenuScreen


@pytest.fixture
def browser(tmp_path):
    pygame.font.init()
    old_look, old_language = get_look().key, current_language()
    write_exercises(BASS_UKULELE, tmp_path/'Uebungen')
    ensure_music(BASS_UKULELE, tmp_path)
    # Imported filenames may resemble generated pieces. They must stay separate.
    own = tmp_path/'Band'/MUSIC_DIRNAME/PIECES[0].filename('start')
    own.parent.mkdir(parents=True)
    own.write_bytes((tmp_path/MUSIC_DIRNAME/PIECES[0].filename('start')).read_bytes())
    menu = MenuScreen(tmp_path, Config(instrument='bass_ukulele'), ProgressTracker())
    yield menu
    menu.close()
    set_look(old_look)
    set_language(old_language)


def key(menu, value):
    return menu.handle_event(pygame.event.Event(pygame.KEYDOWN, key=value, unicode='', mod=0))


def click(menu, surface, name):
    menu.render(surface)
    return menu.handle_event(pygame.event.Event(pygame.MOUSEBUTTONDOWN, button=1,
                                               pos=menu._button_rects[name][0].center))


def test_categories_deduplicate_only_managed_versions_and_keep_selection(browser):
    menu = browser
    assert menu.catalogue.category == 'music' and len(menu._rows) == len(PIECES)
    menu._move(2)
    selected = menu._selected_path()
    key(menu, pygame.K_2)
    assert len(menu._rows) == 10
    assert all(menu.catalogue.kind(row) == 'technique' for row in menu._rows)
    key(menu, pygame.K_3)
    assert len(menu._rows) == 1 and 'Band' in menu._selected_path().parts
    key(menu, pygame.K_1)
    assert menu._selected_path() == selected


def test_versions_keep_independent_history_and_start_the_visible_file(browser):
    menu = browser
    first = menu._rows[0]
    menu._progress.record_result(song_key(first.file, 'bass_ukulele', 0, content_id=first.content_id),
                                 {'accuracy_percent': 93})
    key(menu, pygame.K_v)
    second = menu._rows[menu._selected]
    assert music_for_path(first.file)[0] == music_for_path(second.file)[0]
    assert music_for_path(second.file)[1] == 'groove'
    assert menu._accuracy(second) is None
    assert key(menu, pygame.K_RETURN) == second.file
    key(menu, pygame.K_v)
    assert menu._accuracy(menu._rows[menu._selected]) == 93


@pytest.mark.parametrize('look', ['studio', 'stage', 'hifi'])
@pytest.mark.parametrize('size', [(320, 240), (640, 480), (876, 706), (1280, 720)])
@pytest.mark.parametrize('language', ['de', 'en'])
def test_real_navigation_and_detail_actions_fit_every_layout(browser, look, size, language):
    menu = browser
    set_look(look)
    set_language(language)
    surface = pygame.Surface(size)
    for category in ('music', 'technique', 'files'):
        menu.select_category(category)
        menu.render(surface)
        assert menu._row_rects
        assert all(surface.get_rect().contains(rect) for rect, _ in menu._row_rects)
        if size[0] < 940:
            click(menu, surface, 'details')
        menu.render(surface)
        assert 'play' in menu._button_rects
        assert all(surface.get_rect().contains(rect) for rects in menu._button_rects.values() for rect in rects)
        # Every simultaneously visible click target must describe one action.
        actions = [(name, rect) for name, rects in menu._button_rects.items() for rect in rects
                   if rect.width and rect.height]
        for i, (name, rect) in enumerate(actions):
            assert all(not rect.colliderect(other) for other_name, other in actions[i+1:]
                       if other_name != name), (size, category, name, actions)
        if category != 'files':
            assert 'preview' in menu._button_rects
        assert click(menu, surface, 'play') == menu._selected_path()
        if size[0] < 940:
            key(menu, pygame.K_ESCAPE)
            assert not menu.catalogue.detail_open


def test_preview_is_explicit_unscored_and_stops_on_selection_and_departure(browser, monkeypatch):
    factory = Mock()
    monkeypatch.setattr(library_catalogue, 'MidiPlayer', factory)
    before = browser._progress.revision
    surface = pygame.Surface((1280, 720))
    browser.render(surface)
    factory.assert_not_called()
    key(browser, pygame.K_SPACE)
    player = factory.return_value
    player.open.assert_called_once()
    key(browser, pygame.K_DOWN)
    player.close.assert_called_once()
    key(browser, pygame.K_SPACE)
    key(browser, pygame.K_RETURN)
    assert player.close.call_count == 2
    assert browser._progress.revision == before
    assert not browser.catalogue._player
    assert browser.catalogue.status == 'idle'


def test_scroll_is_owned_by_pointed_pane_and_settings_consume_outside_click(browser):
    surface = pygame.Surface((1280, 720))
    browser.select_category('technique')
    browser.render(surface)
    row = browser._row_rects[0][0].center
    browser.handle_event(pygame.event.Event(pygame.MOUSEMOTION, pos=row))
    browser.handle_event(pygame.event.Event(pygame.MOUSEWHEEL, y=-2, x=0))
    browser.render(surface)
    scroll = browser._scroll_px
    assert scroll > 0
    browser.catalogue.expanded = True
    browser.render(surface)
    browser.handle_event(pygame.event.Event(pygame.MOUSEMOTION, pos=browser.catalogue.detail_rect.center))
    browser.handle_event(pygame.event.Event(pygame.MOUSEWHEEL, y=-100, x=0))
    browser.render(surface)
    assert browser._scroll_px == scroll
    click(browser, surface, 'setup')
    selected = browser._selected_path()
    assert click(browser, surface, 'category.music') is None
    assert not browser._setup_open and browser._selected_path() == selected
    assert browser.catalogue.category == 'technique'


def test_filters_offer_real_level_choices_and_dismiss_without_starting(browser):
    surface = pygame.Surface((1280, 720))
    browser.select_category('technique')
    click(browser, surface, 'filters')
    click(browser, surface, 'filter.level.1')
    assert not browser.catalogue.filters_open
    assert browser._rows and all(row.difficulty == 1 for row in browser._rows)
    click(browser, surface, 'filters')
    assert click(browser, surface, 'play') is None
    assert not browser.catalogue.filters_open


def test_music_play_uses_practice_settings_and_returns_to_unified_library(browser, monkeypatch):
    app = App(browser._config)
    app._songs_dir = browser._songs_dir
    app._menu = browser
    load = Mock()
    monkeypatch.setattr(app, '_load_song', load)
    app._handle_menu_event(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_RETURN, unicode='', mod=0))
    request = load.call_args.kwargs['practice']
    assert request.path == browser._selected_path()
    app._open_lessons(practice=request)
    assert app._state == 'menu' and browser.catalogue.category == 'music'
    assert browser._selected_path() == request.path


def test_focus_saved_version_clears_filter_without_changing_files(browser):
    path = browser._songs_dir/MUSIC_DIRNAME/PIECES[2].filename('groove')
    before = {row.file: row.file.read_bytes() for row in browser._songs}
    browser.select_category('files')
    browser._search_text = 'nothing matches'
    browser._apply_filter()
    assert browser.focus_path(path)
    assert browser._selected_path() == path
    assert before == {file: file.read_bytes() for file in before}


def test_many_own_files_keep_last_row_clickable_and_return_to_scroll_position(browser):
    from pickhero.library import SongInfo
    menu = browser
    menu._songs += [SongInfo(str(menu._songs_dir/f'user-{i:03}.gp5'), title=f'User {i:03}', strings=4)
                    for i in range(100)]
    menu.select_category('files')
    surface = pygame.Surface((640, 480))
    key(menu, pygame.K_END)
    menu.render(surface)
    selected, scroll = menu._selected_path(), menu._scroll_px
    assert any(i == len(menu._rows)-1 for _, i in menu._row_rects)
    key(menu, pygame.K_1)
    key(menu, pygame.K_3)
    menu.render(surface)
    assert menu._selected_path() == selected and menu._scroll_px == scroll


def test_changing_from_finished_version_reveals_unplayed_version(browser):
    first = browser._rows[0]
    browser._progress.record_result(song_key(first.file, 'bass_ukulele', 0, content_id=first.content_id),
                                    {'accuracy_percent': 93})
    browser._set_filter(('progress', 'done'))
    assert len(browser._rows) == 1
    key(browser, pygame.K_v)
    assert music_for_path(browser._selected_path()) == (music_for_path(first.file)[0], 'groove')
    assert browser._accuracy(browser._rows[browser._selected]) is None


def test_background_scan_reveals_requested_version_when_ready(browser, monkeypatch):
    from dataclasses import asdict

    from pickhero.import_jobs import ImportJob
    job = Mock(spec=ImportJob)
    job.poll.return_value = False
    job.status = {}
    job.error = None
    job.result = {'songs': [asdict(song) for song in browser._scanned_songs]}
    monkeypatch.setattr('pickhero.import_jobs.ImportJob', lambda *a, **kw: job)
    menu = MenuScreen(browser._songs_dir, browser._config, browser._progress, background=True)
    path = browser._songs_dir/MUSIC_DIRNAME/PIECES[2].filename('groove')
    assert not menu.focus_path(path)
    job.poll.return_value = True
    menu.update()
    assert menu._selected_path() == path
    assert menu.catalogue.category == 'music'


def test_preview_without_midi_output_does_not_claim_to_play(browser, monkeypatch):
    player = Mock()
    player.open.return_value = False
    monkeypatch.setattr(library_catalogue, 'MidiPlayer', lambda track: player)
    key(browser, pygame.K_SPACE)
    assert browser.catalogue.status == 'unavailable'
    assert browser.catalogue._player is None
    player.close.assert_called_once()
    assert not browser._progress._data
