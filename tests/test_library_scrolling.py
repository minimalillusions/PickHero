"""Long song lists navigated through real events and each look's renderer."""

import pygame
import pytest

from pickhero import library
from pickhero.config import Config
from pickhero.progress import ProgressTracker, song_key
from pickhero.ui.colors import get_look, set_look
from pickhero.ui.menu import MenuScreen
from pickhero.ui.page import Page


LOOKS = ("studio", "stage", "hifi")


@pytest.fixture
def browser(tmp_path, monkeypatch):
    previous = get_look().key
    pygame.display.init()
    pygame.font.init()
    songs = [library.SongInfo(str(tmp_path / f"piece-{index:02}.gp5"),
                              title=f"Piece {index:02}", strings=4,
                              difficulty=1 + index % 5, bars=8, bpm=80)
             for index in range(45)]
    monkeypatch.setattr(library, "scan", lambda *args, **kwargs: songs)

    def make(look, *, carry=True, size=(1280, 720)):
        set_look(look)
        tracker = ProgressTracker()
        if carry:
            tracker.record_result(song_key(songs[0].file, "bass_ukulele", 0),
                                  {"accuracy_percent": 60})
        screen = MenuScreen(tmp_path, Config(instrument="bass_ukulele"), tracker)
        surface = pygame.Surface(size)
        screen.render(surface)
        pointer = _row(screen, 0).center
        monkeypatch.setattr(pygame.mouse, "get_pos", lambda: pointer)
        return screen, surface

    yield make
    set_look(previous)


def _key(screen, key):
    return screen.handle_event(pygame.event.Event(pygame.KEYDOWN, key=key, unicode=""))


def _wheel(screen, y):
    screen.handle_event(pygame.event.Event(pygame.MOUSEWHEEL, y=y, x=0))


def _row(screen, index):
    return next((rect for rect, value in screen._row_rects if value == index), None)


@pytest.mark.parametrize("look", LOOKS)
def test_mouse_wheel_moves_list_and_does_not_snap_back_to_selection(browser, look):
    screen, surface = browser(look)
    initial_rect = _row(screen, 0).copy()
    _wheel(screen, -1)
    screen.render(surface)
    offset = screen._scroll_px
    assert offset > 0
    assert screen._selected == 0
    assert _row(screen, 0) != initial_rect
    for _ in range(3):
        screen.render(surface)
        assert screen._scroll_px == offset
        assert screen._selected == 0


@pytest.mark.parametrize("look", LOOKS)
def test_wheel_reaches_both_ends_without_blank_overscroll(browser, monkeypatch, look):
    screen, surface = browser(look)
    point = screen._row_rects[0][0].center
    monkeypatch.setattr(pygame.mouse, "get_pos", lambda: point)
    _wheel(screen, -1000)
    screen.render(surface)
    assert _row(screen, len(screen._rows) - 1) is not None
    bottom = screen._scroll_px
    assert bottom > 0
    _wheel(screen, -1000)
    screen.render(surface)
    assert screen._scroll_px == bottom
    _wheel(screen, 1000)
    screen.render(surface)
    assert screen._scroll_px == 0
    assert _row(screen, 0) is not None


@pytest.mark.parametrize("look", LOOKS)
@pytest.mark.parametrize("carry", (False, True))
@pytest.mark.parametrize("size", ((1280, 720), (900, 520)))
def test_arrows_keep_every_selected_row_fully_visible(browser, look, carry, size):
    screen, surface = browser(look, carry=carry, size=size)
    full_height = _row(screen, 0).height
    for key in (pygame.K_DOWN, pygame.K_UP):
        for _ in range(len(screen._rows) - 1):
            _key(screen, key)
            screen.render(surface)
            rect = _row(screen, screen._selected)
            assert rect is not None, (look, carry, size, screen._selected)
            assert rect.height == full_height, (look, screen._selected, rect)
            assert surface.get_rect().contains(rect)


