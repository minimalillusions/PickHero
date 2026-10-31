"""Tests for pickhero.matcher module."""

import pytest

from pickhero.audio.detector import DetectedNote
from pickhero.audio.input import TimestampedNote
from pickhero.matcher import MatchType, MatchResult, NoteMatcher
from pickhero.tabs.timeline import NoteEvent, SongMetadata, Timeline


def _note_event(timestamp_ms: float, midi_note: int = 64, string: int = 1,
                fret: int = 0, duration_ms: float = 500.0) -> NoteEvent:
    return NoteEvent(
        timestamp_ms=timestamp_ms,
        duration_ms=duration_ms,
        midi_note=midi_note,
        string=string,
        fret=fret,
    )


def _detected(midi_note: int, timestamp_ms: float, is_onset: bool = True,
              confidence: float = 0.95) -> TimestampedNote:
    return TimestampedNote(
        note=DetectedNote(
            midi_note=midi_note,
            frequency=440.0,  # placeholder
            confidence=confidence,
            name="A4",  # placeholder
            is_onset=is_onset,
        ),
        timestamp_ms=timestamp_ms,
    )


def _make_matcher(notes: list[NoteEvent], timing_window_ms: float = 100.0,
                  audio_offset_ms: float = 0.0,
                  chord_threshold_ms: float = 50.0) -> NoteMatcher:
    timeline = Timeline(notes, SongMetadata(title="Test", tempo=120))
    return NoteMatcher(
        timeline,
        timing_window_ms=timing_window_ms,
        audio_offset_ms=audio_offset_ms,
        chord_threshold_ms=chord_threshold_ms,
    )


class TestExactHit:
    def test_exact_midi_match(self):
        """Detected MIDI matches tab note exactly -> HIT."""
        tab_note = _note_event(1000.0, midi_note=64)
        matcher = _make_matcher([tab_note])

        detected = [_detected(64, 1000.0)]
        results = matcher.process_detected_notes(detected, 1050.0)

        hits = [r for r in results if r.match_type == MatchType.HIT]
        assert len(hits) == 1
        assert hits[0].semitone_distance == 0
        assert tab_note in hits[0].matched_events
        assert matcher.get_note_state(tab_note) == MatchType.HIT

    def test_exact_hit_updates_statistics(self):
        tab_note = _note_event(1000.0, midi_note=64)
        matcher = _make_matcher([tab_note])

        detected = [_detected(64, 1000.0)]
        matcher.process_detected_notes(detected, 1050.0)

        stats = matcher.get_statistics()
        assert stats["hits"] == 1
        assert stats["accuracy_percent"] == 100.0


class TestCloseMatch:
    def test_one_semitone_above(self):
        """±1 semitone -> CLOSE."""
        tab_note = _note_event(1000.0, midi_note=64)
        matcher = _make_matcher([tab_note])

        detected = [_detected(65, 1000.0)]  # +1 semitone
        results = matcher.process_detected_notes(detected, 1050.0)

        close = [r for r in results if r.match_type == MatchType.CLOSE]
        assert len(close) == 1
        assert close[0].semitone_distance == 1
        assert matcher.get_note_state(tab_note) == MatchType.CLOSE

    def test_one_semitone_below(self):
        tab_note = _note_event(1000.0, midi_note=64)
        matcher = _make_matcher([tab_note])

        detected = [_detected(63, 1000.0)]  # -1 semitone
        results = matcher.process_detected_notes(detected, 1050.0)

        close = [r for r in results if r.match_type == MatchType.CLOSE]
        assert len(close) == 1


class TestWrongNoteIgnored:
    def test_far_off_note_no_penalty(self):
        """>1 semitone away, no matching candidate -> no penalty."""
        tab_note = _note_event(1000.0, midi_note=64)
        matcher = _make_matcher([tab_note])

        detected = [_detected(70, 1000.0)]  # 6 semitones off
        results = matcher.process_detected_notes(detected, 1050.0)

        # No HIT or CLOSE results, note stays PENDING
        hit_close = [r for r in results if r.match_type in (MatchType.HIT, MatchType.CLOSE)]
        assert len(hit_close) == 0
        assert matcher.get_note_state(tab_note) == MatchType.PENDING


