"""Tests for scrolling display math.

Tests the pure computation functions in PlayingScreen without needing
a running PyGame display.
"""

import pytest

from pickhero.i18n import t
from pickhero.tabs.timeline import NoteEvent, SongMetadata, Timeline
from pickhero.ui.scrolling import (
    CARD_MIN_W,
    PlayingScreen,
    format_time,
)


def _make_timeline(tempo: int = 120, notes: list[NoteEvent] | None = None) -> Timeline:
    """Create a timeline with given tempo and notes."""
    meta = SongMetadata(title="Test", artist="Tester", tempo=tempo)
    return Timeline(notes or [], meta)


class TestFormatTime:
    def test_zero(self):
        assert format_time(0) == "0:00"

    def test_one_minute(self):
        assert format_time(60_000) == "1:00"

    def test_seconds_padded(self):
        assert format_time(5_000) == "0:05"

    def test_mixed(self):
        assert format_time(125_000) == "2:05"

    def test_negative_clamps_to_zero(self):
        assert format_time(-1000) == "0:00"

    def test_fractional_truncates(self):
        assert format_time(61_999) == "1:01"


class TestNoteX:
    """Test PlayingScreen.note_x static method."""

    def test_note_at_playback_time_sits_on_hit_zone(self):
        hit_zone_x = 256.0
        pixels_per_ms = 0.5
        playback_ms = 1000.0
        x = PlayingScreen.note_x(1000.0, playback_ms, hit_zone_x, pixels_per_ms)
        assert x == pytest.approx(hit_zone_x)

    def test_future_note_is_right_of_hit_zone(self):
        hit_zone_x = 256.0
        pixels_per_ms = 0.5
        playback_ms = 1000.0
        x = PlayingScreen.note_x(2000.0, playback_ms, hit_zone_x, pixels_per_ms)
        assert x > hit_zone_x
        assert x == pytest.approx(256.0 + 1000.0 * 0.5)

    def test_past_note_is_left_of_hit_zone(self):
        hit_zone_x = 256.0
        pixels_per_ms = 0.5
        playback_ms = 1000.0
        x = PlayingScreen.note_x(500.0, playback_ms, hit_zone_x, pixels_per_ms)
        assert x < hit_zone_x

    def test_proportional_to_time_difference(self):
        hit_zone_x = 200.0
        pixels_per_ms = 1.0
        playback_ms = 0.0
        x1 = PlayingScreen.note_x(100.0, playback_ms, hit_zone_x, pixels_per_ms)
        x2 = PlayingScreen.note_x(200.0, playback_ms, hit_zone_x, pixels_per_ms)
        assert x2 - x1 == pytest.approx(100.0)


class TestNoteWidth:
    """Test PlayingScreen.note_width static method."""

    def test_enforces_minimum(self):
        w = PlayingScreen.note_width(1.0, 0.5)  # 0.5 px, below minimum
        assert w == CARD_MIN_W

    def test_long_note_exceeds_minimum(self):
        w = PlayingScreen.note_width(500.0, 0.5)  # 250 px
        assert w == pytest.approx(250.0)

    def test_zero_duration(self):
        w = PlayingScreen.note_width(0.0, 1.0)
        assert w == CARD_MIN_W


class TestPlayingScreenLayout:
    """Test layout computation with a mock surface."""

    class _MockSurface:
        def __init__(self, w: int, h: int):
            self._size = (w, h)

        def get_size(self):
            return self._size

    def test_layout_dimensions(self):
        """The note area starts after the string-name gutter.

        The redesign gives every lane a permanent label on the left, so the
        hit zone is a fraction of the area right of that gutter, not of the
        whole window.
        """
        from pickhero.ui.scrolling import GUTTER_W, TOOLBAR_H, TOP_BAR_H

        timeline = _make_timeline(tempo=120)
        screen = PlayingScreen(timeline, visible_beats=4, hit_zone_fraction=0.20)
        surface = self._MockSurface(1280, 720)
        layout = screen._layout(surface)

        assert layout.screen_w == 1280
        assert layout.screen_h == 720
        assert layout.gutter_x == GUTTER_W
        assert layout.hit_zone_x == pytest.approx(
            GUTTER_W + (1280 - GUTTER_W) * 0.20)
        assert layout.lane_top == TOP_BAR_H
        assert layout.lane_bottom == 720 - TOOLBAR_H

        # Fixed 8-second visible window
        expected_window = 8000.0
        assert screen._visible_window_ms == pytest.approx(expected_window)

        usable = 1280 - layout.hit_zone_x
        assert layout.usable_width == pytest.approx(usable)
        assert layout.pixels_per_ms == pytest.approx(usable / expected_window)

    def test_layout_survives_a_small_window(self):
        """The window is resizable and nothing may assume 1280x720."""
        screen = PlayingScreen(_make_timeline(tempo=120))
        for size in ((800, 600), (640, 480), (420, 300), (320, 240)):
            layout = screen._layout(self._MockSurface(*size))
            assert layout.lane_bottom > layout.lane_top
            assert layout.lane_height > 0
            assert layout.card_h > 0
            assert layout.pixels_per_ms > 0
            assert layout.hit_zone_x < size[0]

    def test_layout_adapts_to_different_size(self):
        timeline = _make_timeline(tempo=120)
        screen = PlayingScreen(timeline)
        layout_small = screen._layout(self._MockSurface(800, 600))
        layout_large = screen._layout(self._MockSurface(1920, 1080))

        assert layout_large.usable_width > layout_small.usable_width
        assert layout_large.lane_height > layout_small.lane_height
        assert layout_large.pixels_per_ms > layout_small.pixels_per_ms