@pytest.mark.parametrize("look", LOOKS)
def test_home_end_and_page_keys_reveal_the_target(browser, look):
    screen, surface = browser(look)
    full_height = _row(screen, 0).height
    for key in (pygame.K_END, pygame.K_HOME, pygame.K_PAGEDOWN, pygame.K_PAGEUP):
        _key(screen, key)
        screen.render(surface)
        rect = _row(screen, screen._selected)
        assert rect is not None and rect.height == full_height
        if key == pygame.K_END:
            assert screen._selected == len(screen._rows) - 1
        elif key == pygame.K_HOME:
            assert screen._selected == 0 and screen._scroll_px == 0


@pytest.mark.parametrize("look", LOOKS)
def test_keyboard_reclaims_selection_after_free_wheel_scroll(browser, monkeypatch, look):
    screen, surface = browser(look)
    point = screen._row_rects[0][0].center
    monkeypatch.setattr(pygame.mouse, "get_pos", lambda: point)
    _wheel(screen, -1000)
    screen.render(surface)
    assert _row(screen, 0) is None and screen._selected == 0
    _key(screen, pygame.K_UP)  # Already at index0, but still needs revealing.
    screen.render(surface)
    assert screen._selected == 0
    assert screen._scroll_px == 0
    assert _row(screen, 0) is not None