class TestMissedNote:
    def test_advance_past_window_without_detection(self):
        """Advance past window with no detection -> MISS."""
        tab_note = _note_event(1000.0, midi_note=64)
        matcher = _make_matcher([tab_note], timing_window_ms=100.0)

        # Past 1000 + the reach, which is how long a note stays open now:
        # retiring it at the timing window meant a late strike arrived to
        # find its note already written off as never played.
        results = matcher.process_detected_notes([], 1700.0)

        misses = [r for r in results if r.match_type == MatchType.MISS]
        assert len(misses) == 1
        assert matcher.get_note_state(tab_note) == MatchType.MISS

    def test_miss_updates_statistics(self):
        tab_note = _note_event(1000.0, midi_note=64)
        matcher = _make_matcher([tab_note], timing_window_ms=100.0)

        # Well past the reach: a note is not written off the moment its
        # timing window passes, because it can still be played late.
        matcher.process_detected_notes([], 1700.0)

        stats = matcher.get_statistics()
        assert stats["misses"] == 1
        assert stats["accuracy_percent"] == 0.0


class TestChordMatching:
    def test_match_one_note_of_chord_marks_all(self):
        """Match one note of a simultaneous pair -> both marked HIT."""
        note_a = _note_event(1000.0, midi_note=64, string=1)
        note_b = _note_event(1000.0, midi_note=59, string=2)
        matcher = _make_matcher([note_a, note_b], chord_threshold_ms=50.0)

        # Detect just one note of the chord
        detected = [_detected(64, 1000.0)]
        results = matcher.process_detected_notes(detected, 1050.0)

        hits = [r for r in results if r.match_type == MatchType.HIT]
        assert len(hits) == 1
        # Both notes should be marked
        assert matcher.get_note_state(note_a) == MatchType.HIT
        assert matcher.get_note_state(note_b) == MatchType.HIT

    def test_non_simultaneous_notes_not_grouped(self):
        """Notes far apart in time should not be grouped as chord."""
        note_a = _note_event(1000.0, midi_note=64, string=1)
        note_b = _note_event(2000.0, midi_note=59, string=2)
        matcher = _make_matcher([note_a, note_b], chord_threshold_ms=50.0)

        detected = [_detected(64, 1000.0)]
        matcher.process_detected_notes(detected, 1050.0)

        assert matcher.get_note_state(note_a) == MatchType.HIT
        assert matcher.get_note_state(note_b) == MatchType.PENDING


class TestOnsetOnly:
    def test_non_onset_detections_filtered(self):
        """is_onset=False detections are filtered out."""
        tab_note = _note_event(1000.0, midi_note=64)
        matcher = _make_matcher([tab_note])

        detected = [_detected(64, 1000.0, is_onset=False)]
        results = matcher.process_detected_notes(detected, 1050.0)

        hit_close = [r for r in results if r.match_type in (MatchType.HIT, MatchType.CLOSE)]
        assert len(hit_close) == 0
        assert matcher.get_note_state(tab_note) == MatchType.PENDING


class TestTimingWindow:
    def test_detection_at_edge_of_window_matches(self):
        """Note detected at edge of window still matches."""
        tab_note = _note_event(1000.0, midi_note=64, duration_ms=500.0)
        matcher = _make_matcher([tab_note], timing_window_ms=100.0)

        # Detect at 1099ms — just within the 100ms window of a note at 1000ms
        detected = [_detected(64, 1099.0)]
        results = matcher.process_detected_notes(detected, 1099.0)

        hits = [r for r in results if r.match_type == MatchType.HIT]
        assert len(hits) == 1

    def test_detection_outside_window_no_match(self):
        """Detection outside window -> no match."""
        tab_note = _note_event(1000.0, midi_note=64, duration_ms=100.0)
        matcher = _make_matcher([tab_note], timing_window_ms=50.0)

        # Detect at 1200ms — the note ended at 1100ms, window is 50ms
        # get_active_notes_at_time checks [1200-50, 1200+50] = [1150, 1250]
        # note range is [1000, 1100] which doesn't overlap
        detected = [_detected(64, 1200.0)]
        results = matcher.process_detected_notes(detected, 1200.0)

        hits = [r for r in results if r.match_type == MatchType.HIT]
        assert len(hits) == 0


class TestStatistics:
    def test_mixed_results(self):
        notes = [
            _note_event(1000.0, midi_note=64, string=1),
            _note_event(2000.0, midi_note=59, string=2),
            _note_event(3000.0, midi_note=55, string=3),
        ]
        matcher = _make_matcher(notes, timing_window_ms=100.0)

        # Hit first note
        matcher.process_detected_notes([_detected(64, 1000.0)], 1050.0)
        # Close on second note
        matcher.process_detected_notes([_detected(60, 2000.0)], 2050.0)
        # Miss third note, past the reach
        matcher.process_detected_notes([], 3700.0)

        stats = matcher.get_statistics()
        assert stats["hits"] == 1
        assert stats["close"] == 1
        assert stats["misses"] == 1
        assert stats["total"] == 3
        assert stats["accuracy_percent"] == pytest.approx(100 / 3)


