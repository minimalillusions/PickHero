"""Note matching engine.

Compares detected audio notes against the tab timeline to produce
hit/close/miss feedback. No pygame dependency — pure logic.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

from pickhero.audio.note_utils import semitone_distance
from pickhero.audio.input import TimestampedNote
from pickhero.tabs.timeline import NoteEvent, Timeline


class MatchType(Enum):
    """What happened to a written note.

    Finding the right string and fret is one skill. Playing it in time is a
    different one, learned later and mostly by hearing a beat. The app used to
    collapse both into a single red: a note played correctly two hundred
    milliseconds early looked exactly like a note never played at all, and the
    player was told nothing about which it was.

    So the right pitch at the wrong moment is its own verdict, with a
    direction. It is not a miss and it is not scored as one.
    """

    PENDING = "pending"
    HIT = "hit"          # right pitch, in time
    EARLY = "early"      # right pitch, played too soon
    LATE = "late"        # right pitch, played too late
    CLOSE = "close"      # a semitone out
    MISS = "miss"        # nothing heard where a note was written
    STRAY = "stray"      # something heard that belongs to no written note


# The verdicts that mean the player found the right note, whatever the timing.
ON_PITCH = (MatchType.HIT, MatchType.EARLY, MatchType.LATE)


@dataclass
class MatchResult:
    """Result of matching a detected note against the timeline."""
    match_type: MatchType
    matched_events: list[NoteEvent] = field(default_factory=list)
    semitone_distance: int | None = None
    # How far from the written time the player played it, in milliseconds. Negative
    # is early. None when nothing was heard at all.
    timing_error_ms: float | None = None
    # For a stray: what was heard and when, so the screen can say so.
    heard_midi: int | None = None
    heard_at_ms: float | None = None
    timing_uncertain: bool = False


# A detected pitch may sit an octave away from the written note because the
# detector locked onto a harmonic or a sub-harmonic. That is the detector's
# error, not the player's, so one octave is forgiven.
#
# Exactly one. The old rule folded the distance with a plain modulo, so a note
# three octaves away came out as a perfect hit: on a bass ukulele, playing the
# open E at 41 Hz scored green against an E written two octaves higher. And it
# was lopsided — thirteen semitones counted as close while eleven, the same
# distance on the other side of the octave, was thrown away.
OCTAVE_FOLD_LIMIT = 13


def _octave_forgiving(dist: int) -> int:
    """Semitone distance with a single octave folded out of it."""
    if dist > OCTAVE_FOLD_LIMIT:
        return dist
    return min(dist, abs(dist - 12))


class NoteMatcher:
    """Matches detected audio notes against tab timeline events.

    Each NoteEvent in the timeline is tracked by state (PENDING -> HIT/CLOSE/MISS).
    Detected notes are compared to PENDING events within the timing window.
    """

    def __init__(
        self,
        timeline: Timeline,
        timing_window_ms: float = 100.0,
        audio_offset_ms: float = 0.0,
        retire_delay_ms: float = 0.0,
        chord_threshold_ms: float = 50.0,
        note_filter: Callable[[NoteEvent], bool] | None = None,
        chord_partial_credit: bool = True,
    ):
        self._timeline = timeline
        self._timing_window_ms = timing_window_ms
        self._audio_offset_ms = audio_offset_ms
        # How long after a note is played its detection can still arrive. A
        # pitch detector cannot report a note until its analysis window has
        # filled with it, so a detection ALWAYS arrives later than the sound.
        # Without accounting for that, _mark_missed_notes retires the note as
        # a miss before its own detection turns up, and no amount of
        # back-dating the timestamp can rescue it: the note is already gone.
        # This is why a wider timing window appeared to help. It was hiding
        # premature retirement rather than tolerating real timing error.
        self._retire_delay_ms = retire_delay_ms
        # How far out a strike may land and still be read as an attempt at a
        # note rather than as noise. Wider than the timing window on purpose:
        # inside the window is in time, outside it is early or late, and
        # beyond this it was not that note at all. Half a second is about the
        # largest error a person makes while still meaning the same note.
        self._reach_ms = max(timing_window_ms, 500.0)
        # Everything before this has already been retired or matched.
        self._retired_upto_ms = 0.0
        self._assessment_start_ms = 0.0
        self._assessment_end_ms = math.inf
        self._chord_threshold_ms = chord_threshold_ms
        self.note_filter = note_filter
        self.chord_partial_credit = chord_partial_credit

        # State per note event, keyed by (timestamp_ms, string)
        self._note_states: dict[tuple[float, int], MatchType] = {}

        # Statistics
        self.hits = 0
        self.close = 0
        self.misses = 0
        # Of the notes the player found, how many were in time, and how the player was off
        # when the player was not. Kept apart from `hits` on purpose: one number for
        # both skills hides which of the two is the problem.
        self.in_time = 0
        self.early = 0
        self.late = 0
        self.timing_errors: list[float] = []
        self.timing_unknown = 0
        # Strikes that belonged to no written note. Not scored -- they are
        # reported so the player can see the app heard them.
        self.stray: list[tuple[float, int]] = []

        # Per-measure statistics: {measure_idx: {"hits": n, "close": n, "misses": n}}
        self._measure_stats: dict[int, dict[str, int]] = defaultdict(
            lambda: {"hits": 0, "close": 0, "misses": 0}
        )

    @property
    def audio_offset_ms(self) -> float:
        return self._audio_offset_ms

    def set_timing_window(self, milliseconds: float) -> None:
        """Update future timing verdicts without rewriting previous results."""
        value = float(milliseconds)
        if not math.isfinite(value) or value <= 0:
            raise ValueError("Timing window must be finite and positive")
        self._timing_window_ms = value
        self._reach_ms = max(value, 500.0)

    @audio_offset_ms.setter
    def audio_offset_ms(self, value: float) -> None:
        self._audio_offset_ms = value

    def _note_key(self, event: NoteEvent) -> tuple[float, int]:
        return (event.timestamp_ms, event.string)

    def _get_state(self, event: NoteEvent) -> MatchType:
        return self._note_states.get(self._note_key(event), MatchType.PENDING)

    def _set_state(self, event: NoteEvent, state: MatchType) -> None:
        self._note_states[self._note_key(event)] = state

    def _is_filtered(self, event: NoteEvent) -> bool:
        """Return True if this note should be excluded by the difficulty filter."""
        if not self._assessment_start_ms <= event.timestamp_ms < self._assessment_end_ms:
            return True
        if self.note_filter is None:
            return False
        return not self.note_filter(event)

    def get_note_state(self, event: NoteEvent) -> MatchType:
        """Get the current match state of a timeline note."""
        return self._get_state(event)

    def _find_chord_siblings(self, event: NoteEvent) -> list[NoteEvent]:
        """Find notes within chord_threshold_ms of the given event."""
        return [
            n for n in self._timeline.get_active_notes_at_time(
                event.timestamp_ms, self._chord_threshold_ms
            )
            if abs(n.timestamp_ms - event.timestamp_ms) <= self._chord_threshold_ms
        ]

    def _record_match(self, event: NoteEvent, match_type: MatchType,
                      error_ms: float | None = None, *, timing_uncertain: bool = False) -> None:
        """Record a match for a note, updating stats and measure stats."""
        self._set_state(event, match_type)
        if match_type in ON_PITCH:
            # The player found the note. That is counted whatever the timing, because
            # finding it is the skill being learned first.
            self.hits += 1
            self._measure_stats[event.measure]["hits"] += 1
            if timing_uncertain:
                self.timing_unknown += 1
            elif match_type is MatchType.HIT:
                self.in_time += 1
            elif match_type is MatchType.EARLY:
                self.early += 1
            else:
                self.late += 1
            if error_ms is not None and not timing_uncertain:
                self.timing_errors.append(error_ms)
        elif match_type == MatchType.CLOSE:
            self.close += 1
            self._measure_stats[event.measure]["close"] += 1
        elif match_type == MatchType.MISS:
            self.misses += 1
            self._measure_stats[event.measure]["misses"] += 1

    def has_pending_notes_at(self, playback_ms: float) -> bool:
        """Return True if there are unmatched notes at or before playback_ms."""
        window_start = playback_ms - self._timing_window_ms
        candidates = self._timeline.get_notes_in_range(window_start, playback_ms + 1)
        for note in candidates:
            if self._is_filtered(note):
                continue
            if self._get_state(note) == MatchType.PENDING:
                return True
        return False

    def first_pending_between(self, start_ms: float, end_ms: float) -> float | None:
        """Earliest pending onset crossed by a frame, including a slow frame."""
        for note in self._timeline.get_notes_in_range(
                max(self._assessment_start_ms, start_ms), end_ms + 0.001):
            if not self._is_filtered(note) and self._get_state(note) == MatchType.PENDING:
                return note.timestamp_ms
        return None

    @property
    def completion_grace_ms(self) -> float:
        """Time needed for the last late strike and its detection to arrive."""
        return self._reach_ms + self._retire_delay_ms + 1.0

    def set_retire_delay(self, delay_ms: float) -> None:
        """Update the detector allowance in song time after a speed change."""
        self._retire_delay_ms = max(0.0, delay_ms)

    def finish(self) -> list[MatchResult]:
        """Close an assessed interval; every included note receives a verdict."""
        results = []
        for note in self._timeline.get_notes_in_range(
                self._assessment_start_ms, self._assessment_end_ms):
            if not self._is_filtered(note) and self._get_state(note) == MatchType.PENDING:
                self._record_match(note, MatchType.MISS)
                results.append(MatchResult(MatchType.MISS, [note]))
        return results

    def _mark_missed_notes(self, playback_ms: float) -> list[MatchResult]:
        """Mark PENDING notes that have passed the timing window as MISS.

        Only the notes that have newly passed. This used to re-examine every
        note from the start of the song on every frame, so the work per frame
        grew with how far into the piece the player was: quadratic over a run,
        sixty times a second, on hardware chosen for being modest.
        """
        results = []
        # A note stays open as long as a strike could still be understood as
        # an attempt at it -- the whole reach, not just the timing window.
        # Retiring at the window meant a note played late was marked missed
        # before the strike for it arrived, so "too late" could never be
        # reported at all: it came out as red, which is the thing this is
        # supposed to stop saying.
        cutoff = playback_ms - self._reach_ms - self._retire_delay_ms
        if cutoff <= 0:
            return results

        candidates = self._timeline.get_notes_in_range(self._retired_upto_ms,
                                                       cutoff)
        self._retired_upto_ms = max(self._retired_upto_ms, cutoff)
        for note in candidates:
            if self._is_filtered(note):
                continue
            if self._get_state(note) == MatchType.PENDING:
                self._record_match(note, MatchType.MISS)
                results.append(MatchResult(
                    match_type=MatchType.MISS,
                    matched_events=[note],
                    semitone_distance=None,
                ))
        return results

    def process_detected_notes(
        self, detected: list[TimestampedNote], playback_ms: float
    ) -> list[MatchResult]:
        """Process detected notes against the timeline.

        Args:
            detected: Notes from AudioCapture.get_notes()
            playback_ms: Current playback position in the song

        Returns:
            List of match results for this frame.
        """
        results = []

        # Process each detected note with an onset
        for ts_note in detected:
            if not ts_note.note.is_onset:
                continue

            adjusted_ms = ts_note.timestamp_ms + self._audio_offset_ms
            detected_midi = ts_note.note.midi_note
            timing_uncertain = ts_note.note.timing_uncertain

            # Look well beyond the timing window. Whether the player found the note
            # and whether the player found it in time are two separate questions, and
            # the search has to be wide enough to ask the first one before the
            # second is decided. Searching only inside the timing window meant
            # a note played correctly but early matched nothing, produced no
            # feedback at all, and then turned red as though it had never been
            # played.
            candidates = self._timeline.get_notes_struck_near(
                adjusted_ms, self._reach_ms
            )

            # Filter to PENDING and non-filtered only
            pending = [
                n for n in candidates
                if self._get_state(n) == MatchType.PENDING and not self._is_filtered(n)
            ]
            if not pending:
                # Heard, but belonging to no note that is still open. It is
                # not a mistake and it is not scored, but it must not be
                # silence either: without this, playing a note a long way off
                # looks exactly like not playing at all, and the player cannot tell
                # whether the app is deaf or the player is early.
                self.stray.append((adjusted_ms, detected_midi))
                results.append(MatchResult(
                    match_type=MatchType.STRAY,
                    matched_events=[],
                    semitone_distance=None,
                    timing_error_ms=None,
                    heard_midi=detected_midi,
                    heard_at_ms=adjusted_ms,
                ))
                continue

            # The right note first, the nearest in time second. A strike
            # belongs to the note it sounds like, not to whichever note
            # happens to be closest on the clock.
            best = None
            best_rank = None
            for note in pending:
                dist = semitone_distance(detected_midi, note.midi_note)
                effective = _octave_forgiving(dist)
                rank = (effective, abs(adjusted_ms - note.timestamp_ms))
                if best_rank is None or rank < best_rank:
                    best = note
                    best_rank = rank

            if best is None or best_rank is None:
                continue
            best_dist = best_rank[0]
            error_ms = adjusted_ms - best.timestamp_ms

            # Classify: pitch first, then timing.
            if best_dist == 0:
                if timing_uncertain or abs(error_ms) <= self._timing_window_ms:
                    match_type = MatchType.HIT
                else:
                    match_type = (MatchType.EARLY if error_ms < 0
                                  else MatchType.LATE)
            elif best_dist == 1:
                match_type = MatchType.CLOSE
            else:
                # Too far off — ignore this detection, no penalty
                continue

            if timing_uncertain:
                # HIT still supplies green pitch feedback; it is deliberately
                # excluded from in_time, early, late and the residual median.
                error_ms = None

            # Chord handling
            siblings = self._find_chord_siblings(best)
            # Filter out excluded notes from siblings
            siblings = [s for s in siblings if not self._is_filtered(s)]

            if self.chord_partial_credit and len(siblings) > 1:
                # Partial credit mode: only mark the matched note
                matched_events = []
                if self._get_state(best) == MatchType.PENDING:
                    self._record_match(best, match_type, error_ms, timing_uncertain=timing_uncertain)
                    matched_events.append(best)

                # Check if majority of chord is now matched
                total_in_chord = len(siblings)
                needed = math.ceil(total_in_chord / 2)
                matched_count = sum(
                    1 for s in siblings
                    if self._get_state(s) in (*ON_PITCH, MatchType.CLOSE)
                )
                if matched_count >= needed:
                    # Auto-complete remaining pending notes
                    for s in siblings:
                        if self._get_state(s) == MatchType.PENDING:
                            self._record_match(s, match_type, error_ms, timing_uncertain=timing_uncertain)
                            matched_events.append(s)
            else:
                # Easy mode (old behavior): mark all chord siblings
                matched_events = []
                for sibling in siblings:
                    if self._get_state(sibling) == MatchType.PENDING:
                        # A chord is struck in one moment, so its notes share
                        # the one timing error.
                        self._record_match(sibling, match_type, error_ms, timing_uncertain=timing_uncertain)
                        matched_events.append(sibling)

                # Ensure the best note itself is included
                if best not in matched_events:
                    if self._get_state(best) == MatchType.PENDING:
                        self._record_match(best, match_type, error_ms, timing_uncertain=timing_uncertain)
                        matched_events.append(best)

            results.append(MatchResult(
                match_type=match_type,
                matched_events=matched_events,
                semitone_distance=best_dist,
                timing_error_ms=error_ms,
                timing_uncertain=timing_uncertain,
            ))

        # A busy render frame can leave valid, timestamped detections queued
        # beyond the retirement deadline. Consume those before closing notes.
        results.extend(self._mark_missed_notes(playback_ms))
        return results

    def get_statistics(self) -> dict:
        """Two scores, because they are two skills.

        `accuracy_percent` is how often the player found the right note. That is the
        one that counts as playing the piece, and a note found early or late
        counts in it -- the player found it.

        `timing_percent` is how many measured hits the player played in time, and it
        is reported separately so a good ear with a shaky pulse does not read
        as failure. `timing_median_ms` says which way the player is off, which is the
        part the player can actually act on: "you are 80 ms early" is advice, "62
        percent" is not. Ambiguous attack times are counted separately as
        `timing_unknown`; they contribute to pitch, never to the timing
        numerator, denominator or median.
        """
        total = self.hits + self.close + self.misses
        accuracy = (self.hits / total * 100) if total > 0 else 0.0
        measured = self.hits - self.timing_unknown
        timing = (self.in_time / measured * 100) if measured > 0 else 0.0
        # A proper median: with an even count, the upper of the two middle
        # values is not the middle. On six notes scattered either side of the
        # beat it reported an eighty millisecond bias that was not there, and
        # the app would have told them to correct a fault the player did not have.
        errors = sorted(self.timing_errors)
        if not errors:
            median = None
        elif len(errors) % 2:
            median = errors[len(errors) // 2]
        else:
            middle = len(errors) // 2
            median = (errors[middle - 1] + errors[middle]) / 2.0
        return {
            "hits": self.hits,
            "close": self.close,
            "misses": self.misses,
            "total": total,
            "accuracy_percent": accuracy,
            "in_time": self.in_time,
            "early": self.early,
            "late": self.late,
            "timing_percent": timing,
            "timing_median_ms": median,
            "timing_measured": measured,
            "timing_unknown": self.timing_unknown,
        }

    def get_weakest_sections(
        self, threshold: float = 0.6, min_length: int = 2
    ) -> list[tuple[int, int, float]]:
        """Find contiguous measures below accuracy threshold.

        Returns list of (start_measure, end_measure, accuracy) sorted by
        accuracy ascending. Only returns sections of at least min_length measures.
        """
        if not self._measure_stats:
            return []

        max_measure = max(self._measure_stats.keys())
        weak_runs: list[tuple[int, int, float]] = []
        run_start = None
        run_hits = 0
        run_total = 0

        for m in range(max_measure + 1):
            stats = self._measure_stats.get(m)
            if stats is None:
                # No notes in this measure — not weak, break any run
                if run_start is not None and (m - run_start) >= min_length:
                    acc = run_hits / run_total if run_total > 0 else 0.0
                    weak_runs.append((run_start, m - 1, acc * 100))
                run_start = None
                run_hits = 0
                run_total = 0
                continue

            total = stats["hits"] + stats["close"] + stats["misses"]
            if total == 0:
                if run_start is not None and (m - run_start) >= min_length:
                    acc = run_hits / run_total if run_total > 0 else 0.0
                    weak_runs.append((run_start, m - 1, acc * 100))
                run_start = None
                run_hits = 0
                run_total = 0
                continue

            acc = stats["hits"] / total
            if acc < threshold:
                if run_start is None:
                    run_start = m
                    run_hits = 0
                    run_total = 0
                run_hits += stats["hits"]
                run_total += total
            else:
                if run_start is not None and (m - run_start) >= min_length:
                    run_acc = run_hits / run_total if run_total > 0 else 0.0
                    weak_runs.append((run_start, m - 1, run_acc * 100))
                run_start = None
                run_hits = 0
                run_total = 0

        # Close any open run
        if run_start is not None and (max_measure + 1 - run_start) >= min_length:
            acc = run_hits / run_total if run_total > 0 else 0.0
            weak_runs.append((run_start, max_measure, acc * 100))

        # Sort by accuracy ascending (weakest first)
        weak_runs.sort(key=lambda x: x[2])
        return weak_runs

    def reset(self, start_ms: float = 0.0, end_ms: float | None = None) -> None:
        """Begin an assessed interval [start_ms, end_ms), excluding skipped notes."""
        self._assessment_start_ms = max(0.0, start_ms)
        self._assessment_end_ms = math.inf if end_ms is None else end_ms
        self._retired_upto_ms = self._assessment_start_ms
        self._note_states.clear()
        self.hits = 0
        self.close = 0
        self.misses = 0
        # Of the notes the player found, how many were in time, and how the player was off
        # when the player was not. Kept apart from `hits` on purpose: one number for
        # both skills hides which of the two is the problem.
        self.in_time = 0
        self.early = 0
        self.late = 0
        self.timing_errors: list[float] = []
        self.timing_unknown = 0
        # Strikes that belonged to no written note. Not scored -- they are
        # reported so the player can see the app heard them.
        self.stray: list[tuple[float, int]] = []
        self._measure_stats.clear()