@pytest.mark.parametrize("look", LOOKS)
def test_click_after_scrolling_selects_the_visible_song(browser, monkeypatch, look):
    screen, surface = browser(look)
    point = screen._row_rects[0][0].center
    monkeypatch.setattr(pygame.mouse, "get_pos", lambda: point)
    _wheel(screen, -12)
    screen.render(surface)
    rect, index = screen._row_rects[len(screen._row_rects) // 2]
    assert index > 0
    screen.handle_event(pygame.event.Event(pygame.MOUSEBUTTONDOWN, button=1, pos=rect.center))
    screen.render(surface)
    assert screen._selected == index
    assert _row(screen, index) is not None
    assert _key(screen, pygame.K_RETURN) == screen._rows[index].file


@pytest.mark.parametrize("look", LOOKS)
def test_resize_preserves_visible_keyboard_selection_and_filter_resets_view(browser, look):
    screen, surface = browser(look)
    _key(screen, pygame.K_END)
    screen.render(surface)
    surface = pygame.Surface((900, 520))
    screen.render(surface)
    assert _row(screen, len(screen._rows) - 1) is not None
    screen._set_filter(("level", 1))
    screen.render(surface)
    assert screen._selected == 0 and screen._scroll_px == 0
    assert len(screen._rows) == 9
    assert _row(screen, 0) is not None


@pytest.mark.parametrize("look", LOOKS)
def test_arrows_reveal_results_while_search_has_focus(browser, look):
    screen, surface = browser(look)
    _key(screen, pygame.K_f)
    for letter in "piece":
        screen.handle_event(pygame.event.Event(pygame.KEYDOWN, key=ord(letter), unicode=letter))
    for _ in range(20):
        _key(screen, pygame.K_DOWN)
    screen.render(surface)
    assert screen.is_searching and screen._selected == 20
    assert _row(screen, 20) is not None


@pytest.mark.parametrize("look", LOOKS)
def test_partly_scrolled_rows_only_register_the_visible_area(browser, monkeypatch, look):
    screen, surface = browser(look)
    original = Page.mark_row
    recorded = []

    def observe(page, rect, index):
        before = len(page.row_hit)
        result = original(page, rect, index)
        for visible, value in page.row_hit[before:]:
            assert surface.get_clip().contains(visible), (look, value, visible, surface.get_clip())
            recorded.append(value)
        return result

    monkeypatch.setattr(Page, "mark_row", observe)
    _wheel(screen, -1)
    screen.render(surface)
    assert recorded


@pytest.mark.parametrize("look", LOOKS)
def test_changing_look_keeps_last_keyboard_selection_accessible(browser, look):
    screen, surface = browser(look)
    _key(screen, pygame.K_END)
    screen.render(surface)
    for target in LOOKS:
        set_look(target)
        screen.render(surface)
        assert _row(screen, screen._selected) is not None
        assert _key(screen, pygame.K_RETURN) == screen._rows[-1].file


def test_stage_setup_can_be_closed_before_scrolling_song_list(browser):
    screen, surface = browser("stage")
    _key(screen, pygame.K_s)
    screen.render(surface)
    assert "setup.card" in screen._button_rects
    _key(screen, pygame.K_ESCAPE)
    screen.render(surface)
    _wheel(screen, -1)
    screen.render(surface)
    assert screen._scroll_px > 0


@pytest.mark.parametrize("look", LOOKS)
def test_open_setup_preserves_list_navigation_outside_its_controls(browser, monkeypatch, look):
    screen, surface = browser(look)
    first = _row(screen, 0)
    # Left end of the list remains outside the right-hand Studio/Hi-Fi card.
    point = (first.left + 2, first.centery)
    monkeypatch.setattr(pygame.mouse, "get_pos", lambda: point)
    _key(screen, pygame.K_s)
    screen.render(surface)
    _wheel(screen, -3)
    screen.render(surface)
    assert screen._scroll_px > 0
    _key(screen, pygame.K_END)
    screen.render(surface)
    assert screen._selected == len(screen._rows) - 1
    # Close the card to inspect the selected row's full click area.
    _key(screen, pygame.K_s)
    screen.render(surface)
    assert _row(screen, screen._selected) is not None


@pytest.mark.parametrize("size", [(1280, 720), (900, 600), (640, 480), (360, 280)])
def test_settings_keep_their_position_when_the_user_changes_look(browser, size):
    screen, surface = browser("studio", size=size)
    anchor = screen._button_rects["setup"][0].copy()
    assert anchor.centerx > size[0] / 2 and anchor.top < 30
    screen._handle_click(anchor.center)
    expected = None
    for look in LOOKS:
        set_look(look)
        screen.render(surface)
        assert screen._button_rects["setup"][0] == anchor
        geometry = {name: tuple(screen._button_rects[name][0]) for name in
                    ("setup.card", "instrument", "device")
                    if name in screen._button_rects}
        if expected is None:
            expected = geometry
        assert geometry == expected
        assert surface.get_rect().contains(screen._button_rects["setup.card"][0])
    screen._handle_click(anchor.center)
    screen.render(surface)
    assert not screen._setup_open and "setup.card" not in screen._button_rects


@pytest.mark.parametrize("look", LOOKS)
def test_short_settings_panel_reaches_last_controls_without_scrolling_songs(browser, look):
    screen, surface = browser(look, size=(640, 480))
    screen._handle_click(screen._button_rects["setup"][0].center)
    screen.render(surface)
    before = screen._scroll_px
    card = screen._button_rects["setup.card"][0]
    screen.handle_event(pygame.event.Event(pygame.MOUSEMOTION, pos=card.center))
    screen.handle_event(pygame.event.Event(pygame.MOUSEWHEEL, y=-20, x=0))
    screen.render(surface)
    assert screen._scroll_px == before
    assert "folder" in screen._button_rects and "sort" in screen._button_rects
    assert screen._handle_click(screen._button_rects["timing"][0].center) == "timing"
    screen.handle_event(pygame.event.Event(pygame.MOUSEWHEEL, y=20, x=0))
    screen.render(surface)
    assert "instrument" in screen._button_rects
    assert screen._handle_click(screen._button_rects["device"][0].center) == "device"


def test_changing_theme_from_the_panel_keeps_the_control_under_the_pointer(browser):
    screen, surface = browser("studio")
    screen._handle_click(screen._button_rects["setup"][0].center)
    screen.render(surface)
    spot = screen._button_rects["theme"][0].center
    visited = set()
    for _ in LOOKS:
        screen._handle_click(spot)
        screen.render(surface)
        visited.add(get_look().key)
        assert screen._setup_open
        assert screen._button_rects["theme"][0].collidepoint(spot)
    assert visited == set(LOOKS)