class TestReset:
    def test_reset_clears_state(self):
        tab_note = _note_event(1000.0, midi_note=64)
        matcher = _make_matcher([tab_note])

        matcher.process_detected_notes([_detected(64, 1000.0)], 1050.0)
        assert matcher.get_note_state(tab_note) == MatchType.HIT

        matcher.reset()
        assert matcher.get_note_state(tab_note) == MatchType.PENDING
        stats = matcher.get_statistics()
        assert stats["hits"] == 0
        assert stats["close"] == 0
        assert stats["misses"] == 0


class TestNoDoubleMatch:
    def test_same_note_cannot_be_matched_twice(self):
        """Same tab note can't be matched twice."""
        tab_note = _note_event(1000.0, midi_note=64)
        matcher = _make_matcher([tab_note])

        # First detection -> HIT
        matcher.process_detected_notes([_detected(64, 1000.0)], 1050.0)
        assert matcher.hits == 1

        # Second detection of same pitch -> should not increment
        matcher.process_detected_notes([_detected(64, 1010.0)], 1060.0)
        assert matcher.hits == 1


class TestAudioOffset:
    def test_offset_shifts_detection_time(self):
        """audio_offset_ms shifts the detected timestamp to song time."""
        tab_note = _note_event(5000.0, midi_note=64, duration_ms=500.0)
        matcher = _make_matcher([tab_note], timing_window_ms=100.0,
                                audio_offset_ms=5000.0)

        # Detection at 0ms audio time + 5000ms offset = 5000ms song time
        detected = [_detected(64, 0.0)]
        results = matcher.process_detected_notes(detected, 5050.0)

        hits = [r for r in results if r.match_type == MatchType.HIT]
        assert len(hits) == 1


class TestOctaveForgiveness:
    """One octave is the detector's mistake to make. Three is the player's."""

    def test_an_octave_below_still_counts_as_a_hit(self):
        matcher = _make_matcher([_note_event(1000, midi_note=52)])
        results = matcher.process_detected_notes([_detected(40, 1000)], 1000)
        assert results[0].match_type == MatchType.HIT

    def test_two_octaves_away_is_not_a_hit(self):
        """A bass ukulele spans barely two octaves. Playing the open E against
        an E written two octaves up used to score a perfect green."""
        matcher = _make_matcher([_note_event(1000, midi_note=52)])
        results = matcher.process_detected_notes([_detected(28, 1000)], 1000)
        assert results == []
        assert matcher.get_statistics()["hits"] == 0

    def test_three_octaves_away_is_not_a_hit(self):
        matcher = _make_matcher([_note_event(1000, midi_note=64)])
        results = matcher.process_detected_notes([_detected(28, 1000)], 1000)
        assert results == []

    def test_the_octave_window_is_symmetric(self):
        """Eleven semitones and thirteen are the same distance from an octave.
        The old modulo counted thirteen as close and threw eleven away."""
        below = _make_matcher([_note_event(1000, midi_note=52)])
        above = _make_matcher([_note_event(1000, midi_note=52)])
        r_below = below.process_detected_notes([_detected(41, 1000)], 1000)
        r_above = above.process_detected_notes([_detected(65, 1000)], 1000)
        assert r_below[0].match_type == MatchType.CLOSE
        assert r_above[0].match_type == MatchType.CLOSE


class TestMissRetirement:
    """Retirement walks forward once, not over the whole song every frame."""

    def test_every_unplayed_note_is_still_missed(self):
        notes = [_note_event(t * 100.0) for t in range(1, 21)]
        matcher = _make_matcher(notes, timing_window_ms=50.0)
        for frame in range(0, 40):
            matcher.process_detected_notes([], frame * 100.0)
        assert matcher.get_statistics()["misses"] == 20

    def test_a_note_is_not_missed_twice(self):
        matcher = _make_matcher([_note_event(100.0)], timing_window_ms=50.0)
        results = []
        for frame in range(0, 20):
            results.extend(matcher.process_detected_notes([], frame * 100.0))
        assert len(results) == 1
        assert matcher.get_statistics()["misses"] == 1

    def test_a_reset_lets_the_same_notes_be_missed_again(self):
        notes = [_note_event(t * 100.0) for t in range(1, 6)]
        matcher = _make_matcher(notes, timing_window_ms=50.0)
        for frame in range(0, 12):
            matcher.process_detected_notes([], frame * 100.0)
        assert matcher.get_statistics()["misses"] == 5
        matcher.reset()
        for frame in range(0, 12):
            matcher.process_detected_notes([], frame * 100.0)
        assert matcher.get_statistics()["misses"] == 5


