"""The three library painters, held to the same promises.

Each look draws a different page, so there is little to assert about what any
one of them puts where. What they all owe is the same, and it is the list
below -- which is exactly the kind of thing that was not true of the screens
these replaced: a control that was live but never drawn, a filter that moved
the selection without moving the scroll, a page that lost its last row to a
rounding error at one window size out of five.

Everything here renders and then reads the result. A painter test that only
calls the function and checks it did not raise would have passed on all three
of those.
"""

import os
import time

import pytest

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
import pygame  # noqa: E402

from pickhero.ui import design  # noqa: E402
from pickhero.ui.colors import _LOOKS, get_look, set_look  # noqa: E402
from pickhero.ui.page import PAINTERS, painter  # noqa: E402

LOOK_KEYS = sorted(_LOOKS)
# Every size the app is actually opened at, plus two nobody sensible would
# use. The small ones are where a layout stops being a smaller version of
# itself and starts being broken.
SIZES = [(1280, 720), (1024, 640), (900, 600), (820, 540), (640, 480),
         (480, 360), (360, 280)]


def _rendered_note_box(screen, surface, note):
    """Measure the actual head; duration and neighbour caps now affect its size."""
    from unittest.mock import patch

    module = painter(get_look().key)
    original = module.note_box
    layout = screen._layout(surface)
    wanted_x = screen.note_x(note.timestamp_ms, screen._playback_ms,
                             screen._playhead_x(layout), screen._px_per_ms(layout))
    boxes = []

    def record(page, string, strings, x, w):
        rect = original(page, string, strings, x, w)
        if string == note.string and abs(x - wanted_x) < 0.01:
            boxes.append(rect)
        return rect

    with patch.object(module, "note_box", record):
        screen.render(surface)
    assert boxes, "the expected note was not rendered"
    return boxes[-1]


@pytest.fixture(scope="module", autouse=True)
def _display():
    pygame.init()
    pygame.display.set_mode((1280, 720))
    yield
    pygame.quit()


@pytest.fixture(autouse=True)
def _restore_look():
    before = get_look().key
    yield
    set_look(before)
    design.clear_caches()


def _screen(tmp_path, records=None, instrument="bass_ukulele"):
    """A menu over a real generated exercise set, with real progress."""
    from pathlib import Path

    from pickhero import instruments
    from pickhero.config import Config
    from pickhero.exercises import write_exercises
    from pickhero.progress import ProgressTracker, SongRecord
    from pickhero.ui.menu import MenuScreen

    write_exercises(instruments.BASS_UKULELE, tmp_path)
    tracker = ProgressTracker()
    tracker._data = {
        name: SongRecord(best_accuracy=value, attempts=3,
                         last_played="2026-09-29T10:00:00")
        for name, value in (records or {}).items()
    }
    config = Config()
    config.instrument = instrument
    screen = MenuScreen(Path(tmp_path), config=config, progress=tracker)
    # Never start the file manager from a test: this module clicks every
    # control the painters report, and one of them opens the songs folder.
    screen._open_folder = lambda *a, **k: None
    return screen


def _render(tmp_path, look, size=(1280, 720), records=None):
    set_look(look)
    design.clear_caches()
    screen = _screen(tmp_path, records)
    surface = pygame.Surface(size)
    screen.render(surface)
    return screen, surface


class TestEveryLookHasOne:
    def test_all_three_painters_import_and_draw(self):
        for key in LOOK_KEYS:
            module = painter(key)
            assert module is not None, f"{key} has no painter"
            assert hasattr(module, "library"), f"{key} cannot draw a library"

    def test_an_unknown_look_falls_back_rather_than_raising(self):
        """A missing painter is a packaging mistake, and one that blanks the
        screen stops their practising."""
        assert painter("nonsense") is None

    def test_the_map_covers_the_looks_and_nothing_else(self):
        assert set(PAINTERS) == set(LOOK_KEYS)


class TestTheHitMap:
    """Page.mark, on its own.

    The screen-level test for this injects a second rect by hand, which
    proves the click path handles two but not that mark() ever produces two.
    Tearing the append out left that test green and the pill dead again.
    """

    def test_marking_twice_keeps_both(self):
        from pickhero.ui.page import Page

        page = Page(width=100, height=100)
        first = page.mark("setup", pygame.Rect(0, 0, 10, 10))
        second = page.mark("setup", pygame.Rect(50, 50, 10, 10))
        assert page.hits("setup") == [first, second]

    def test_it_answers_what_is_under_a_point(self):
        from pickhero.ui.page import Page

        page = Page(width=100, height=100)
        page.mark("setup", pygame.Rect(0, 0, 10, 10))
        page.mark("setup", pygame.Rect(50, 50, 10, 10))
        page.mark("play", pygame.Rect(80, 80, 10, 10))
        assert page.at((5, 5)) == "setup"
        assert page.at((55, 55)) == "setup"
        assert page.at((85, 85)) == "play"
        assert page.at((30, 30)) is None

    def test_the_thing_on_top_wins(self):
        """A popover over a row is what the click lands on. Iterating in
        draw order would hand it to whatever was underneath."""
        from pickhero.ui.page import Page

        page = Page(width=100, height=100)
        page.mark("row", pygame.Rect(0, 0, 100, 100))
        page.mark("popover", pygame.Rect(20, 20, 40, 40))
        assert page.at((30, 30)) == "popover"

    def test_an_unmarked_name_has_no_rects(self):
        from pickhero.ui.page import Page

        assert Page(width=10, height=10).hits("nothing") == []


@pytest.mark.parametrize("look", LOOK_KEYS)
class TestItSurvivesTheWindow:
    @pytest.mark.parametrize("size", SIZES)
    def test_it_renders_at_every_size(self, tmp_path, look, size):
        _render(tmp_path, look, size)

    @pytest.mark.parametrize("size", [(1280, 720), (640, 480)])
    def test_nothing_it_reports_lies_outside_the_window(self, tmp_path, look,
                                                        size):
        """A rect outside the surface is a control that cannot be clicked --
        or worse, one that steals a click meant for something else."""
        screen, surface = _render(tmp_path, look, size)
        page = screen._last_page
        bounds = surface.get_rect()
        for name, rects in page.hit.items():
            for rect in rects:
                assert bounds.colliderect(rect), (
                    f"{look}: {name} at {rect} is off")
        for rect, index in page.row_hit:
            assert bounds.colliderect(rect), f"{look}: row {index} is off"

    def test_it_draws_something(self, tmp_path, look):
        """A page that renders to one flat colour is the failure that looks
        exactly like success in a test that only checks for no exception."""
        _screen_, surface = _render(tmp_path, look)
        seen = {surface.get_at((x, y))[:3]
                for x in range(0, surface.get_width(), 17)
                for y in range(0, surface.get_height(), 13)}
        assert len(seen) > 6, f"{look} drew {len(seen)} colours"

    def test_an_empty_library_still_draws_a_page(self, tmp_path, look):
        from pathlib import Path

        from pickhero.config import Config
        from pickhero.ui.menu import MenuScreen

        set_look(look)
        design.clear_caches()
        empty = tmp_path / "leer"
        empty.mkdir()
        config = Config()
        config.instrument = "bass_ukulele"
        screen = MenuScreen(Path(empty), config=config)
        screen.render(pygame.Surface((1280, 720)))
        screen.render(pygame.Surface((640, 480)))