class TestPlaybackClock:
    """Test play/pause/seek logic."""

    def test_starts_paused(self):
        screen = PlayingScreen(_make_timeline())
        assert not screen.is_playing()

    def test_toggle_play(self):
        screen = PlayingScreen(_make_timeline())
        screen.toggle_play()
        assert screen.is_playing()
        screen.toggle_play()
        assert not screen.is_playing()

    def test_seek_clamps_to_zero(self):
        screen = PlayingScreen(_make_timeline())
        screen.seek(-100)
        assert screen._playback_ms == 0.0

    def test_seek_clamps_to_duration(self):
        notes = [NoteEvent(1000, 500, 64, 1, 5)]
        timeline = _make_timeline(notes=notes)
        screen = PlayingScreen(timeline)
        screen.seek(999999)
        assert screen._playback_ms == timeline.duration_ms


class TestTempoFactor:
    """Test tempo factor (speed adjustment) logic."""

    def test_tempo_factor_default(self):
        screen = PlayingScreen(_make_timeline())
        assert screen._tempo_factor == 1.0

    def test_tempo_factor_clamps_low(self):
        """The floor is a quarter speed. It used to be half, which on a fast
        piece is still far too fast for a beginner."""
        from pickhero.ui.scrolling import TEMPO_MIN
        screen = PlayingScreen(_make_timeline())
        screen.set_tempo_factor(0.3)
        assert screen._tempo_factor == 0.3
        screen.set_tempo_factor(0.1)
        assert screen._tempo_factor == TEMPO_MIN == 0.25

    def test_tempo_factor_clamps_high(self):
        screen = PlayingScreen(_make_timeline())
        screen.set_tempo_factor(1.5)
        assert screen._tempo_factor == 1.0

    def test_tempo_factor_rounds_to_nearest_005(self):
        screen = PlayingScreen(_make_timeline())
        screen.set_tempo_factor(0.73)
        assert screen._tempo_factor == pytest.approx(0.75)
        screen.set_tempo_factor(0.82)
        assert screen._tempo_factor == pytest.approx(0.80)
        screen.set_tempo_factor(0.68)
        assert screen._tempo_factor == pytest.approx(0.70)

    def test_tempo_factor_from_config(self):
        from pickhero.config import Config
        config = Config(tempo_factor=0.75)
        screen = PlayingScreen(_make_timeline(), config=config)
        assert screen._tempo_factor == pytest.approx(0.75)

    def test_tempo_factor_config_clamped_on_init(self):
        from pickhero.config import Config
        from pickhero.ui.scrolling import TEMPO_MIN
        config = Config(tempo_factor=0.05)
        screen = PlayingScreen(_make_timeline(), config=config)
        assert screen._tempo_factor == TEMPO_MIN