class TestFindingTheNoteIsItsOwnSkill:
    """Right pitch, wrong moment, is not a miss.

    The app used to collapse both into one red. A note played correctly two
    hundred milliseconds early looked exactly like a note never played at
    all, and the player was told nothing about which it was. Finding the
    string and the fret is the skill being learned first; playing it in time
    is a different one, learned later and mostly by hearing a beat.
    """

    def _play(self, offset_ms: float, midi: int = 64, window: float = 150.0):
        notes = [_note_event(2000.0, midi_note=64, duration_ms=1800.0)]
        matcher = _make_matcher(notes, timing_window_ms=window)
        results = matcher.process_detected_notes(
            [_detected(midi, 2000.0 + offset_ms)], 2000.0 + offset_ms)
        return matcher, results

    def test_in_time_is_a_hit(self):
        _, results = self._play(0.0)
        assert results[0].match_type == MatchType.HIT

    def test_the_edge_of_the_window_is_still_a_hit(self):
        for offset in (-150.0, 150.0):
            _, results = self._play(offset)
            assert results[0].match_type == MatchType.HIT, offset

    def test_too_early_says_so_instead_of_turning_red(self):
        _, results = self._play(-260.0)
        assert results[0].match_type == MatchType.EARLY
        assert results[0].timing_error_ms == pytest.approx(-260.0)

    def test_too_late_says_so_instead_of_turning_red(self):
        _, results = self._play(300.0)
        assert results[0].match_type == MatchType.LATE
        assert results[0].timing_error_ms == pytest.approx(300.0)

    def test_the_window_is_symmetric(self):
        """It was 150 ms early and 444 late, so anticipating -- which is what
        a beginner reading a scrolling display does -- was punished three
        times harder than dragging."""
        early, late = self._play(-400.0), self._play(400.0)
        assert early[1][0].match_type == MatchType.EARLY
        assert late[1][0].match_type == MatchType.LATE

    def test_far_enough_out_it_was_not_that_note(self):
        _, results = self._play(-900.0)
        assert all(r.match_type != MatchType.EARLY for r in results)

    def test_a_wrong_note_in_time_is_still_wrong(self):
        """Timing forgiveness is for the right note. A different note played
        perfectly on the beat is not a timing problem."""
        _, results = self._play(0.0, midi=70)
        assert all(r.match_type not in (MatchType.EARLY, MatchType.LATE)
                   for r in results)

    def test_a_mistimed_note_counts_toward_the_score(self):
        matcher, _ = self._play(-260.0)
        stats = matcher.get_statistics()
        assert stats["hits"] == 1
        assert stats["misses"] == 0
        assert stats["accuracy_percent"] == pytest.approx(100.0)

    def test_but_not_toward_the_timing_score(self):
        matcher, _ = self._play(-260.0)
        stats = matcher.get_statistics()
        assert stats["early"] == 1
        assert stats["in_time"] == 0
        assert stats["timing_percent"] == pytest.approx(0.0)

    def test_the_two_scores_are_reported_apart(self):
        """Four notes: two in time, one early, one not played. The player found
        three of four and was in time on two of those three."""
        notes = [_note_event(1000.0 * (i + 1), midi_note=64, duration_ms=400.0)
                 for i in range(4)]
        matcher = _make_matcher(notes, timing_window_ms=150.0)
        for offset, at in ((0.0, 1000.0), (0.0, 2000.0), (-280.0, 3000.0)):
            matcher.process_detected_notes(
                [_detected(64, at + offset)], at + offset)
        matcher.process_detected_notes([], 9000.0)
        stats = matcher.get_statistics()
        assert stats["hits"] == 3 and stats["misses"] == 1
        assert stats["in_time"] == 2 and stats["early"] == 1
        assert stats["accuracy_percent"] == pytest.approx(75.0)
        assert stats["timing_percent"] == pytest.approx(200.0 / 3.0)

    def test_the_median_says_which_way_she_is_off(self):
        """'You are 80 ms early' is advice. A percentage is not."""
        notes = [_note_event(1000.0 * (i + 1), midi_note=64, duration_ms=400.0)
                 for i in range(3)]
        matcher = _make_matcher(notes, timing_window_ms=150.0)
        for i, offset in enumerate((-80.0, -100.0, -60.0)):
            at = 1000.0 * (i + 1) + offset
            matcher.process_detected_notes([_detected(64, at)], at)
        assert matcher.get_statistics()["timing_median_ms"] == pytest.approx(-80.0)

    def test_nothing_played_has_no_timing_error(self):
        notes = [_note_event(1000.0, midi_note=64, duration_ms=400.0)]
        matcher = _make_matcher(notes, timing_window_ms=150.0)
        results = []
        for frame in range(200):
            results += matcher.process_detected_notes([], frame * 50.0)
        missed = [r for r in results if r.match_type == MatchType.MISS]
        assert missed and missed[0].timing_error_ms is None


