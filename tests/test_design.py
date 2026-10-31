"""Tests for the visual system and the redesigned screens.

Rendering cannot be asserted pixel by pixel, but the rules that keep it
readable can be: the note fill must never carry the verdict, cards must not
overlap in a fast passage, and every screen must survive a small window.
"""

import os
import pathlib
import tempfile

import pytest

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
import pygame  # noqa: E402

from pickhero.ui import design  # noqa: E402
from pickhero.ui.colors import get_look  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def _display():
    pygame.init()
    pygame.display.set_mode((1280, 720))
    yield
    pygame.quit()


class TestFontLifetime:
    """The cache must not outlive the fonts it caches."""

    def test_rendering_after_a_pygame_restart_does_not_crash(self):
        """A cached font whose SDL_ttf handle was freed kills the process.

        pygame.quit() frees the fonts; the Python objects in the cache survive
        it pointing at released memory. Rendering with one is an access
        violation, not an exception, so nothing in the app can recover. This
        test would have taken the whole suite down before design.font() was
        tied to pygame's lifetime.
        """
        pygame.init()
        pygame.display.set_mode((320, 240))
        design.text(pygame.Surface((320, 240)), "vorher", (4, 4), (255, 255, 255))
        pygame.quit()

        pygame.init()
        pygame.display.set_mode((320, 240))
        rect = design.text(pygame.Surface((320, 240)), "nachher", (4, 4),
                           (255, 255, 255))
        assert rect.width > 0

    def test_font_survives_a_bare_font_module_shutdown(self):
        pygame.init()
        pygame.display.set_mode((320, 240))
        design.font(design.TEXT_MD)
        pygame.font.quit()
        assert design.font(design.TEXT_MD).size("x")[0] > 0


class TestPalette:
    def test_every_string_has_its_own_colour(self):
        colours = list(design.STRING_PALETTE.values())
        for i, a in enumerate(colours):
            for b in colours[i + 1:]:
                distance = sum(abs(x - y) for x, y in zip(a, b))
                assert distance > 60, f"{a} and {b} are too close"

    def test_there_are_no_verdict_constants_left_in_the_module(self):
        """A constant cannot know what page it will be drawn on.

        design.HIT and its three companions were tuned for a dark stage and
        measured 1.01:1 to 1.59:1 on Hi-fi's paper -- nine places where a
        number was drawn and could not be seen. They are gone rather than
        deprecated: the drawing code cannot reach for a colour that does not
        exist, and reaching for one was the defect. The verdicts live on the
        Look now, and tests/test_contrast.py measures them per look.
        """
        for name in ("HIT", "CLOSE", "MISS", "OFFBEAT"):
            assert not hasattr(design, name), (
                f"design.{name} is back; it cannot be right on three looks")

    def test_colour_is_never_the_only_cue_for_a_verdict(self, monkeypatch):
        """Equal verdict colours still leave different thickness and fill."""
        from dataclasses import replace
        from pickhero.config import Config
        from pickhero.matcher import MatchResult, MatchType
        from pickhero.tabs.timeline import NoteEvent, SongMetadata, Timeline
        from pickhero.ui import scrolling
        from pickhero.ui.colors import set_look

        previous = get_look().key
        set_look("studio")
        try:
            neutral = (180, 180, 180)
            look = replace(get_look(), feedback_hit=neutral, feedback_close=neutral,
                           feedback_miss=neutral, feedback_early=neutral)
            monkeypatch.setattr(scrolling, "get_look", lambda: look)
            monkeypatch.setattr(scrolling.PlayingScreen, "_init_midi_player",
                                lambda *args: None)
            tuning = {1: 43, 2: 38, 3: 33, 4: 28}
            note = NoteEvent(1000.0, 100.0, 33, 3, 0)
            screen = scrolling.PlayingScreen(
                Timeline([note], SongMetadata(tempo=60, tuning=tuning)),
                config=Config(instrument="bass_guitar"))
            screen._audio_enabled = True
            screen._playback_ms = note.timestamp_ms
            original = design.card
            drawn = []

            def card(surface, rect, fill, **kwargs):
                original(surface, rect, fill, **kwargs)
                if kwargs.get("ring") == neutral:
                    # Read the actual pixels before the fret digit overlays
                    # them. The straight edge avoids rounded-corner coverage.
                    rim = sum(tuple(surface.get_at((x, rect.centery))[:3]) == neutral
                              for x in range(rect.left - 10, rect.left))
                    drawn.append((kwargs["ring_width"], rim,
                                  tuple(surface.get_at(rect.center)[:3])))

            monkeypatch.setattr(design, "card", card)
            results = {}
            for state in (MatchType.HIT, MatchType.EARLY, MatchType.LATE,
                          MatchType.CLOSE, MatchType.MISS):
                screen._feedback.reset()
                screen._feedback.add_results([MatchResult(state, [note])], 1000.0)
                before = len(drawn)
                screen.render(pygame.Surface((1280, 720)))
                assert len(drawn) == before + 1
                results[state] = drawn[-1]

            close_width, close_rim, normal_fill = results[MatchType.CLOSE]
            assert close_width == 3 and close_rim > 0
            for state in (MatchType.HIT, MatchType.EARLY, MatchType.LATE):
                width, rim, fill = results[state]
                assert width == 5 and rim > close_rim
                assert fill == normal_fill
            miss_width, _, missed_fill = results[MatchType.MISS]
            assert miss_width == 3
            normal_contrast = sum(abs(a - b) for a, b in zip(normal_fill, look.stage_bg))
            missed_contrast = sum(abs(a - b) for a, b in zip(missed_fill, look.stage_bg))
            assert missed_contrast < normal_contrast
        finally:
            set_look(previous)

    def test_shade_and_mix_stay_in_gamut(self):
        for factor in (0.1, 0.5, 1.0, 1.4, 2.0):
            for channel in design.shade((200, 120, 60), factor):
                assert 0 <= channel <= 255
        for t in (0.0, 0.5, 1.0):
            for channel in design.mix((0, 0, 0), (255, 255, 255), t):
                assert 0 <= channel <= 255