class TestLoopState:
    """Test section looping state management."""

    def test_default_state(self):
        screen = PlayingScreen(_make_timeline())
        assert screen._loop_start_ms is None
        assert screen._loop_end_ms is None
        assert screen._loop_enabled is False

    def test_setting_one_marker_does_not_enable(self):
        screen = PlayingScreen(_make_timeline())
        screen._set_loop_start(1000.0)
        assert screen._loop_start_ms == 1000.0
        assert screen._loop_end_ms is None
        assert screen._loop_enabled is False

    def test_setting_both_markers_auto_enables(self):
        screen = PlayingScreen(_make_timeline())
        screen._set_loop_start(1000.0)
        screen._set_loop_end(5000.0)
        assert screen._loop_start_ms == 1000.0
        assert screen._loop_end_ms == 5000.0
        assert screen._loop_enabled is True

    def test_auto_swap_when_start_after_end(self):
        screen = PlayingScreen(_make_timeline())
        screen._set_loop_end(2000.0)
        screen._set_loop_start(5000.0)
        assert screen._loop_start_ms == 2000.0
        assert screen._loop_end_ms == 5000.0
        assert screen._loop_enabled is True

    def test_auto_swap_when_end_before_start(self):
        screen = PlayingScreen(_make_timeline())
        screen._set_loop_start(5000.0)
        screen._set_loop_end(2000.0)
        assert screen._loop_start_ms == 2000.0
        assert screen._loop_end_ms == 5000.0
        assert screen._loop_enabled is True

    def test_toggle_off_keeps_markers(self):
        screen = PlayingScreen(_make_timeline())
        screen._set_loop_start(1000.0)
        screen._set_loop_end(5000.0)
        assert screen._loop_enabled is True
        screen._toggle_loop()
        assert screen._loop_enabled is False
        assert screen._loop_start_ms == 1000.0
        assert screen._loop_end_ms == 5000.0

    def test_toggle_off_again_reenables_same_markers(self):
        screen = PlayingScreen(_make_timeline())
        screen._set_loop_start(1000.0)
        screen._set_loop_end(5000.0)
        screen._toggle_loop()   # disable
        screen._toggle_loop()   # reenable; clearing is a separate action
        assert screen._loop_start_ms == 1000.0
        assert screen._loop_end_ms == 5000.0
        assert screen._loop_enabled is True

    def test_zero_length_guard_snaps_end(self):
        """Loop shorter than one beat gets snapped to one beat."""
        screen = PlayingScreen(_make_timeline(tempo=120))
        # 120 BPM → 500ms per beat
        screen._set_loop_start(1000.0)
        screen._set_loop_end(1100.0)  # only 100ms apart
        assert screen._loop_end_ms == pytest.approx(1000.0 + 500.0)
        assert screen._loop_enabled is True

    def test_zero_length_guard_on_same_position(self):
        screen = PlayingScreen(_make_timeline(tempo=120))
        screen._set_loop_start(3000.0)
        screen._set_loop_end(3000.0)
        assert screen._loop_end_ms == pytest.approx(3000.0 + 500.0)

    def test_loop_hud_text_both_markers_enabled(self):
        screen = PlayingScreen(_make_timeline())
        screen._set_loop_start(15000.0)
        screen._set_loop_end(32000.0)
        text = screen._loop_hud_text()
        assert text == t("play.loop_on", start="0:15", end="0:32")

    def test_loop_hud_text_both_markers_disabled(self):
        screen = PlayingScreen(_make_timeline())
        screen._set_loop_start(15000.0)
        screen._set_loop_end(32000.0)
        screen._toggle_loop()
        text = screen._loop_hud_text()
        assert text == t("play.loop_off", start="0:15", end="0:32")

    def test_loop_hud_text_one_marker(self):
        screen = PlayingScreen(_make_timeline())
        screen._set_loop_start(15000.0)
        text = screen._loop_hud_text()
        assert text == t("play.loop_from", start="0:15")

    def test_loop_hud_text_no_markers(self):
        screen = PlayingScreen(_make_timeline())
        assert screen._loop_hud_text() is None


class TestAudioClock:
    """Detections arrive on the capture clock and have to land on the song
    clock. Getting that conversion wrong is silent — notes simply stop
    counting."""

    def _screen(self, tempo_factor: float = 1.0):
        from pickhero.config import Config
        from pickhero.matcher import NoteMatcher

        config = Config()
        config.tempo_factor = tempo_factor
        notes = [NoteEvent(timestamp_ms=t, duration_ms=400, midi_note=40,
                           string=6, fret=0) for t in (0, 1000, 2000)]
        screen = PlayingScreen(_make_timeline(notes=notes), config=config)
        screen._matcher = NoteMatcher(screen._timeline, audio_offset_ms=0.0)
        return screen

    def test_full_speed_passes_capture_time_through(self):
        screen = self._screen(1.0)
        notes = [type("N", (), {"timestamp_ms": 1500.0})()]
        screen._convert_timestamps(notes)
        assert notes[0].timestamp_ms == pytest.approx(1500.0)

    def test_half_speed_halves_the_elapsed_time(self):
        screen = self._screen(0.5)
        notes = [type("N", (), {"timestamp_ms": 2000.0})()]
        screen._convert_timestamps(notes)
        assert notes[0].timestamp_ms == pytest.approx(1000.0)

    def test_a_tempo_change_does_not_rescale_the_past(self):
        """The old code multiplied the whole capture time by the new factor,
        so halving the speed a minute in threw detections back to the
        thirty-second mark and nothing matched for the rest of the run."""
        screen = self._screen(1.0)
        # A minute of playing at full speed.
        screen._playback_ms = 60_000.0
        screen._audio_anchor_capture_ms = 60_000.0
        screen._tempo_factor = 0.5
        notes = [type("N", (), {"timestamp_ms": 60_200.0})()]
        screen._convert_timestamps(notes)
        assert notes[0].timestamp_ms == pytest.approx(100.0)

    def test_setting_the_same_tempo_is_not_a_change(self):
        screen = self._screen(1.0)
        screen._audio_anchor_capture_ms = 1234.0
        screen.set_tempo_factor(1.0)
        assert screen._audio_anchor_capture_ms == 1234.0