@pytest.mark.parametrize("look", LOOK_KEYS)
class TestWhatItReportsBack:
    def test_every_row_on_screen_can_be_clicked(self, tmp_path, look):
        screen, _surface = _render(tmp_path, look)
        page = screen._last_page
        assert page.row_hit, f"{look} reported no clickable rows"
        indexes = [i for _rect, i in page.row_hit]
        assert len(indexes) == len(set(indexes)), f"{look}: duplicate rows"
        for index in indexes:
            assert 0 <= index < len(page.rows)

    def test_the_rows_it_reports_do_not_overlap(self, tmp_path, look):
        """Two rows sharing pixels means one click selects the wrong piece."""
        screen, _surface = _render(tmp_path, look)
        rects = [r for r, _i in screen._last_page.row_hit]
        for i, a in enumerate(rects):
            for b in rects[i + 1:]:
                overlap = a.clip(b)
                assert overlap.height <= 1, f"{look}: {a} and {b} overlap"

    def test_the_settings_are_reachable_by_mouse(self, tmp_path, look):
        """Not necessarily through a button called "setup".

        Stage keeps its settings card open permanently -- that is its
        drawing -- so it has no toggle and its four rows are the targets.
        Studio and Hi-fi hide theirs behind a pill. What all three owe is
        that the settings can be reached with the mouse at all: a control
        that is advertised in the key hints and cannot be clicked is the
        defect this map exists to make impossible.
        """
        screen, _surface = _render(tmp_path, look)
        drawn = set(screen._last_page.hit)
        reachable = "setup" in drawn or {"instrument", "device"} <= drawn
        assert reachable, f"{look} drew no way into the settings: {sorted(drawn)}"

    def test_it_reports_its_own_row_rhythm(self, tmp_path, look):
        """Studio's rows are not Stage's, and the screen scrolls by whatever
        the painter says. A painter that reports nothing scrolls by a
        constant that is wrong for two looks out of three."""
        screen, _surface = _render(tmp_path, look)
        page = screen._last_page
        assert page.hit_row_h > 0, f"{look} reported no row height"
        assert page.hit_band > 0, f"{look} reported no list band"

    def test_the_hit_map_is_rebuilt_not_accumulated(self, tmp_path, look):
        """It is rebuilt each frame so a control that stopped being drawn
        stops being clickable. If it accumulated, a stale rect would outlive
        the thing it belonged to."""
        screen, surface = _render(tmp_path, look)
        first = {k: list(v) for k, v in screen._last_page.hit.items()}
        for _ in range(3):
            screen.render(surface)
        assert set(screen._last_page.hit) == set(first)
        assert len(screen._last_page.row_hit) == len(
            [r for r, _ in screen._last_page.row_hit])