class TestSomethingHeardThatFitsNothing:
    """A strike belonging to no written note must not look like silence.

    Played far enough off, a note used to produce nothing at all and then
    the written note turned red -- which is exactly what not playing looks
    like. The player could not tell whether the app was deaf or the player was early, and
    that is the difference between a fault the player can correct and one the player
    cannot.
    """

    def test_a_strike_far_from_any_note_is_reported(self):
        notes = [_note_event(2000.0, midi_note=64)]
        matcher = _make_matcher(notes, timing_window_ms=150.0)
        results = matcher.process_detected_notes([_detected(64, 900.0)], 900.0)
        assert [r.match_type for r in results] == [MatchType.STRAY]
        assert results[0].heard_midi == 64
        assert results[0].heard_at_ms == pytest.approx(900.0)

    def test_it_is_not_scored(self):
        """Not a mistake and not a hit. It is information, not a verdict."""
        notes = [_note_event(2000.0, midi_note=64)]
        matcher = _make_matcher(notes, timing_window_ms=150.0)
        matcher.process_detected_notes([_detected(64, 900.0)], 900.0)
        stats = matcher.get_statistics()
        assert stats["hits"] == 0 and stats["misses"] == 0 and stats["close"] == 0

    def test_it_does_not_consume_the_note_it_missed(self):
        """The note must still be playable afterwards."""
        notes = [_note_event(2000.0, midi_note=64)]
        matcher = _make_matcher(notes, timing_window_ms=150.0)
        matcher.process_detected_notes([_detected(64, 900.0)], 900.0)
        later = matcher.process_detected_notes([_detected(64, 2000.0)], 2000.0)
        assert later[0].match_type == MatchType.HIT

    def test_a_note_inside_the_reach_is_not_a_stray(self):
        notes = [_note_event(2000.0, midi_note=64)]
        matcher = _make_matcher(notes, timing_window_ms=150.0)
        results = matcher.process_detected_notes([_detected(64, 1700.0)], 1700.0)
        assert results[0].match_type == MatchType.EARLY

    def test_the_run_ends_with_a_record_of_them(self):
        notes = [_note_event(2000.0, midi_note=64)]
        matcher = _make_matcher(notes, timing_window_ms=150.0)
        matcher.process_detected_notes([_detected(59, 700.0)], 700.0)
        matcher.process_detected_notes([_detected(62, 800.0)], 800.0)
        assert [m for _, m in matcher.stray] == [59, 62]


class TestTheTimingSummary:
    """One sentence the player can act on, or nothing at all."""

    def _run(self, offsets, window=150.0):
        notes = [_note_event(1000.0 * (i + 1), midi_note=64, duration_ms=400.0)
                 for i in range(len(offsets))]
        matcher = _make_matcher(notes, timing_window_ms=window)
        for i, offset in enumerate(offsets):
            at = 1000.0 * (i + 1) + offset
            matcher.process_detected_notes([_detected(64, at)], at)
        return matcher.get_statistics()

    def test_a_consistent_bias_shows_in_the_median(self):
        stats = self._run([-80.0, -95.0, -70.0, -85.0, -75.0, -90.0])
        assert stats["timing_median_ms"] < -60.0
        assert stats["in_time"] == 6

    def test_scatter_without_bias_leaves_the_median_near_zero(self):
        stats = self._run([-90.0, 85.0, -70.0, 75.0, -80.0, 80.0])
        assert abs(stats["timing_median_ms"]) < 50.0

    def test_too_few_notes_to_say_anything(self):
        """Three notes is not a pulse. Telling them to correct a fault the player may
        not have is worse than telling them nothing."""
        stats = self._run([-80.0, -90.0, -70.0])
        assert stats["hits"] == 3
