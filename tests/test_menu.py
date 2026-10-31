"""The browser's own logic: filtering, sorting, and what to play next.

This is the screen the player spends the most time on and it had no tests.
Everything here works on SongInfo objects directly, so no files are parsed
and no window is needed.
"""

import os
from datetime import datetime, timedelta, timezone

import pytest

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
import pygame  # noqa: E402

from pickhero.config import Config  # noqa: E402
from pickhero.library import SongInfo  # noqa: E402
from pickhero.progress import ProgressTracker  # noqa: E402
from pickhero.ui.menu import MenuScreen, PASSED_PERCENT  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def _pygame():
    pygame.init()
    yield
    pygame.quit()


def _song(stem, *, root, difficulty=3, title="", artist="", folder="",
          playable=True):
    path = (root / folder / f"{stem}.gp5") if folder else (root / f"{stem}.gp5")
    return SongInfo(path=str(path), title=title or stem, artist=artist,
                    difficulty=difficulty, notes=10, playable=playable)


def _menu(tmp_path, songs, progress=None):
    screen = MenuScreen(tmp_path, config=Config(), progress=progress)
    screen._songs = songs
    screen._apply_filter()
    return screen


class TestSearch:
    def test_the_title_is_searched(self, tmp_path):
        menu = _menu(tmp_path, [_song("a", title="Tonleiter", root=tmp_path),
                                _song("b", title="Achtel", root=tmp_path)])
        menu._search_text = "tonl"
        menu._apply_filter()
        assert [s.display_title for s in menu._rows] == ["Tonleiter"]

    def test_the_artist_is_searched(self, tmp_path):
        menu = _menu(tmp_path, [_song("a", artist="Uebung 1", root=tmp_path),
                                _song("b", artist="Anderer", root=tmp_path)])
        menu._search_text = "uebung"
        menu._apply_filter()
        assert len(menu._rows) == 1

    def test_the_folder_is_searched(self, tmp_path):
        menu = _menu(tmp_path, [_song("a", folder="Uebungen", root=tmp_path),
                                _song("b", root=tmp_path)])
        menu._search_text = "uebungen"
        menu._apply_filter()
        assert len(menu._rows) == 1

    def test_search_is_case_insensitive(self, tmp_path):
        menu = _menu(tmp_path, [_song("a", title="Tonleiter", root=tmp_path)])
        menu._search_text = "TONLEITER"
        menu._apply_filter()
        assert len(menu._rows) == 1

    def test_a_search_with_no_hits_leaves_an_empty_list(self, tmp_path):
        menu = _menu(tmp_path, [_song("a", root=tmp_path)])
        menu._search_text = "gibtsnicht"
        menu._apply_filter()
        assert menu._rows == []
        menu.render(pygame.Surface((1280, 720)))


class TestSorting:
    def test_by_difficulty_puts_the_easiest_first(self, tmp_path):
        menu = _menu(tmp_path, [_song("hard", difficulty=5, root=tmp_path),
                                _song("easy", difficulty=1, root=tmp_path)])
        menu._sort_mode = "difficulty"
        menu._sort_rows()
        assert [s.difficulty for s in menu._rows] == [1, 5]

    def test_by_name_is_alphabetical(self, tmp_path):
        menu = _menu(tmp_path, [_song("b", title="Beta", root=tmp_path),
                                _song("a", title="Alpha", root=tmp_path)])
        menu._sort_mode = "name_asc"
        menu._sort_rows()
        assert [s.display_title for s in menu._rows] == ["Alpha", "Beta"]

    def test_last_played_puts_the_most_recent_first(self, tmp_path):
        """The rank was reversed along with the timestamp, so every piece the
        player had never opened sat at the top of the one list whose purpose
        is showing what they were last working on."""
        progress = ProgressTracker()
        now = datetime.now(timezone.utc)
        for stem, when in (("alt", now - timedelta(days=3)), ("neu", now)):
            progress.record_result(stem, {"accuracy_percent": 50.0,
                                          "hits": 5, "total": 10})
            progress.get_best(stem).last_played = when.isoformat()
        menu = _menu(tmp_path, [_song("nie", root=tmp_path),
                                _song("alt", root=tmp_path),
                                _song("neu", root=tmp_path)], progress)
        menu._sort_mode = "last_played"
        menu._sort_rows()
        assert [s.file.stem for s in menu._rows] == ["neu", "alt", "nie"]