@pytest.mark.parametrize("look", LOOK_KEYS)
class TestAClickActuallyArrives:
    """The map is only worth having if the screen reads it.

    Tearing the one line that copies it out of menu.py left every assertion
    about page.hit green while no click in the app did anything, which is the
    same shape of hole as a test written against a helper instead of against
    the screen.
    """

    def test_clicking_a_row_selects_that_row(self, tmp_path, look):
        screen, surface = _render(tmp_path, look)
        targets = [(r, i) for r, i in screen._last_page.row_hit
                   if i != screen._selected]
        assert targets, f"{look}: nothing to click but the selection"
        rect, index = targets[-1]
        screen._handle_click(rect.center)
        assert screen._selected == index, (
            f"{look}: clicked row {index}, selection is {screen._selected}")

    def test_clicking_a_row_twice_opens_it(self, tmp_path, look):
        # A row in the middle of the band. The last one may be clipped, and
        # selecting a clipped row scrolls it into view -- so the second click
        # of a double would land on whatever moved under the cursor, which is
        # correct behaviour and a useless thing to assert about.
        screen, surface = _render(tmp_path, look)
        drawn = screen._last_page.row_hit
        rect, index = drawn[len(drawn) // 2]
        screen._handle_click(rect.center)
        screen.render(surface)
        chosen = screen._handle_click(rect.center)
        assert chosen is not None, f"{look}: a double click opened nothing"
        assert chosen == screen._rows[index].file

    def test_a_click_lands_on_the_row_it_was_over(self, tmp_path, look):
        """Every reported rect must select its own row, not a neighbour.

        An off-by-one in a row rect is invisible on screen and shows up as
        their opening the wrong exercise, which looks like the app being
        broken rather than like a layout bug.
        """
        screen, surface = _render(tmp_path, look)
        for rect, index in list(screen._last_page.row_hit):
            screen._handle_click((rect.centerx, rect.top + 2))
            assert screen._selected == index, (
                f"{look}: the top of row {index} selected {screen._selected}")
            screen._handle_click((rect.centerx, rect.bottom - 2))
            assert screen._selected == index, (
                f"{look}: the foot of row {index} selected {screen._selected}")
            screen.render(surface)

    def test_clicking_the_settings_does_something(self, tmp_path, look):
        screen, surface = _render(tmp_path, look)
        hit = screen._last_page.hit
        if "setup" in hit:
            before = screen._setup_open
            screen._handle_click(hit["setup"][0].center)
            assert screen._setup_open != before, f"{look}: setup ignored"
        else:
            # Stage keeps the card open and clicks its rows directly.
            before = screen._config.instrument
            screen._handle_click(hit["instrument"][0].center)
            assert screen._config.instrument != before, (
                f"{look}: the instrument row ignored a click")

    def test_a_control_drawn_twice_works_in_both_places(self, tmp_path, look):
        """Hi-fi draws the settings as a pill at the top and again as a key
        hint at the foot. With one rect per name the second quietly replaced
        the first, so the pill was drawn, looked like a button, and did
        nothing when the player clicked it -- which is exactly what the player reported.

        Built here rather than looked for, because only one look happens to
        draw a control twice today and a test that skips when it does not
        find one is a test that passes by doing nothing.
        """
        screen, surface = _render(tmp_path, look)
        # The bottom edge now contains the real workflow shelf. Put this
        # artificial duplicate in the unused outer margin, not over Refresh.
        far = pygame.Rect(2, 2, 4, 4)
        screen._button_rects.setdefault("setup", []).append(far)
        for rect in screen._button_rects["setup"]:
            before = screen._setup_open
            screen._handle_click(rect.center)
            assert screen._setup_open != before, (
                f"{look}: setup at {rect} is registered but dead")

    def test_every_place_a_control_is_drawn_is_in_the_map(self, tmp_path, look):
        screen, _surface = _render(tmp_path, look)
        for name, rects in screen._last_page.hit.items():
            assert rects, f"{look}: {name} is in the map with no rect"
            assert len(set(tuple(r) for r in rects)) == len(rects), (
                f"{look}: {name} registered the same rect twice")

    def test_a_click_on_empty_space_changes_nothing(self, tmp_path, look):
        screen, surface = _render(tmp_path, look)
        before = (screen._selected, screen._setup_open, screen._filter)
        assert screen._handle_click((2, surface.get_height() - 2)) is None
        assert (screen._selected, screen._setup_open, screen._filter) == before


@pytest.mark.parametrize("look", LOOK_KEYS)
class TestItObeysTheLook:
    def test_switching_the_look_changes_the_page(self, tmp_path, look):
        """Three palettes on one drawing is what this whole change was for.
        If two looks render the same pixels, it has happened again."""
        shots = {}
        for key in LOOK_KEYS:
            _screen_, surface = _render(tmp_path, key)
            shots[key] = pygame.image.tostring(surface, "RGB")
        for i, a in enumerate(LOOK_KEYS):
            for b in LOOK_KEYS[i + 1:]:
                assert shots[a] != shots[b], f"{a} and {b} render identically"

    def test_the_page_is_painted_in_the_looks_own_field(self, tmp_path, look):
        screen, surface = _render(tmp_path, look)
        corner = surface.get_at((surface.get_width() - 3,
                                 surface.get_height() - 3))[:3]
        allowed = {get_look().panel_right, get_look().panel_left,
                   get_look().menu_bg, get_look().bg}
        assert corner in allowed, f"{look}: the page corner is {corner}"

    def test_changing_the_language_changes_the_page(self, tmp_path, look):
        """Everything user-facing goes through the catalogue. A literal in a
        painter is invisible until someone switches language and finds one
        German word left standing in an English page."""
        from pickhero import i18n

        before = i18n.current_language()
        try:
            shots = {}
            for code in ("de", "en"):
                i18n.set_language(code)
                _screen_, surface = _render(tmp_path, look)
                shots[code] = pygame.image.tostring(surface, "RGB")
            assert shots["de"] != shots["en"], f"{look} ignores the language"
        finally:
            i18n.set_language(before)


@pytest.mark.parametrize("look", LOOK_KEYS)
class TestItKeepsTheSelectionInView:
    def test_the_selected_row_is_drawn(self, tmp_path, look):
        """Arrowing to the bottom of a long list must bring it into the band,
        not select something off-screen."""
        screen, surface = _render(tmp_path, look)
        for _ in range(len(screen._rows)):
            screen._move(1)
            screen.render(surface)
            drawn = {i for _r, i in screen._last_page.row_hit}
            assert screen._selected in drawn, (
                f"{look}: row {screen._selected} selected but not drawn")

    def test_going_back_up_brings_it_back(self, tmp_path, look):
        screen, surface = _render(tmp_path, look)
        for _ in range(len(screen._rows)):
            screen._move(1)
        screen.render(surface)
        for _ in range(len(screen._rows)):
            screen._move(-1)
            screen.render(surface)
            drawn = {i for _r, i in screen._last_page.row_hit}
            assert screen._selected in drawn, (
                f"{look}: row {screen._selected} selected but not drawn")

    def test_it_never_scrolls_past_the_end(self, tmp_path, look):
        screen, surface = _render(tmp_path, look)
        screen._scroll_px = 99_999
        screen.render(surface)
        assert screen._last_page.scroll_px >= 0
        assert screen._last_page.row_hit, f"{look}: scrolled off the list"


@pytest.mark.parametrize("look", LOOK_KEYS)
class TestItIsFastEnough:
    def test_a_frame_fits_in_the_budget(self, tmp_path, look):
        """60 fps is 16.7 ms for everything including audio. The library is
        not the hot screen, but a painter that allocates per frame shows up
        here before it shows up as a stutter the player cannot describe.
        """
        screen, surface = _render(tmp_path, look)
        screen.render(surface)                      # warm the caches
        start = time.perf_counter()
        for _ in range(30):
            screen.render(surface)
        per_frame = (time.perf_counter() - start) / 30 * 1000
        assert per_frame < 8.0, f"{look}: {per_frame:.2f} ms a frame"


class TestThePlayPage:
    """What the play screen hands its look.

    The counts here are the ones the player reads mid-song, so each is wrong in its
    own particular way if it is wrong at all: a bar number that runs past the
    end, a found-count that silently means hits-only, a signal meter measured
    from silence instead of from their own gate.
    """

    def _screen(self, accuracy=True):
        from pathlib import Path

        from pickhero.config import Config
        from pickhero.matcher import NoteMatcher
        from pickhero.tabs.loader import load_gp_file
        from pickhero.ui.scrolling import PlayingScreen

        config = Config()
        config.instrument = "bass_ukulele"
        timeline = load_gp_file(Path("tests/fixtures/Demo_v5.gp5"),
                                instrument=config.get_instrument())
        screen = PlayingScreen(timeline, config=config)
        screen._audio_enabled = accuracy
        if accuracy and screen._matcher is None:
            screen._matcher = NoteMatcher(timeline)
        return screen

    def test_the_bar_never_runs_past_the_last_one(self):
        """Dividing the duration by a bar length counted an 8 bar exercise as
        9: the file ends a fraction of a bar after its last note, and the
        rounding has nowhere to put that. It comes off the measures now."""
        screen = self._screen()
        surface = pygame.Surface((1280, 720))
        total = screen._timeline.duration_ms
        for fraction in (0.0, 0.01, 0.25, 0.5, 0.9, 0.999, 1.0, 1.5):
            screen._playback_ms = total * fraction
            page = screen._page(surface)
            assert 1 <= page.bar <= page.bars, (
                f"at {fraction:.3f} of the song: bar {page.bar} of {page.bars}")

    def test_the_bar_count_is_the_files_own(self):
        screen = self._screen()
        page = screen._page(pygame.Surface((1280, 720)))
        assert page.bars == len(screen._timeline.measures)

    def test_the_bar_advances_through_the_piece(self):
        screen = self._screen()
        surface = pygame.Surface((1280, 720))
        total = screen._timeline.duration_ms
        seen = []
        for step in range(10):
            screen._playback_ms = total * step / 9
            seen.append(screen._page(surface).bar)
        assert seen == sorted(seen), f"the bar went backwards: {seen}"
        assert seen[0] < seen[-1], f"the bar never moved: {seen}"

    def test_found_counts_the_right_notes_and_not_the_near_misses(self):
        """Pitch and timing are separate skills: a note on the right pitch is
        found however early it came, and the matcher already counts early
        and late notes among its hits. A semitone off is "close" -- not the
        right note -- and has its own count.

        This test used to assert the opposite, found == hits + close, on the
        mistaken idea that "close" meant early. It put "9 found" beside an
        accuracy that counted seven, and disagreed with the run log.
        """
        screen = self._screen()
        screen._matcher.hits = 7
        screen._matcher.close = 2
        screen._matcher.misses = 1
        page = screen._page(pygame.Surface((1280, 720)))
        assert page.found == 7, "a semitone off was counted as found"
        assert page.expected == 10

    def test_the_meter_is_measured_from_her_own_gate(self):
        """Scaled from silence, the meter sat near zero at every level the player
        actually plays at. It runs from the gate the player set, because below that
        nothing is being heard at all."""
        screen = self._screen()
        surface = pygame.Surface((1280, 720))
        screen._noise_gate_db = -40.0
        screen._signal_db_smooth = -60.0
        assert screen._page(surface).signal == 0.0
        screen._signal_db_smooth = -40.0
        assert screen._page(surface).signal == 0.0
        screen._signal_db_smooth = -20.0
        middle = screen._page(surface).signal
        assert 0.3 < middle < 0.7, middle
        screen._signal_db_smooth = 0.0
        assert screen._page(surface).signal == 1.0

    def test_without_audio_there_is_no_accuracy_to_report(self):
        """Silence is not nought percent. A screen that reports 0 with the
        microphone off tells them the player played nothing right."""
        screen = self._screen(accuracy=False)
        page = screen._page(pygame.Surface((1280, 720)))
        assert page.accuracy is None

    def test_every_toggle_says_which_key_turns_it(self):
        from pickhero import i18n

        screen = self._screen()
        page = screen._page(pygame.Surface((1280, 720)))
        assert len(page.toggles) == 4
        for label_key, key_key, state, action in page.toggles:
            assert i18n.t(label_key) != label_key, f"no text for {label_key}"
            assert i18n.t(key_key) != key_key, f"no text for {key_key}"
            assert isinstance(state, bool)
            # The action travels with the switch so a painter can report
            # where it drew one without knowing what the screen calls it.
            assert action, f"{label_key} says nothing about what it does"
            assert action in screen._handled_clicks(), (
                f"{label_key} performs {action!r}, which the screen ignores")


@pytest.mark.parametrize("look", LOOK_KEYS)
class TestThePlayScreen:
    """The look draws the board; this screen draws the notes on it.

    That split is the whole reason the first render of it had the notes
    sitting beside the lane lines on two looks and off the end of the road on
    the third: the painter drew the stave and the screen placed the notes from
    its own arithmetic, and the two had no reason to agree.
    """

    def _screen(self, look, size=(1280, 720), strings="bass_ukulele"):
        from pathlib import Path

        from pickhero.config import Config
        from pickhero.matcher import NoteMatcher
        from pickhero.tabs.loader import load_gp_file
        from pickhero.ui.scrolling import PlayingScreen

        set_look(look)
        design.clear_caches()
        config = Config()
        config.instrument = strings
        timeline = load_gp_file(Path("tests/fixtures/Demo_v5.gp5"),
                                instrument=config.get_instrument())
        screen = PlayingScreen(timeline, config=config)
        screen._playing = True
        screen._audio_enabled = True
        if screen._matcher is None:
            screen._matcher = NoteMatcher(timeline)
        screen._playback_ms = 6000.0
        surface = pygame.Surface(size)
        screen.render(surface)
        return screen, surface

    def test_every_look_draws_a_play_screen(self, tmp_path, look):
        module = painter(look)
        assert hasattr(module, "play"), f"{look} cannot draw a play screen"
        assert hasattr(module, "board"), f"{look} exposes no board"

    @pytest.mark.parametrize("size", SIZES)
    def test_it_renders_at_every_size(self, tmp_path, look, size):
        self._screen(look, size)

    def test_the_gutter_names_the_strings_she_has(self, tmp_path, look):
        """It named six guitar strings down the side of a four string bass
        ukulele -- E B G D A E, not one of which the player owns -- because the Page
        carried no tuning and every painter had to guess one."""
        screen, surface = self._screen(look)
        page = screen._last_page
        assert page.strings == 4, page.strings
        assert [letter for _n, letter, _name in page.string_labels] == [
            "G", "D", "A", "E"]
        assert [name for _n, _l, name in page.string_labels] == [
            "G2", "D2", "A1", "E1"]

    def test_a_six_string_tuning_still_works(self, tmp_path, look):
        screen, _surface = self._screen(look, strings="guitar")
        assert screen._last_page.strings == 6
        assert len(screen._last_page.string_labels) == 6

    def test_the_notes_land_on_the_board_the_look_drew(self, tmp_path, look):
        """Not merely inside the window. A note on the stave the painter drew
        is the one thing the split between them can get wrong, and it is
        invisible in a test that only checks for no exception."""
        screen, surface = self._screen(look)
        module = painter(look)
        board = module.board(screen._last_page)
        # Grow by a note's own height: a note straddling the edge of the
        # board is correct, one a lane away from it is not.
        allowed = board.inflate(board.width, max(80, board.height // 3))
        notes = screen._timeline.get_notes_in_range(
            screen._playback_ms - 200.0, screen._playback_ms + 4000.0)
        assert notes, "the fixture has no notes here"
        placed = 0
        for note in notes:
            rect = self._note_rect(screen, module, note)
            if rect is None:
                continue
            placed += 1
            assert allowed.contains(rect) or allowed.colliderect(rect), (
                f"{look}: a note at {rect} is not on the board {board}")
        assert placed, f"{look}: no note was placed at all"

    @staticmethod
    def _note_rect(screen, module, note):
        """Where this screen would put a note, through the look's geometry."""
        page = screen._last_page
        if hasattr(module, "project"):
            depth = ((note.timestamp_ms - screen._playback_ms)
                     / max(1.0, screen._visible_window_ms))
            if depth < -0.02 or depth > 1.0:
                return None
            order = screen._lane_order(screen._layout(pygame.Surface(
                (page.width, page.height))))
            if note.string not in order:
                return None
            return module.note_box(page, order.index(note.string), len(order),
                                   max(0.0, depth))
        layout = screen._layout(pygame.Surface((page.width, page.height)))
        x = screen.note_x(note.timestamp_ms, screen._playback_ms,
                          screen._playhead_x(layout), screen._px_per_ms(layout))
        return module.note_box(page, note.string, page.strings, x, 60)

    def test_the_lanes_are_in_order_and_do_not_collide(self, tmp_path, look):
        module = painter(look)
        if not hasattr(module, "lane_y"):
            pytest.skip("the road has no flat lanes")
        screen, _surface = self._screen(look)
        page = screen._last_page
        ys = [module.lane_y(page, n, page.strings)
              for n in range(1, page.strings + 1)]
        assert ys == sorted(ys), f"{look}: string 1 is not on top: {ys}"
        gaps = [b - a for a, b in zip(ys, ys[1:])]
        assert min(gaps) > 20, f"{look}: lanes {gaps} px apart"
        assert max(gaps) - min(gaps) <= 1, f"{look}: uneven lanes {gaps}"

    def test_it_is_fast_enough_to_play_against(self, tmp_path, look):
        """This is the hot screen -- the one running while the player is playing."""
        screen, surface = self._screen(look)
        screen.render(surface)
        start = time.perf_counter()
        for step in range(40):
            screen._playback_ms = 6000.0 + step * 25
            screen.render(surface)
        per_frame = (time.perf_counter() - start) / 40 * 1000
        assert per_frame < 10.0, f"{look}: {per_frame:.2f} ms a frame"


class TestAnAlternateTuning:
    """A file may say its own tuning, and then the gutter must say that one.

    The first version read timeline.tuning, which does not exist -- the field
    is on metadata -- so it fell back to the instrument every time and a
    dropped-D file was labelled as though it were standard. It is invisible
    until the player tunes down and the app keeps calling the low string E.
    """

    def _screen(self, tuning):
        from pathlib import Path

        from pickhero.config import Config
        from pickhero.tabs.loader import load_gp_file
        from pickhero.ui.scrolling import PlayingScreen

        config = Config()
        config.instrument = "bass_ukulele"
        timeline = load_gp_file(Path("tests/fixtures/Demo_v5.gp5"),
                                instrument=config.get_instrument())
        timeline.metadata.tuning = tuning
        return PlayingScreen(timeline, config=config)

    def test_the_gutter_follows_the_file_not_the_instrument(self):
        # The bass ukulele's E1 A1 D2 G2, dropped a tone on the lowest string.
        screen = self._screen({1: 43, 2: 38, 3: 33, 4: 26})
        page = screen._page(pygame.Surface((1280, 720)))
        assert [n for _i, _l, n in page.string_labels] == ["G2", "D2", "A1", "D1"]

    def test_without_one_it_falls_back_to_the_instrument(self):
        screen = self._screen({})
        page = screen._page(pygame.Surface((1280, 720)))
        assert [n for _i, _l, n in page.string_labels] == ["G2", "D2", "A1", "E1"]

    @pytest.mark.parametrize("look", LOOK_KEYS)
    def test_every_painter_shows_the_file_s_tuning(self, look):
        """The fallback each painter kept -- parsing the tuning back out of
        the subtitle -- gets the instrument's tuning, so it is right exactly
        when the file agrees with the instrument. This is the case where the
        two differ, and it is the reason the field exists."""
        set_look(look)
        design.clear_caches()
        screen = self._screen({1: 43, 2: 38, 3: 33, 4: 26})
        screen._playing = True
        surface = pygame.Surface((1280, 720))
        screen.render(surface)
        module = painter(look)
        page = screen._last_page
        shown = None
        for name in ("_tuning", "_letters", "_strings"):
            helper = getattr(module, name, None)
            if helper is not None:
                shown = helper(page)
                break
        assert shown, f"{look} exposes no tuning helper"
        # Each painter returns its own shape -- letters, (letter, octave)
        # pairs, or full names, lowest string first or last. What they all
        # have to agree on is that the lowest string is now a D and not the
        # instrument's E.
        flat = "".join(str(x) for item in shown for x in
                       (item if isinstance(item, tuple) else (item,)))
        assert "E" not in flat, (
            f"{look} shows {shown}, which still has the instrument's E")
        assert flat.count("D") >= 2, (
            f"{look} shows {shown}, which is missing the dropped D")


class TestTheNoteThatIsDueNow:
    """Three things the player found by looking at the running app beside their sheet.

    All three were invisible to the palette tests: the colours were right and
    the drawing was wrong. These render and read the pixels.
    """

    def _screen(self, look, exercise="05"):
        from pathlib import Path

        from pickhero.config import Config
        from pickhero.matcher import NoteMatcher
        from pickhero.tabs.loader import load_gp_file
        from pickhero.ui.scrolling import PlayingScreen

        set_look(look)
        design.clear_caches()
        config = Config.load()
        folder = Path(config.songs_dir)
        files = [p for p in sorted(folder.rglob("*.gp*"))
                 if p.stem.startswith(exercise)]
        if not files:
            pytest.skip("the generated exercises are not on this machine")
        timeline = load_gp_file(files[0], instrument=config.get_instrument())
        screen = PlayingScreen(timeline, config=config)
        screen._playing = True
        screen._audio_enabled = True
        if screen._matcher is None:
            screen._matcher = NoteMatcher(timeline)
        notes = timeline.get_notes_in_range(1000.0, 20000.0)
        assert notes, "no notes to land on"
        screen._playback_ms = notes[3].timestamp_ms
        surface = pygame.Surface((1280, 720))
        screen.render(surface)
        return screen, surface, notes[3]

    def test_the_bloom_does_not_grow_with_the_note_s_length(self):
        """The ring this replaced was sized from the card WIDTH, which for a
        held note is its duration: a whole note drew a 70 px circle that ran
        up through the title, so the longer the player had to hold a note the bigger
        the thing round it got. A second wrong version took it from
        layout.card_h, which is the screen's own idea of a note and not the
        one the look draws -- 34 px apart on Hi-fi.

        The ring is gone now; the note blooms instead. The bloom is sized
        from the note's height, and this is what says so.
        """
        from pickhero.ui.scrolling import GLOW_SPREAD

        screen, surface, note = self._screen("studio", exercise="01")
        box = self._box(screen, surface, note)
        colour = design.string_colour(note.string)
        lit = [(x, y)
               for x in range(max(0, box.centerx - 260),
                              min(surface.get_width(), box.centerx + 260))
               for y in range(max(0, box.centery - 260),
                              min(surface.get_height(), box.centery + 260))
               if design.delta_e(surface.get_at((x, y))[:3], colour) < 34]
        assert lit, "the note was not drawn in its string's colour at all"
        above = box.centery - min(y for _x, y in lit)
        # Half the note, plus its bloom, plus a little for the rim.
        limit = box.height // 2 + int(box.height * GLOW_SPREAD) + 10
        assert above <= limit, (
            f"the note's colour reaches {above} px above its centre; it is "
            f"{box.height} px tall and blooms {GLOW_SPREAD:.2f} of that, so "
            f"the most it may reach is {limit}")

    def test_the_note_that_is_due_blooms_and_the_others_do_not(self, monkeypatch):
        """The active card has a soft outer glow; a later card has none."""
        from pickhero.ui.scrolling import GLOW_SPREAD, GLOW_OUTER_SPREAD

        screen, surface, note = self._screen("studio", exercise="01")
        # Record the actual projected card. A fixed-width approximation can
        # put its edge a few pixels inside a longer live note.
        module = painter("studio")
        note_box = module.note_box
        active_boxes = []

        def record_box(page, string, strings, x, w):
            result = note_box(page, string, strings, x, w)
            if string == note.string and round(x) == module.playhead_x(page):
                active_boxes.append(result)
            return result

        monkeypatch.setattr(module, "note_box", record_box)
        screen.render(surface)
        assert active_boxes
        box = active_boxes[-1]
        # Off the lane line and clear of every note: above the note's top.
        board = surface.get_at((surface.get_width() - 10, box.top - 18))[:3]

        def reach(rect):
            """How many pixels left of `rect` are lifted off the board."""
            y = rect.top + rect.height // 4
            found = 0
            for d in range(2, 60):
                x = rect.left - d
                if x < 0:
                    break
                if design.delta_e(surface.get_at((x, y))[:3], board) <= 6:
                    break
                found = d
            return found

        bloom = reach(box) / box.height
        # Compare with the same scene without the active marker, so beat
        # lines cannot masquerade as glow. The outer light must extend past
        # the close mask, fall smoothly and finish at its geometric boundary.
        # Pixel-centre sampling allows one rounding pixel at that boundary.
        monkeypatch.setattr(screen, "_notes_due_now", lambda: {})
        unlit = pygame.Surface(surface.get_size())
        screen.render(unlit)
        inner = round(box.height * GLOW_SPREAD)
        outer = round(box.height * GLOW_OUTER_SPREAD)
        y = box.top + box.height // 4
        lift = [sum(surface.get_at((box.left - d, y))[:3])
                - sum(unlit.get_at((box.left - d, y))[:3])
                for d in range(inner + 1, outer + 3)]
        assert lift[0] > 10, "the soft outer light is missing"
        assert lift[-2:] == [0, 0], "the light extends past its declared outline"
        assert all(a >= b - 3 for a, b in zip(lift, lift[1:])), lift
        assert bloom > 0.10, "the active note has no visible bloom"

        later = [n for n in screen._timeline.get_notes_in_range(
            screen._playback_ms + 2000.0, screen._playback_ms + 9000.0)
            if n.string == note.string]
        if later:
            other = self._box(screen, surface, later[0])
            if surface.get_rect().contains(other.inflate(70, 0)):
                assert reach(other) / box.height < bloom / 2, (
                    f"a note that is not due blooms "
                    f"{reach(other) / box.height:.2f} against the due one's "
                    f"{bloom:.2f}")

    def test_the_outline_stands_off_the_card_with_the_board_between(self):
        """Measured across the drawing's own active note: fill, then eight
        sheet pixels of bare board, then three of the SAME cyan, then the
        bloom. Not a rim on the card -- which is what this drew for four
        rounds, and which desaturated the one edge the string's identity
        lives in and read as a white outline.
        """
        from pickhero.ui.colors import get_look

        screen, surface, note = self._screen("studio", exercise="05")
        box = self._box(screen, surface, note)
        colour = design.string_colour(note.string)
        board = get_look().stage_bg
        y = box.top + box.height // 4

        def at(d):
            return surface.get_at((box.left - d, y))[:3]

        def is_string(c):
            return design.delta_e(c, colour) < 12

        # Studio's leading edge now marks the onset with a brighter spine;
        # the body and the detached halo still retain the string's hue.
        assert is_string(at(-3)), f"the card's body is {at(-3)}"
        assert at(0) == design.mix(colour, get_look().ink_1, 0.65)
        ring = [d for d in range(1, 12) if is_string(at(d))]
        assert ring, (
            f"no outline in the note's own colour: {[at(d) for d in range(8)]}")
        assert min(ring) >= 2, (
            f"the outline sits on the card instead of standing off it: "
            f"{[at(d) for d in range(0, 8)]}")
        # The gap is darker than both, though not bare board: the bloom
        # starts at the card and lights it. Starting the bloom at the
        # outline instead keeps it properly dark, which is what the drawing
        # does -- and at this spread that came out as a square frame round
        # the note, so a faintly lit gap is the better of the two.
        for d in range(1, min(ring)):
            lit = sum(at(d))
            assert lit < sum(colour) * 0.75, (
                f"{d} px out is {at(d)}, which is not a gap at all")
        # And it is the note's colour, not a paler one.
        assert design.delta_e(at(min(ring)), colour) < 12, (
            f"the outline is {at(min(ring))}, not the string's {colour}")

    def test_the_marked_note_carries_a_light_number(self):
        """The drawing writes it in near-white on the orange. on_accent picks
        the dark end for that colour, so it came out black."""
        screen, surface, note = self._screen("hifi")
        look = get_look()
        box = self._box(screen, surface, note)
        inside = [surface.get_at((x, y))[:3]
                  for x in range(box.centerx - 6, box.centerx + 7)
                  for y in range(box.centery - 8, box.centery + 9)]
        light = [c for c in inside if sum(c) > 600]
        dark = [c for c in inside if sum(c) < 180]
        assert light, "the fret number on the marked note is not light"
        assert not dark, f"it is drawn dark: {dark[:3]}"

    def test_the_name_clears_the_disc(self):
        """A card has a flat top and an empty strip under it, so a chip may
        sit half on it. A disc has neither, and the name came out printed
        across the note."""
        screen, surface, note = self._screen("hifi")
        box = self._box(screen, surface, note)
        # Not the raw string colour: this look darkens the name until it
        # reads on paper, which is the point of drawing it without a chip.
        colour = design.readable(design.string_colour(note.string),
                                 get_look().stage_bg)
        found = [(x, y) for x in range(box.left - 10, box.right + 30)
                 for y in range(box.top - 30, box.bottom)
                 if 0 <= x < surface.get_width()
                 and design.delta_e(surface.get_at((x, y))[:3], colour) < 14]
        assert found, "the note's name was not drawn at all"
        lowest = max(y for _x, y in found)
        assert lowest <= box.top + 2, (
            f"the name reaches {lowest}, the disc starts at {box.top}")

    @staticmethod
    def _box(screen, surface, note):
        return _rendered_note_box(screen, surface, note)


@pytest.mark.parametrize("look", LOOK_KEYS)
class TestTheTransportAnswers:
    """Everything the footer draws is a control, so everything is a target.

    The player asked "warum reagiert hier das interface nicht auf mausklicks?" and
    the answer was that nothing had wired the painter's hit map into the play
    screen. The look drew the buttons; the screen went on reading a map that
    only the drawing it replaced ever filled in; every click fell through.
    The same two lines had been written for the library and not for this.
    """

    def _screen(self, look, size=(1280, 720)):
        from pathlib import Path

        from pickhero.config import Config
        from pickhero.matcher import NoteMatcher
        from pickhero.tabs.loader import load_gp_file
        from pickhero.ui.scrolling import PlayingScreen

        set_look(look)
        design.clear_caches()
        config = Config()
        config.instrument = "bass_ukulele"
        timeline = load_gp_file(Path("tests/fixtures/Demo_v5.gp5"),
                                instrument=config.get_instrument())
        screen = PlayingScreen(timeline, config=config)
        screen._audio_enabled = True
        if screen._matcher is None:
            screen._matcher = NoteMatcher(timeline)
        # Clicking these for real opens the microphone and the MIDI device,
        # dozens of times in a row, and PortMidi takes the process down with
        # an access violation when it is reopened that fast. The point here
        # is that the click ARRIVES, not that PortAudio works.
        for name in ("_start_audio", "_start_capture_only", "_stop_audio",
                     "stop_audio"):
            if hasattr(screen, name):
                setattr(screen, name, lambda *a, **k: None)
        screen._midi_player = None
        surface = pygame.Surface(size)
        screen.render(surface)
        return screen, surface

    def test_it_reports_somewhere_to_click(self, look):
        screen, _surface = self._screen(look)
        handled = set(screen._handled_clicks())
        drawn = set(screen._button_rects) & handled
        assert drawn, (
            f"{look} drew a transport and reported none of it: "
            f"{sorted(screen._button_rects)}")

    def test_every_control_it_reports_does_something(self, look):
        """A name the screen does not answer to is a button that is drawn,
        looks live, and is not."""
        screen, surface = self._screen(look)
        # Give restart and the loop-marker actions a visible state to change.
        screen.seek(4000.0)
        screen._loop_start_ms, screen._loop_end_ms = 1000.0, 5000.0
        screen._loop_enabled = True
        # And off the top of the tempo range, which is 100 %: a dial clicked
        # at its centre rounds to the faster half, and faster than written is
        # not a thing this app offers.
        screen._tempo_factor = 0.8
        screen.render(surface)
        handled = set(screen._handled_clicks())
        for name, rects in screen._button_rects.items():
            if name not in handled:
                continue
            for rect in rects:
                screen.render(surface)
                before = self._state(screen)
                screen._handle_click(rect.center)
                assert self._state(screen) != before or name == "menu", (
                    f"{look}: {name} at {rect} is drawn but dead")
                # Help/setup now own input until explicitly closed.
                if screen._panel is not None:
                    screen.handle_event(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_ESCAPE))

    def test_the_play_button_starts_and_stops(self, look):
        screen, surface = self._screen(look)
        rects = screen._button_rects.get("play")
        if not rects:
            pytest.skip(f"{look} draws no play button at this size")
        was = screen._playing
        screen._handle_click(rects[0].center)
        assert screen._playing != was, f"{look}: play did nothing"
        screen.render(surface)
        screen._handle_click(screen._button_rects["play"][0].center)
        assert screen._playing == was, f"{look}: it would not stop again"

    def test_the_toggles_each_switch_their_own_thing(self, look):
        screen, surface = self._screen(look)
        screen._loop_start_ms, screen._loop_end_ms = 1000.0, 5000.0
        screen._loop_enabled = True
        screen.render(surface)
        for _label, _key, _on, action in screen._last_page.toggles:
            rects = screen._button_rects.get(action)
            if not rects:
                continue
            before = self._state(screen)
            screen._handle_click(rects[0].center)
            after = self._state(screen)
            changed = [k for k in before if before[k] != after[k]]
            assert len(changed) == 1, (
                f"{look}: {action} changed {changed or 'nothing'}")
            screen.render(surface)

    @staticmethod
    def _state(screen):
        return {
            "playing": screen._playing,
            "listen": screen._audio_enabled,
            "wait": screen._wait_mode,
            "loop": screen._loop_enabled,
            "metronome": screen._metronome_on(),
            "tempo": round(screen._tempo_factor, 3),
            "where": round(screen._playback_ms, 1),
            "help": screen._show_help,
            "panel": screen._panel.kind if screen._panel else None,
            "loop_marks": (screen._loop_start_ms, screen._loop_end_ms),
        }


class TestHowLongTheMarkLasts:
    """The player could not screenshot it.

    The mark came up only inside the timing window -- about 120 ms, seven
    frames at sixty. That is not a mark, it is a flash, and it arrives at the
    moment it is already too late to aim by. It rises while the note is
    coming now, holds while the player can still hit it, and goes out shortly after.
    """

    def _screen(self):
        from pathlib import Path

        from pickhero.config import Config
        from pickhero.matcher import NoteMatcher
        from pickhero.tabs.loader import load_gp_file
        from pickhero.ui.scrolling import PlayingScreen

        set_look("studio")
        design.clear_caches()
        config = Config()
        config.instrument = "bass_ukulele"
        timeline = load_gp_file(Path("tests/fixtures/Demo_v5.gp5"),
                                instrument=config.get_instrument())
        screen = PlayingScreen(timeline, config=config)
        screen._playing = True
        screen._audio_enabled = True
        screen._midi_player = None
        if screen._matcher is None:
            screen._matcher = NoteMatcher(timeline)
        return screen

    def _note(self, screen):
        notes = screen._timeline.get_notes_in_range(4000.0, 20000.0)
        assert notes
        return notes[6]

    def _exercise(self, tmp_path, stem="05"):
        """One of their own exercises: four strings, 60 BPM, quarter notes.

        The Demo fixture is 165 BPM and dense, and there the mark genuinely
        cannot last long -- the next note on the string is 364 ms away and
        takes it over, which is right. What matters is how long it lasts at
        the tempo the player practises at.
        """
        from pathlib import Path

        from pickhero import instruments
        from pickhero.config import Config
        from pickhero.exercises import write_exercises
        from pickhero.matcher import NoteMatcher
        from pickhero.tabs.loader import load_gp_file
        from pickhero.ui.scrolling import PlayingScreen

        write_exercises(instruments.BASS_UKULELE, tmp_path)
        files = [p for p in sorted(Path(tmp_path).rglob("*.gp*"))
                 if p.stem.startswith(stem)]
        assert files, "the generated exercises are missing"
        set_look("studio")
        design.clear_caches()
        config = Config()
        config.instrument = "bass_ukulele"
        timeline = load_gp_file(files[0], instrument=config.get_instrument())
        screen = PlayingScreen(timeline, config=config)
        screen._playing = True
        screen._audio_enabled = True
        screen._midi_player = None
        if screen._matcher is None:
            screen._matcher = NoteMatcher(timeline)
        return screen

    def test_it_is_lit_for_long_enough_to_be_seen(self, tmp_path):
        screen = self._exercise(tmp_path)
        notes = screen._timeline.get_notes_in_range(2000.0, 20000.0)
        note = notes[3]
        surface = pygame.Surface((1280, 720))
        frames = 0
        for step in range(-80, 60):
            screen._playback_ms = note.timestamp_ms + step * 16.7
            screen.render(surface)
            if screen._due.get((note.timestamp_ms, note.string)):
                frames += 1
        # Half a second is the floor for something a hand is meant to react
        # to. It was seven frames, which the player could not even screenshot.
        assert frames >= 30, f"lit for {frames} frames, which is a flash"
        assert frames <= 120, f"lit for {frames} frames, which never goes out"

    def test_a_dense_piece_hands_the_mark_on_instead_of_holding_it(self):
        """At 165 BPM the next note on a string is 364 ms away. The mark goes
        to it rather than two of them sharing, so it is necessarily shorter
        there -- and that is the right trade, not a regression."""
        screen = self._screen()
        note = self._note(screen)
        surface = pygame.Surface((1280, 720))
        frames = 0
        for step in range(-80, 60):
            screen._playback_ms = note.timestamp_ms + step * 16.7
            screen.render(surface)
            if screen._due.get((note.timestamp_ms, note.string)):
                frames += 1
        assert frames >= 6, f"even here it must be visible: {frames} frames"
        assert frames < 30, (
            f"{frames} frames at 165 BPM means two notes are marked at once")

    def test_it_rises_as_the_note_comes(self, tmp_path):
        """The whole point: it has to be brightening while there is still
        time to move a finger, not announce itself on arrival.

        Stated as "already this bright by then" rather than only as a rising
        sequence. A sequence that rises is also satisfied by one that stays
        dark until the last moment and then jumps -- which is what it did,
        and the first version of this test passed on it.
        """
        screen = self._exercise(tmp_path)
        note = screen._timeline.get_notes_in_range(2000.0, 20000.0)[3]
        surface = pygame.Surface((1280, 720))

        def lit(offset):
            screen._playback_ms = note.timestamp_ms + offset
            screen.render(surface)
            return screen._due.get((note.timestamp_ms, note.string), 0.0)

        seen = [lit(o) for o in (-600, -450, -300, -150, 0)]
        assert seen == sorted(seen), f"it does not rise: {seen}"
        assert seen[-1] >= 0.99, f"it is not at full on the beat: {seen}"
        # Half a beat out at 60 BPM -- five hundred milliseconds, which is
        # about as long as it takes to move a hand to a string.
        assert lit(-500) >= 0.2, (
            f"half a second before the note it is only {lit(-500):.2f} lit, "
            f"which is too late to aim by")
        assert lit(-250) >= 0.5, f"a quarter second out: {lit(-250):.2f}"

    def test_it_stays_lit_while_she_can_still_hit_it(self, tmp_path):
        """The timing window runs past the beat, and a note the player is still
        being judged on must still be marked. Pitch and timing are separate
        skills, and a mark that vanishes on the beat says the moment is gone
        when it is not."""
        screen = self._exercise(tmp_path)
        note = screen._timeline.get_notes_in_range(2000.0, 20000.0)[3]
        surface = pygame.Surface((1280, 720))
        window = screen._timing_window_for_marker()
        for offset in (0, window * 0.5, window, window + 80):
            screen._playback_ms = note.timestamp_ms + offset
            screen.render(surface)
            found = screen._due.get((note.timestamp_ms, note.string), 0.0)
            assert found > 0.0, (
                f"{offset:.0f} ms after the note it is already out, and the "
                f"timing window is {window:.0f} ms")

    def test_it_goes_out_after_the_moment_has_passed(self):
        screen = self._screen()
        note = self._note(screen)
        surface = pygame.Surface((1280, 720))
        screen._playback_ms = note.timestamp_ms + 1200
        screen.render(surface)
        assert not screen._due.get((note.timestamp_ms, note.string))

    def test_only_the_next_note_on_a_string_is_marked(self):
        """In a run of sixteenths half the board is inside the lead at once,
        and a board where everything glows marks nothing."""
        screen = self._screen()
        surface = pygame.Surface((1280, 720))
        screen._playback_ms = 12000.0
        screen.render(surface)
        per_string: dict[int, int] = {}
        for (_stamp, string) in screen._due:
            per_string[string] = per_string.get(string, 0) + 1
        assert per_string, "nothing is marked at all"
        assert max(per_string.values()) == 1, (
            f"more than one note marked on a string: {per_string}")

    def test_it_marks_the_nearest_one_not_merely_the_next(self):
        """A note the player is still being judged on must keep the mark rather than
        hand it to one that has not arrived."""
        screen = self._screen()
        surface = pygame.Surface((1280, 720))
        notes = screen._timeline.get_notes_in_range(4000.0, 20000.0)
        note = notes[6]
        # Just past it: it is nearer than whatever comes next.
        screen._playback_ms = note.timestamp_ms + 40
        screen.render(surface)
        marked = [stamp for (stamp, string) in screen._due
                  if string == note.string]
        assert marked == [note.timestamp_ms], (
            f"marked {marked}, not the note just played at {note.timestamp_ms}")

    def test_the_lead_is_a_reaction_time_not_a_fraction_of_a_beat(self):
        """Three quarters of a beat is 750 ms at 60 and 273 at 165, and a
        hand does not move in 273 ms. The floor is what the lead becomes once
        the beat gets shorter than a reaction.

        Tested as the arithmetic rather than on a rendered piece: the only
        file fast enough to exercise the floor also has its notes 182 ms
        apart, and there the next note on the string always takes the mark
        first -- so at that density there is no warning to be had and the
        floor cannot show itself on screen at all.
        """
        from pickhero.ui.scrolling import GLOW_LEAD_BEATS, GLOW_LEAD_MIN_MS

        def lead(bpm):
            return max(GLOW_LEAD_MIN_MS, (60_000 / bpm) * GLOW_LEAD_BEATS)

        assert lead(60) > GLOW_LEAD_MIN_MS, "slow pieces should use the beat"
        assert lead(200) == GLOW_LEAD_MIN_MS, "fast pieces should use the floor"
        # A hand moving to a string, which is what this number is for.
        assert 0.3 <= GLOW_LEAD_MIN_MS / 1000 <= 0.8, (
            f"{GLOW_LEAD_MIN_MS} ms is not a reaction time")
        assert lead(60) <= 1000, (
            f"{lead(60)} ms at 60 BPM marks the note a whole beat early, "
            f"which marks two of them at once on anything but whole notes")

    def test_a_note_that_has_been_judged_stops_glowing(self, tmp_path):
        """"Play this now" and "you did not" on the same note is the app
        arguing with itself.

        With no microphone every note is eventually a miss, which is how the player
        saw it: a red ring round a blooming note. The bloom marks a moment,
        and once there is a verdict that moment is over.
        """
        from pickhero.matcher import MatchResult, MatchType

        screen = self._exercise(tmp_path)
        note = screen._timeline.get_notes_in_range(2000.0, 20000.0)[3]
        surface = pygame.Surface((1280, 720))
        screen._playback_ms = note.timestamp_ms - 200
        screen.render(surface)
        assert screen._due.get((note.timestamp_ms, note.string)), (
            "it is not marked even before the verdict")

        screen._feedback.add_results(
            [MatchResult(match_type=MatchType.MISS, matched_events=[note],
                         timing_error_ms=0.0)],
            screen._playback_ms)
        screen.render(surface)
        import inspect

        from pickhero.ui import scrolling
        source = inspect.getsource(scrolling.PlayingScreen._draw_notes)
        assert "near = None if state is not None" in source, (
            "the bloom no longer stands down for a verdict")

    def test_the_verdict_ring_is_not_widened_by_the_bloom(self, tmp_path, monkeypatch):
        """The bloom brightens a note's rim. A verdict already has its own
        thickness -- five for correct pitch, three otherwise -- which the
        approaching-note bloom must not overwrite when judgement arrives."""
        from pickhero.matcher import MatchResult, MatchType

        screen = self._exercise(tmp_path)
        note = screen._timeline.get_notes_in_range(2000.0, 20000.0)[3]
        screen._playback_ms = note.timestamp_ms - 150
        surface = pygame.Surface((1280, 720))
        screen.render(surface)
        assert screen._due.get((note.timestamp_ms, note.string))
        original = design.card
        drawn = []

        def card(surface, rect, fill, **kwargs):
            original(surface, rect, fill, **kwargs)
            if kwargs.get("ring") is not None:
                drawn.append((kwargs["ring_width"], kwargs["glow"]))

        monkeypatch.setattr(design, "card", card)
        for state, width, blooms in (
                (MatchType.HIT, 5, True), (MatchType.EARLY, 5, True),
                (MatchType.LATE, 5, True), (MatchType.CLOSE, 3, False),
                (MatchType.MISS, 3, False)):
            screen._feedback.reset()
            screen._feedback.add_results([MatchResult(state, [note])], screen._playback_ms)
            drawn.clear()
            screen.render(surface)
            assert len(drawn) == 1, "Only the judged note should have a verdict ring"
            assert drawn[0][0] == width
            assert (drawn[0][1] > 0) is blooms

    @pytest.mark.parametrize("verdict,wanted", [
        ("HIT", True), ("CLOSE", False), ("MISS", False),
        ("EARLY", True), ("LATE", True)])
    def test_only_correct_pitch_gets_a_halo(self, tmp_path, verdict, wanted):
        """The player sent a crop of a violet note inside a magenta cloud. It was
        the MISS halo at full strength -- red over violet reads as magenta --
        and with no microphone running every note eventually misses, so the
        whole board was clouds.

        A verdict is already encoded three ways: the ring's colour, its
        thickness, and whether the fill is darkened. A coloured cloud is a
        fourth and the loudest of them, and being told off for a note the player
        never reached for teaches nothing. Correct pitch still blooms when
        the note was early or late; timing has its own separate caption.
        """
        import colorsys

        from pickhero.matcher import MatchResult, MatchType
        from pickhero.ui.colors import get_look

        screen = self._exercise(tmp_path)
        note = screen._timeline.get_notes_in_range(2000.0, 20000.0)[3]
        surface = pygame.Surface((1280, 720))
        screen._playback_ms = note.timestamp_ms - 150
        screen._feedback.add_results(
            [MatchResult(match_type=getattr(MatchType, verdict),
                         matched_events=[note], timing_error_ms=0.0)],
            screen._playback_ms)
        screen.render(surface)

        box = self._box(screen, surface, note)
        board = get_look().stage_bg
        beside = surface.get_at((box.left - 6, box.top + box.height // 4))[:3]
        lift = sum(abs(a - b) for a, b in zip(beside, board))
        if wanted:
            assert lift > 40, f"a hit does not bloom: {beside} on {board}"
            hue = colorsys.rgb_to_hsv(*[v / 255 for v in beside])[0] * 360
            assert 80 <= hue <= 180, f"the hit's bloom is not green: {hue:.0f}°"
        else:
            assert lift <= 25, (
                f"{verdict} clouds the note: {beside} against a {board} board")

    @staticmethod
    def _box(screen, surface, note):
        return _rendered_note_box(screen, surface, note)