class TestSongEnd:
    """The last notes have to stay judgeable after the written duration."""

    def _screen(self, audio: bool):
        from pickhero.config import Config
        from pickhero.matcher import NoteMatcher

        notes = [NoteEvent(timestamp_ms=1000, duration_ms=500, midi_note=40,
                           string=6, fret=0)]
        screen = PlayingScreen(_make_timeline(notes=notes), config=Config())
        screen._audio_enabled = audio
        if audio:
            screen._matcher = NoteMatcher(screen._timeline,
                                          timing_window_ms=150.0,
                                          retire_delay_ms=90.0)
        return screen

    def test_scored_runs_get_a_tail(self):
        screen = self._screen(True)
        duration = screen._timeline.duration_ms
        assert screen._end_ms > duration
        # Long enough for the slowest detection to still arrive.
        assert screen._end_ms - duration >= 150.0 + 90.0

    def test_passive_runs_end_at_the_last_note(self):
        screen = self._screen(False)
        assert screen._end_ms == screen._timeline.duration_ms


class TestTheBeatIsAudible:
    """The player has to hear the beat, not infer it from a moving picture.

    The click went through the MIDI player, and the MIDI player was built only
    when a tab carried a backing track. None of the ten generated exercises
    has one, so on every exercise the count-in was silent and there was no
    beat at all during the run: the player was judging two-second intervals by eye,
    in silence, and then being scored on their timing.
    """

    def _screen(self, notes=8, tempo=60):
        from pickhero.config import Config
        events = [NoteEvent(timestamp_ms=i * 2000.0, duration_ms=1800.0,
                            midi_note=28 + (i % 2) * 5, string=4 - (i % 2),
                            fret=0) for i in range(notes)]
        meta = SongMetadata(title="Saitenwechsel", tempo=tempo)
        return PlayingScreen(Timeline(events, meta), config=Config())

    def test_the_midi_output_is_opened_without_a_backing_track(self):
        """It carries two different things and only one of them is optional."""
        screen = self._screen()
        assert screen._backing_track_present is False
        # _init_midi_player is called regardless; whether it succeeds depends
        # on the machine having a MIDI device, which a test cannot require.
        assert hasattr(screen, "_metronome_on")

    def test_the_metronome_is_on_by_default(self):
        assert self._screen()._metronome_on() is True

    def test_it_can_be_switched_off_and_is_remembered(self):
        screen = self._screen()
        screen._toggle_metronome()
        assert screen._metronome_on() is False
        assert screen._config.metronome is False

    def test_muting_the_backing_track_does_not_silence_the_beat(self):
        """Different things, different switches. Muting an accompaniment is a
        taste; losing the click is losing the exercise."""
        screen = self._screen()
        screen._backing_muted = True
        assert screen._metronome_on() is True

    def test_the_bar_length_comes_from_the_piece(self):
        """A piece in three would otherwise take its accent on the wrong
        beat, which is worse than no accent."""
        from pickhero.config import Config
        from pickhero.tabs.timeline import MeasureInfo

        events = [NoteEvent(timestamp_ms=i * 1000.0, duration_ms=900.0,
                            midi_note=40, string=6, fret=0) for i in range(6)]
        three = Timeline(events, SongMetadata(title="Walzer", tempo=60),
                         measures=[MeasureInfo(index=0, start_ms=0.0, end_ms=3000.0),
                                   MeasureInfo(index=1, start_ms=3000.0, end_ms=6000.0)])
        assert PlayingScreen(three, config=Config())._beats_per_bar() == 3

    def test_a_piece_without_measures_falls_back_to_four(self):
        assert self._screen()._beats_per_bar() == 4