class TestPrimitives:
    def test_card_clamps_a_runaway_glow(self):
        """A glow derived from an age goes wild when the clock jumps back."""
        surface = pygame.Surface((200, 120))
        rect = pygame.Rect(40, 30, 60, 40)
        ring = get_look().feedback_hit
        design.card(surface, rect, (120, 120, 200), ring=ring, glow=40.0)
        design.card(surface, rect, (120, 120, 200), ring=ring, glow=-5.0)

    def test_text_returns_its_rect_and_respects_alignment(self):
        surface = pygame.Surface((400, 80))
        left = design.text(surface, "abc", (10, 10), (255, 255, 255))
        right = design.text(surface, "abc", (390, 10), (255, 255, 255), align="right")
        centre = design.text(surface, "abc", (200, 40), (255, 255, 255), align="center")
        assert left.x == 10
        assert right.right == 390
        assert abs(centre.centerx - 200) <= 1

    def test_font_cache_returns_the_same_object(self):
        assert design.font(18, bold=True) is design.font(18, bold=True)
        assert design.font(18) is not design.font(19)


class TestPlayingScreenRedesign:
    def _screen(self, instrument_key="bass_ukulele"):
        from pathlib import Path
        from pickhero.config import Config
        from pickhero.tabs.loader import load_gp_file
        from pickhero.ui.scrolling import PlayingScreen

        config = Config()
        config.instrument = instrument_key
        timeline = load_gp_file(Path("tests/fixtures/Demo_v5.gp5"),
                                instrument=config.get_instrument())
        return PlayingScreen(timeline, config=config)

    @pytest.mark.parametrize("size", [(1280, 720), (1024, 640), (820, 540),
                                      (640, 480), (560, 400), (420, 320)])
    def test_renders_at_any_window_size(self, size):
        surface = pygame.Surface(size)
        for key in ("bass_ukulele", "guitar"):
            screen = self._screen(key)
            screen._playing = True
            for ms in (-1800.0, 0.0, 12000.0):
                screen._playback_ms = ms
                screen.render(surface)

    def test_cards_never_overlap_the_next_note_on_the_string(self):
        from pickhero.ui.scrolling import CARD_FLOOR_W

        screen = self._screen("guitar")
        surface = pygame.Surface((1280, 720))
        layout = screen._layout(surface)
        screen._playback_ms = 12000.0
        notes = screen._timeline.get_notes_in_range(11000.0, 20000.0)
        gaps = screen._gaps_to_next_note(notes)
        for note in notes:
            width = screen._card_width(
                note, screen.note_width(note.duration_ms, layout.pixels_per_ms),
                int(layout.card_h), gaps, layout)
            gap_ms = gaps[(note.timestamp_ms, note.string)]
            if gap_ms < 1e8:
                room = gap_ms * layout.pixels_per_ms
                assert width <= max(room, CARD_FLOOR_W) + 0.5
            assert width >= CARD_FLOOR_W

    def test_the_verdict_never_changes_the_fill(self):
        """The fill is the string's identity; the ring carries the judgement.

        Recolouring the card would remove the one cue telling the player which
        string to use, at the exact moment they are looking at it.
        """
        import inspect
        from pickhero.ui import scrolling

        source = inspect.getsource(scrolling.PlayingScreen._draw_notes)
        assert "ring =" in source
        # The only place the fill is darkened is the miss case, and it is
        # darkened TOWARDS THE STAGE rather than recoloured -- so it stays the
        # string's own hue on Studio and Stage, and the look's own note colour
        # on Hi-fi. Never the miss red.
        assert "fill = design.mix(base, look.stage_bg, 0.55)" in source
        assert "fill = design.MISS" not in source
        assert "fill = look.feedback_miss" not in source