class TestSuggestion:
    def test_the_easiest_unplayed_piece_is_offered(self, tmp_path):
        menu = _menu(tmp_path, [_song("schwer", difficulty=4, root=tmp_path),
                                _song("leicht", difficulty=1, root=tmp_path)])
        assert menu.suggestion().stem == "leicht"

    def test_a_piece_already_played_well_is_skipped(self, tmp_path):
        progress = ProgressTracker()
        progress.record_result("leicht", {"accuracy_percent": PASSED_PERCENT + 5,
                                          "hits": 9, "total": 10})
        menu = _menu(tmp_path, [_song("leicht", difficulty=1, root=tmp_path),
                                _song("mittel", difficulty=2, root=tmp_path)],
                     progress)
        assert menu.suggestion().stem == "mittel"

    def test_when_everything_is_played_well_the_weakest_comes_back(self, tmp_path):
        progress = ProgressTracker()
        progress.record_result("a", {"accuracy_percent": 99.0, "hits": 1, "total": 1})
        progress.record_result("b", {"accuracy_percent": 90.0, "hits": 1, "total": 1})
        menu = _menu(tmp_path, [_song("a", difficulty=1, root=tmp_path),
                                _song("b", difficulty=1, root=tmp_path)], progress)
        assert menu.suggestion().stem == "b"

    def test_an_unreadable_file_is_never_suggested(self, tmp_path):
        menu = _menu(tmp_path, [_song("kaputt", difficulty=1, playable=False,
                                      root=tmp_path),
                                _song("heil", difficulty=3, root=tmp_path)])
        assert menu.suggestion().stem == "heil"

    def test_an_empty_library_suggests_nothing(self, tmp_path):
        assert _menu(tmp_path, []).suggestion() is None


class TestFilters:
    def test_a_level_filter_keeps_only_that_level(self, tmp_path):
        menu = _menu(tmp_path, [_song("a", difficulty=1, root=tmp_path),
                                _song("b", difficulty=4, root=tmp_path)])
        menu._set_filter(("level", 1))
        assert [s.difficulty for s in menu._rows] == [1]

    def test_a_folder_filter_keeps_only_that_folder(self, tmp_path):
        menu = _menu(tmp_path, [_song("a", folder="Uebungen", root=tmp_path),
                                _song("b", root=tmp_path)])
        menu._set_filter(("folder", "Uebungen"))
        assert len(menu._rows) == 1

    def test_the_progress_filter_separates_played_from_unplayed(self, tmp_path):
        progress = ProgressTracker()
        progress.record_result("gespielt", {"accuracy_percent": 95.0,
                                            "hits": 9, "total": 10})
        menu = _menu(tmp_path, [_song("gespielt", root=tmp_path),
                                _song("neu", root=tmp_path)], progress)
        menu._set_filter(("progress", "done"))
        assert [s.file.stem for s in menu._rows] == ["gespielt"]
        menu._set_filter(("progress", "new"))
        assert [s.file.stem for s in menu._rows] == ["neu"]

    def test_a_filter_that_matches_nothing_still_renders(self, tmp_path):
        menu = _menu(tmp_path, [_song("a", difficulty=1, root=tmp_path)])
        menu._set_filter(("level", 5))
        assert menu._rows == []
        assert menu._selected_path() is None
        menu.render(pygame.Surface((1280, 720)))