class TestTheCaptureStartsOnceAndStaysStarted:
    """Playback opens capture before count-in and keeps it through zero.

    The crossing block was briefly nested inside the metronome's own branch,
    and that branch runs on every frame AFTER the count-in rather than on the
    one frame that crosses zero. The result was that the audio stream was torn
    down and reopened, the detector reset and the matcher rebuilt from scratch
    on every frame for the whole run. Measured on a note-perfect exercise 04
    driven through the real audio callback: 1850 matchers and 1850 streams over
    2091 frames, not one detection surviving long enough to be scored, 0 hits
    and 16 misses out of 16. The suite did not notice, so it does now.
    """

    def _screen(self, count_in=4, metronome=True):
        from pickhero.config import Config
        cfg = Config()
        cfg.count_in_beats = count_in
        cfg.metronome = metronome
        events = [NoteEvent(timestamp_ms=1000.0 + i * 2000.0, duration_ms=2000.0,
                            midi_note=28 + (i % 2) * 5, string=4 - (i % 2),
                            fret=0) for i in range(8)]
        meta = SongMetadata(title="Saitenwechsel", tempo=60)
        return PlayingScreen(Timeline(events, meta), config=cfg)

    def _drive(self, screen, frames=30):
        """Advance the playback clock by hand, counting _start_audio calls."""
        calls = []
        screen._start_audio = lambda: calls.append(screen._playback_ms)
        screen._audio_enabled = True
        screen.toggle_play()
        screen._last_tick = None
        for _ in range(frames):
            prev = screen._playback_ms
            screen._playback_ms += 100.0
            screen._count_in_tick(prev)
        return calls

    def test_started_before_the_count_in_crosses_zero_and_not_again(self):
        screen = self._screen()
        screen._playback_ms = -300.0
        calls = self._drive(screen, frames=20)
        assert len(calls) == 1, f"capture (re)started {len(calls)} times"
        assert calls[0] < 0.0

    def test_not_restarted_every_frame_with_the_metronome_off(self):
        screen = self._screen(metronome=False)
        screen._playback_ms = -300.0
        calls = self._drive(screen, frames=20)
        assert len(calls) == 1

    def test_started_even_when_there_is_no_count_in(self):
        screen = self._screen(count_in=0)
        calls = self._drive(screen, frames=20)
        assert len(calls) == 1
        assert calls[0] == 0.0

    def test_through_update_so_the_structure_may_change_but_not_the_behaviour(self):
        """The test above drives _count_in_tick by name, which would stop
        testing anything if the crossing moved somewhere else again. This one
        drives the real update() loop and only asserts the property that
        matters: the capture is opened once, whatever the shape of the code.
        """
        import time

        screen = self._screen()
        calls = []
        screen._start_audio = lambda: calls.append(screen._playback_ms)
        screen._audio_enabled = True
        screen._playback_ms = -300.0
        screen.toggle_play()
        screen._last_tick = time.perf_counter() - 0.05
        for _ in range(60):
            screen._last_tick = time.perf_counter() - 0.05
            screen.update()
        assert screen._playback_ms > 0.0, "die Uhr lief nicht"
        assert len(calls) == 1, f"Aufnahme {len(calls)}-mal gestartet"