class TestMenuSuggestion:
    def _menu(self, tmp_path, records):
        from pathlib import Path
        from pickhero.config import Config
        from pickhero.progress import ProgressTracker, SongRecord
        from pickhero.ui.menu import MenuScreen
        from pickhero.exercises import write_exercises
        from pickhero import instruments

        write_exercises(instruments.BASS_UKULELE, tmp_path)
        tracker = ProgressTracker()
        tracker._data = {
            k: SongRecord(best_accuracy=v, attempts=1) for k, v in records.items()
        }
        config = Config()
        config.instrument = "bass_ukulele"
        return MenuScreen(Path(tmp_path), config=config, progress=tracker)

    def _ordered_names(self, menu):
        """The order the suggestion walks: easiest first, then by title."""
        return [s.file.stem for s in sorted(
            menu._songs,
            key=lambda s: (s.difficulty, s.display_title.lower(),
                           s.file.stem.lower()))]

    def test_suggests_the_easiest_untouched_piece(self, tmp_path):
        menu = self._menu(tmp_path, {})
        names = self._ordered_names(menu)
        assert menu.suggestion() is not None
        assert menu.suggestion().stem == names[0]
        assert menu.suggestion().stem.startswith("01"), (
            "the graded set must start at exercise 1; if this fails the "
            "generated titles have lost their number and the order is wrong"
        )

    def test_skips_pieces_already_played_well(self, tmp_path):
        from pickhero.ui.menu import PASSED_PERCENT
        names = self._ordered_names(self._menu(tmp_path, {}))
        done = {name: PASSED_PERCENT + 5 for name in names[:3]}
        menu = self._menu(tmp_path, done)
        assert menu.suggestion().stem == names[3]

    def test_falls_back_to_the_weakest_when_everything_is_learned(self, tmp_path):
        names = self._ordered_names(self._menu(tmp_path, {}))
        scores = {name: 99.0 for name in names}
        scores[names[4]] = 88.0
        menu = self._menu(tmp_path, scores)
        assert menu.suggestion().stem == names[4]

    def test_renders_at_any_window_size(self, tmp_path):
        menu = self._menu(tmp_path, {})
        for size in ((1280, 720), (900, 600), (640, 480), (480, 360)):
            menu.render(pygame.Surface(size))


class TestCompletionOverlay:
    """The result screen is now the only place the accuracy appears.

    It used to be drawn from inside the HUD method; the redesign replaced that
    method and silently dropped the call, so a finished song showed nothing at
    all. With the running percentage removed from the top bar, that turned
    into losing the score entirely.
    """

    def _finished_screen(self, audio_on: bool):
        from pathlib import Path
        from pickhero.config import Config
        from pickhero.matcher import MatchType
        from pickhero.tabs.loader import load_gp_file
        from pickhero.ui.scrolling import PlayingScreen

        config = Config()
        config.instrument = "bass_ukulele"
        timeline = load_gp_file(Path("tests/fixtures/Demo_v5.gp5"),
                                instrument=config.get_instrument())
        screen = PlayingScreen(timeline, config=config)
        screen._audio_enabled = audio_on
        screen._song_completed = True
        screen._playback_ms = timeline.duration_ms + 100
        if screen._matcher is not None:
            screen._matcher.hits = 7
            screen._matcher.close = 2
            screen._matcher.misses = 1
        return screen

    def test_render_draws_it_when_the_song_is_over(self):
        screen = self._finished_screen(True)
        screen.render(pygame.Surface((800, 600)))
        assert screen._panel.kind == "result"
        assert screen._panel.hit["replay"].width > 0
        assert screen._panel.hit["menu"].width > 0

    @pytest.mark.parametrize("audio_on", [True, False])
    @pytest.mark.parametrize("size", [(1280, 720), (800, 600), (560, 420)])
    def test_it_renders_in_both_modes_and_any_size(self, audio_on, size):
        screen = self._finished_screen(audio_on)
        screen.render(pygame.Surface(size))