class TestRowFits:
    """What is drawn in a row must fit inside the row.

    There was no such guard. The diagnosis proved it by setting ROW_H to 80,
    then 56, then 40 in the live file: the suite stayed green all three
    times. That is why the screen could carry a 48 px difficulty column, a
    563 px blank corridor and seven of ten pieces without anything
    objecting.
    """

    @staticmethod
    def _boxes():
        """Every text box a row draws, as (name, top, bottom), measured."""
        from pickhero.ui import design
        from pickhero.ui.menu import ROW_H, MenuScreen as M
        card_h = ROW_H - M.ROW_GAP
        title_h = design.font(design.TEXT_MD, bold=True).size("Xg")[1]
        meta_h = design.font(design.TEXT_XS).size("Xg")[1]
        ordinal_h = design.font(design.TEXT_SM, mono=True).size("00")[1]
        return card_h, [
            ("Titel", M.TITLE_Y, M.TITLE_Y + title_h),
            ("Meta", M.META_Y, M.META_Y + meta_h),
            ("Prozent", M.TITLE_Y, M.TITLE_Y + title_h),
            ("Nummer", card_h // 2 - 9, card_h // 2 - 9 + ordinal_h),
        ]

    def test_every_text_box_fits_inside_the_card(self):
        card_h, boxes = self._boxes()
        for name, top, bottom in boxes:
            assert 0 <= top and bottom <= card_h, (
                f"{name} spans {top}..{bottom} in a {card_h} px card")

    def test_title_and_meta_do_not_overlap(self):
        _card_h, boxes = self._boxes()
        title = next(b for b in boxes if b[0] == "Titel")
        meta = next(b for b in boxes if b[0] == "Meta")
        assert title[2] <= meta[1], (
            f"title ends at {title[2]}, meta starts at {meta[1]}")

    def test_the_ordinal_does_not_touch_the_title(self):
        from pickhero.ui import design
        from pickhero.ui.menu import MenuScreen as M
        ordinal_right = design.SPACE_3 + M.ORDINAL_W
        assert ordinal_right < M.TEXT_X, (
            f"ordinal ends at {ordinal_right}, title begins at {M.TEXT_X}")

    @pytest.mark.parametrize("language", ["de", "en"])
    def test_the_difficulty_word_fits_its_column(self, language):
        """The right half is three columns with no separator between them.

        Nothing truncates, so a word one pixel too long for its column lands
        on top of the progress bar instead of being cut. The longest word is
        the one that decides the width, in both languages -- German is not
        always the longer of the two here, but it is the one the player reads.
        """
        from pickhero import i18n, library
        from pickhero.ui import design
        from pickhero.ui.menu import MenuScreen as M

        before = i18n.current_language()
        try:
            i18n.set_language(language)
            font = design.font(design.TEXT_XS)
            for level in range(1, 6):
                word = library.level_name(level)
                # dot (8 px wide) + 12 px gap, then the word, and it must
                # stop before the progress bar starts.
                assert 12 + font.size(word)[0] <= M.LEVEL_W, (
                    f"{word!r} is {font.size(word)[0]} px, column is "
                    f"{M.LEVEL_W - 12}")
        finally:
            i18n.set_language(before)

    @pytest.mark.parametrize("language", ["de", "en"])
    def test_the_percentage_does_not_land_on_the_bar(self, language):
        """100 % is the widest it ever gets, and it is right-aligned."""
        from pickhero import i18n
        from pickhero.ui import design
        from pickhero.ui.menu import MenuScreen as M

        before = i18n.current_language()
        try:
            i18n.set_language(language)
            text = i18n.t("menu.percent", percent="100")
            width = design.font(design.TEXT_MD, bold=True).size(text)[0]
            # The bar takes all of SCORE_W except SCORE_GAP, which is what
            # the percentage has to fit in.
            assert width <= M.SCORE_GAP, (
                f"{text!r} is {width} px of a {M.SCORE_GAP} px column")
        finally:
            i18n.set_language(before)

    def test_the_columns_leave_the_title_room_before_they_appear(self):
        """Both side columns are dropped on a narrow window rather than
        overlapping the title, and the wider one goes first."""
        from pickhero.ui.menu import MenuScreen as M
        import inspect

        source = inspect.getsource(M._draw_row)
        assert "if width >= 620:" in source, "level column has no width guard"
        assert "if width >= 520:" in source, "progress bar has no width guard"
        # At its own threshold each column still starts after the title.
        assert 620 - M.SCORE_W - M.LEVEL_W > M.TEXT_X
        assert 520 - M.SCORE_W > M.TEXT_X


class TestListOverflowIsVisible:
    """A clipped row exists if and only if there is more list.

    The player did not know the list went on, and the player was not being careless: the
    band floored to whole rows, so the bottom edge was clean and the screen
    looked finished while three pieces sat below it. Every scanline of the
    34 px dead band was background, and the right gutter carried no ink in
    any of 16 columns.
    """

    @staticmethod
    def _band(available, count):
        from pickhero.ui.menu import MenuScreen
        return MenuScreen.row_band(available, count)

    def test_the_invariant_holds_at_every_height(self):
        from pickhero.ui.menu import ROW_H
        for available in range(60, 1200, 7):
            for count in (1, 2, 5, 7, 10, 11, 40, 200):
                band = self._band(available, count)
                total = count * ROW_H
                clipped = total > band
                assert clipped == (total > band)
                if total > available:
                    # There is more list than room, so something must be cut.
                    assert band < total, (
                        f"{count} rows in {available} px: band {band} hides "
                        f"the overflow without showing a cut row")

    def test_an_overflowing_band_always_shows_a_readable_slice(self):
        from pickhero.ui.menu import ROW_H, ROW_PEEK
        for available in range(ROW_H * 2, 1200, 3):
            band = self._band(available, 200)
            assert band <= available
            assert band % ROW_H == ROW_PEEK % ROW_H or band == available, (
                f"band {band} in {available} px leaves a sliver of "
                f"{band % ROW_H} px, not {ROW_PEEK}")

    def test_her_ten_pieces_fit_her_window(self):
        from pickhero.ui import design
        from pickhero.ui.menu import FOOTER_H, HEADER_H, ROW_H
        available = 718 - FOOTER_H - design.SPACE_2 - HEADER_H - design.SPACE_3
        band = self._band(available, 10)
        assert 10 * ROW_H <= band, (
            f"ten pieces need {10 * ROW_H} px, the band offers {band}")

    def test_nothing_is_cut_when_everything_fits(self):
        from pickhero.ui.menu import ROW_H
        band = self._band(600, 5)
        assert band == 600 and 5 * ROW_H <= band