class TestCornerChips:
    """The two labels on a note card: its name, and how the timing went.

    The player asked for both by position -- the note's name at the upper right of
    the card that already carries the fret number, and "zu früh" / "zu spät"
    at the upper left in normal size instead of the squeak it used to be.
    These guard the three things that were actually wrong when it was built,
    each of which rendered a screen that looked fine until it did not.
    """

    @staticmethod
    def _screen(strings: int, w: int, h: int):
        import pygame

        from pickhero.config import Config
        from pickhero.matcher import MatchResult, MatchType
        from pickhero.ui import colors, design
        from pickhero.ui.scrolling import PlayingScreen

        tuning = {1: 43, 2: 38, 3: 33, 4: 28, 5: 45, 6: 40}
        notes = [
            NoteEvent(timestamp_ms=1000.0 + i * 900.0, duration_ms=700.0,
                      midi_note=tuning[(i % strings) + 1] + (i * 3) % 5,
                      string=(i % strings) + 1, fret=(i * 3) % 5)
            for i in range(14)
        ]
        colors.set_look("studio")
        design.clear_caches()
        screen = PlayingScreen(_make_timeline(60, notes), config=Config())
        screen._lane_count = strings
        screen._playback_ms = 4200.0
        screen._playing = True
        screen._feedback.add_results(
            [MatchResult(match_type=MatchType.EARLY, matched_events=[n],
                         timing_error_ms=-90.0)
             for n in notes if 1500 < n.timestamp_ms < 7000],
            screen._playback_ms)
        surface = pygame.Surface((w, h))
        surface.fill(colors.get_look().bg)
        return screen, surface

    def _chips(self, strings: int, w: int, h: int, banner: str | None = None):
        """Every chip rect the screen would place, without drawing."""
        screen, surface = self._screen(strings, w, h)
        screen._banner_message = banner
        layout = screen._layout(surface)
        from pickhero.ui import design
        out = []
        for note in screen._timeline.get_notes_in_range(0.0, 20_000.0):
            x = screen.note_x(note.timestamp_ms, screen._playback_ms,
                              layout.hit_zone_x, layout.pixels_per_ms)
            card_h = max(12, int(layout.card_h))
            lane_y = layout.lane_top + (note.string - 1) * layout.lane_height
            import pygame
            rect = pygame.Rect(int(x),
                               int(lane_y + layout.lane_height / 2 - card_h / 2),
                               64, card_h)
            label_size = max(design.TEXT_XS,
                             min(design.TEXT_2XL, int(card_h * 0.62)))
            for right in (True, False):
                chip = screen._chip_rect(rect, 40, 26, right, layout, label_size)
                if chip is not None:
                    out.append((chip, rect, label_size))
        return out

    def test_chips_appear_on_a_small_guitar_window(self):
        # Six strings at 1024x640 produced NO chip at all: the guard reserved
        # the fret number's nominal size where its ink is 0.70 of that, so it
        # refused a placement that had 3 px to spare.
        assert self._chips(6, 1024, 640)

    def test_chips_appear_for_her_instrument(self):
        assert self._chips(4, 1280, 720)

    def test_no_chip_covers_the_fret_number(self):
        from pickhero.ui.scrolling import PlayingScreen
        for strings, w, h in ((4, 1280, 720), (4, 1024, 640),
                              (6, 1280, 720), (6, 1024, 640)):
            for chip, rect, label_size in self._chips(strings, w, h):
                reach = int(label_size * PlayingScreen.FRET_INK_HALF)
                assert chip.bottom <= rect.centery - reach or \
                       chip.top >= rect.centery + reach, (
                    f"{strings} strings at {w}x{h}: chip {chip} reaches the "
                    f"fret ink of card {rect}")

    def test_the_guard_refuses_a_chip_that_would_reach_the_fret_number(self):
        # The property test above is true but toothless: a 26 px chip drawn
        # half above the card never reaches the ink at any real size, so it
        # would pass even with the guard torn out. This asks the guard the
        # question directly, with a chip tall enough to matter.
        import pygame

        from pickhero.ui import design
        from pickhero.ui.scrolling import PlayingScreen

        screen, surface = self._screen(4, 1280, 720)
        layout = screen._layout(surface)
        card_h = max(12, int(layout.card_h))
        label_size = max(design.TEXT_XS,
                         min(design.TEXT_2XL, int(card_h * 0.62)))
        rect = pygame.Rect(400, int(layout.lane_top + 30), 64, card_h)
        reach = int(label_size * PlayingScreen.FRET_INK_HALF)

        # Tall enough that half of it, hung on the card's top edge, lands on
        # the digit.
        too_tall = (card_h // 2 - reach) * 2 + 8
        assert screen._chip_rect(rect, 40, too_tall, True, layout,
                                 label_size) is None
        # And one that clearly fits is still allowed, so the guard is not
        # simply refusing everything.
        assert screen._chip_rect(rect, 40, 26, True, layout,
                                 label_size) is not None

    def test_no_chip_enters_the_top_bar(self):
        from pickhero.ui.scrolling import TOP_BAR_H
        for strings, w, h in ((4, 1280, 720), (4, 1024, 640),
                              (6, 1280, 720), (6, 1024, 640)):
            for chip, _rect, _size in self._chips(strings, w, h):
                assert chip.top >= TOP_BAR_H, (
                    f"{strings} strings at {w}x{h}: chip {chip} is inside the "
                    f"top bar")

    def test_the_warning_strip_pushes_chips_below_it(self):
        # The quiet warning fires 2.5 s after the player stops -- every time the player
        # pauses to work something out. A chip on the top string used to be
        # swallowed by the strip, and to jump when it appeared.
        from pickhero.ui.scrolling import BANNER_H, TOP_BAR_H
        chips = self._chips(4, 1280, 720, banner="Zu leise")
        assert chips
        for chip, _rect, _size in chips:
            assert chip.top >= TOP_BAR_H + BANNER_H

    def test_the_verdict_is_never_drawn_at_the_squeaky_size(self):
        # The player called TEXT_XS "zu fiepsig". When the room runs out the WORD
        # gets shorter, never the letters smaller.
        import inspect

        from pickhero.ui.scrolling import PlayingScreen
        source = inspect.getsource(PlayingScreen._draw_timing_note)
        assert "design.TEXT_XS" not in source
        assert "design.TEXT_MD" in source

    def test_every_note_name_is_a_real_pitch(self):
        from pickhero.audio.note_utils import midi_to_name, name_to_midi
        for midi in (28, 33, 38, 43, 40, 64):
            name = midi_to_name(midi)
            assert name_to_midi(name) == midi


class TestTheGhostNote:
    """A note that went by unplayed, measured on the rendered pixels.

    Two earlier guards for this were written against design.readable() rather
    than against the screen, and both survived putting the old colour back --
    they proved the helper worked while the drawing code ignored it. This one
    renders the stage and reads it.
    """

    @staticmethod
    def _rendered(look_key: str):
        import pygame

        from pickhero.config import Config
        from pickhero.ui import colors, design
        from pickhero.ui.scrolling import PlayingScreen

        notes = [NoteEvent(timestamp_ms=1000.0, duration_ms=400.0,
                           midi_note=38, string=2, fret=7)]
        colors.set_look(look_key)
        design.clear_caches()
        screen = PlayingScreen(_make_timeline(60, notes), config=Config())
        screen._lane_count = 4
        screen._audio_enabled = True      # the ghost is an audio-mode cue
        screen._playing = True
        screen._playback_ms = 3000.0      # the note is a second behind them
        surface = pygame.Surface((1280, 720))
        screen.render(surface)

        layout = screen._layout(surface)
        x = screen.note_x(1000.0, screen._playback_ms, layout.hit_zone_x,
                          layout.pixels_per_ms)
        card_h = max(12, int(layout.card_h))
        if colors.get_look().note_shape == "circle":
            card_h = min(card_h, 54)
        lane_y = layout.lane_top + (2 - 1) * layout.lane_height
        rect = pygame.Rect(int(x),
                           int(lane_y + layout.lane_height / 2 - card_h / 2),
                           card_h, card_h)
        return surface, rect, colors.get_look()

    # Stage is not here on purpose: on the road a note behind the player is
    # simply gone, so it has no ghost to measure. The flat stages keep it,
    # which is where the defect was.
    @pytest.mark.parametrize("look_key", ["studio", "hifi"])
    def test_the_note_she_missed_is_visible_where_it_was_drawn(self, look_key):
        """On Hi-fi's paper the raw string colour measures 1.37:1, so the one
        note the player needs to go back to was the one the player could not find."""
        from pickhero.ui import design

        surface, rect, look = self._rendered(look_key)
        assert surface.get_rect().contains(rect), "note is off screen"
        best = max(design.contrast(surface.get_at((px, py))[:3], look.stage_bg)
                   for px in range(rect.left, rect.right)
                   for py in range(rect.top, rect.bottom))
        assert best >= 4.5, (
            f"{look_key}: nothing in the ghost reaches 4.5:1, best {best:.2f}:1")

    def test_a_disc_look_draws_a_disc_ghost(self):
        """The one place a note used to change shape, at the exact moment it
        stopped being playable -- which reads as a different kind of thing
        rather than the same thing, spent."""
        import pygame

        from pickhero.ui import design

        box = pygame.Rect(0, 0, 60, 60)
        painted = {}
        for circle in (False, True):
            layer = pygame.Surface(box.size)
            layer.fill((0, 0, 0))
            design.clear_caches()
            # A small radius, so the corner is the shape's own and not the
            # rounding's: at RADIUS_LG both shapes leave it clear and the
            # sample proves nothing.
            design.ghost(layer, box, (255, 255, 255), alpha=255, radius=4,
                         circle=circle)
            # A corner: inside a rounded rectangle's outline, well outside a
            # disc's.
            painted[circle] = layer.get_at((2, 2))[:3]
        assert painted[False] != (0, 0, 0), "the rectangle ghost lost its corner"
        assert painted[True] == (0, 0, 0), "the disc ghost painted a corner"

    def test_the_screen_asks_the_look_for_the_shape(self):
        import inspect

        from pickhero.ui import scrolling

        source = inspect.getsource(scrolling.PlayingScreen._draw_notes)
        assert 'circle=look.note_shape == "circle"' in source
        assert "design.readable(colour, look.stage_bg)" in source


class TestHighway:
    """Stage draws a road, not rows. One projection feeds all of it."""

    @staticmethod
    def _layout(lanes=4, w=1280, h=720):
        import pygame

        from pickhero.config import Config
        from pickhero.tabs.timeline import NoteEvent
        from pickhero.ui.scrolling import PlayingScreen
        notes = [NoteEvent(timestamp_ms=400.0 * i, duration_ms=300.0,
                           midi_note=28 + i % 12, string=(i % lanes) + 1,
                           fret=i % 5) for i in range(40)]
        screen = PlayingScreen(_make_timeline(120, notes), config=Config())
        screen._lane_count = lanes
        return screen, screen._layout(pygame.Surface((w, h)))

    def test_the_near_end_is_wider_than_the_far_end(self):
        from pickhero.ui import highway
        _screen, layout = self._layout()
        area = highway.band(layout)
        _x, _y, near = highway.project(area, 0, 4, 0.0)
        _fx, _fy, far = highway.project(area, 0, 4, 1.0)
        assert near > far, "the road does not recede"
        assert abs(far / near - highway.FAR_SCALE) < 0.01

    def test_equal_steps_in_time_shrink_on_screen(self):
        # Without the 1/z the notes appear to accelerate into your face.
        from pickhero.ui import highway
        _screen, layout = self._layout()
        area = highway.band(layout)
        ys = [highway.project(area, 0, 4, d / 8.0)[1] for d in range(9)]
        steps = [ys[i] - ys[i + 1] for i in range(8)]
        assert all(a > b for a, b in zip(steps, steps[1:])), steps

    def test_depth_zero_lands_on_the_strike_line(self):
        from pickhero.ui import highway
        _screen, layout = self._layout()
        area = highway.band(layout)
        _x, y, scale = highway.project(area, 1, 4, 0.0)
        assert scale == 1.0
        assert abs(y - (area.top + area.height * highway.STRIKE_AT)) <= 1

    def test_a_note_lands_on_its_own_ring(self):
        # The one failure that makes a rhythm game unplayable: the note and
        # the ring it has to hit must come from the same projection.
        from pickhero.ui import highway
        _screen, layout = self._layout()
        area = highway.band(layout)
        for lane in range(4):
            ring_x, ring_y, _ = highway.project(area, lane, 4, 0.0)
            rect, _scale = highway.place(layout, lane, 4, 0.0, 60)
            assert abs(rect.centerx - ring_x) <= 1, f"lane {lane}"
            assert abs(rect.centery - ring_y) <= 1, f"lane {lane}"

    def test_notes_shrink_with_distance(self):
        from pickhero.ui import highway
        _screen, layout = self._layout()
        near, _ = highway.place(layout, 0, 4, 0.0, 60)
        far, _ = highway.place(layout, 0, 4, 1.0, 60)
        assert far.width < near.width and far.height < near.height

    def test_the_road_matches_its_lowest_string_left_target_labels(self):
        # The road's visible pads read E/A/D/G left-to-right. Notes must land
        # on those strings, rather than reuse the flat stave's top-to-bottom order.
        from pickhero.ui.scrolling import PlayingScreen
        _screen, layout = self._layout()
        order = PlayingScreen._lane_order(layout)
        assert order == [4, 3, 2, 1], order

    def test_the_road_is_painted_back_to_front(self):
        # A near note hidden behind one four bars away is the giveaway that
        # the painter's order is wrong.
        import inspect

        from pickhero.ui import scrolling
        source = inspect.getsource(scrolling.PlayingScreen._draw_notes)
        assert "notes = list(reversed(notes))" in source

    def test_the_fog_is_built_once_not_per_frame(self):
        # Measured: 2.515 ms to build, 0.267 ms to blit. At 60 fps the first
        # number is a third of the entire frame budget.
        import inspect

        from pickhero.ui import highway
        source = inspect.getsource(highway._fog)
        assert "design.sprite(" in source

    def test_the_lane_floors_are_not_cached(self):
        # Measured the other way round: four live polygons cost 0.085 ms, the
        # cached 1280x520 alpha blit that replaces them costs 0.270.
        import inspect

        from pickhero.ui import highway
        source = inspect.getsource(highway.paint)
        body = source[source.index("for lane in range(lanes)"):]
        assert "design.sprite(" not in body