class TestBundledTypefaces:
    """The three looks are three typefaces, and that has to survive a build.

    Every look rendered in Segoe UI before these were bundled, because
    design.font() read a module-level stack and never asked the look. That is
    the single biggest reason the app did not look like the drawings, so it
    gets a guard rather than a comment.
    """

    def test_every_face_a_look_names_is_actually_there(self):
        from pickhero.ui.colors import _LOOKS

        for name, look in _LOOKS.items():
            for role, face in (("sans", look.face_sans), ("mono", look.face_mono)):
                assert face, f"{name} has no {role} face"
                assert face in design.BUNDLED, f"{name}: unknown face {face!r}"
                for weight, file in design.BUNDLED[face].items():
                    assert (design.FONT_DIR / file).is_file(), (
                        f"{name} {role} {weight}: {file} is not in the folder")

    def test_every_weight_the_drawings_use_can_be_reached(self):
        """The suffix game this replaced resolved "bold" to the semibold cut
        on Geist and to the medium cut on Hanken, so two of the four weights
        were unreachable and HankenGrotesk-Bold.ttf shipped undrawn."""
        from pickhero.ui.colors import _LOOKS, get_look, set_look

        before = get_look().key
        try:
            for name, look in _LOOKS.items():
                set_look(name)
                design.clear_caches()
                widths = {}
                for weight in ("light", "regular", "medium", "semibold", "bold"):
                    widths[weight] = design.font(40, weight=weight).size(
                        "Hamburgefonstiv")[0]
                assert widths["light"] <= widths["regular"] <= widths["bold"], (
                    f"{name}: weights do not get heavier: {widths}")
                assert widths["bold"] > widths["light"], (
                    f"{name}: light and bold render identically: {widths}")
        finally:
            set_look(before)
            design.clear_caches()

    def test_a_family_with_no_such_weight_still_renders(self):
        """Asking for a cut that does not exist must fall back, not vanish."""
        assert design.font(20, face="mono", weight="light").size("0")[0] > 0
        assert design.font(20, face="geist", weight="nonsense").size("0")[0] > 0

    def test_each_look_really_gets_a_different_face(self):
        """The failure this replaces was invisible: three looks, one font, and
        nothing anywhere said so."""
        from pickhero.ui.colors import _LOOKS, get_look, set_look

        before = get_look().key
        try:
            widths = {}
            for name in _LOOKS:
                set_look(name)
                design.clear_caches()
                widths[name] = design.font(design.TEXT_2XL, bold=True).size(
                    "PickHero Hmxg")[0]
            assert len(set(widths.values())) == len(widths), (
                f"two looks render identically: {widths}")
        finally:
            set_look(before)
            design.clear_caches()

    def test_the_licences_ship_with_the_files(self):
        """SIL OFL requires the licence to travel with the font. A build that
        drops it is not redistributable."""
        found = {p.name for p in design.FONT_DIR.glob("OFL-*.txt")}
        assert len(found) >= 4, f"only {found}"
        for name in found:
            text = (design.FONT_DIR / name).read_text(encoding="utf-8",
                                                      errors="replace")
            assert "SIL OPEN FONT LICENSE" in text.upper(), name

    def test_the_build_carries_the_folder(self):
        """PyInstaller unpacks data beside the exe; a face not listed in the
        spec is simply absent at runtime and every look falls back silently."""
        spec = pathlib.Path("pickhero.spec").read_text(encoding="utf-8")
        assert "pickhero/assets/fonts" in spec

    def test_a_frozen_build_looks_in_the_unpack_folder(self):
        """__file__ points inside the archive when frozen, so resolving from
        it finds nothing. sys._MEIPASS is the only correct answer there."""
        import sys

        marker = pathlib.Path(tempfile.gettempdir()) / "_pickhero_meipass_probe"
        setattr(sys, "_MEIPASS", str(marker))
        try:
            found = design._font_dir()
        finally:
            delattr(sys, "_MEIPASS")
        assert found == marker / "pickhero" / "assets" / "fonts", found
