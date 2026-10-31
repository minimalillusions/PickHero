"""Scrolling note display for the playing screen.

Renders 6 string lanes with notes scrolling right-to-left, synchronized
to a playback clock. Optionally captures audio and shows hit/miss feedback.
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass
from pathlib import Path

import pygame

from pickhero.audio.midi_playback import BackingTrack, MidiPlayer
from pickhero.audio.note_utils import freq_to_cents_deviation, midi_to_name
from pickhero.config import Config, log_error
from pickhero.i18n import t
from pickhero.instruments import with_tuning
from pickhero.matcher import MatchType, NoteMatcher, ON_PITCH
from pickhero.progress import ProgressTracker
from pickhero.tabs.timeline import NoteEvent, Timeline
from pickhero.ui import design, highway, paint_studio, shapes
from pickhero.ui.colors import (
    cycle_look,
    get_look,
)
from pickhero.ui.feedback import FeedbackRenderer, LANE_VERDICT_MS
from pickhero.ui.page import Page, painter
from pickhero.ui.practice_panels import Action, PracticePanel

# Layout constants
LANE_TOP_MARGIN = 80
LANE_BOTTOM_MARGIN = 40

# -- Redesigned playing view --
# Left column holding the string names, so a beginner can always tell which
# row is which string without opening the help overlay.
GUTTER_W = 76
TOP_BAR_H = 60
TOOLBAR_H = 78
# The warning strip under the top bar. The corner chips on the top string
# need this height to stay clear of it, so it is a constant rather than a
# literal repeated at the two places that care.
BANNER_H = 30
# The width a note card wants. It gives this up when the next note on the
# same string is closer than that, because overlapping cards in a fast
# passage are worse than small ones.
CARD_MIN_W = 34
# Below this a card cannot be drawn at all, so it is the hard floor.
CARD_FLOOR_W = 11
# Under this width the fret number no longer fits inside and goes above.
CARD_LABEL_INSIDE_W = 26
CARD_MAX_H = 84
# How long a judged note keeps its glow.
# The note's shadow. The RAMP's shape is the drawing's -- measured, a
# near-straight fall from the rim, which is what makes it read as a shadow
# rather than a dark rim. Everything else here was decided by looking at
# it, and none of it matches the drawing any more:
#
#   spread   0.74 of the radius reached 14 px, then 0.48 reached eight, and
#            0.34 reaches five. Asked for smaller twice.
#   drift    The drawing drops it straight down. A shadow with no sideways
#            offset has no light source -- the note reads as floating rather
#            than lying on the page. Down and to the LEFT puts the light
#            overhead and to the right, which is where a desk lamp is.
#   alpha    42 of 255 at the rim, then 30, now 21. Lighter twice.
SHADOW_DROP = 0.08
SHADOW_SPREAD = 0.34
SHADOW_DRIFT = -0.11         # negative is left
SHADOW_ALPHA = 21

# How long before a note the mark comes up. Most of a beat, so it rises while
# there is still time to move a finger -- and a floor for fast tempi, where
# half a beat is a blink. The tail is how long it stays after the moment has
# passed, on top of the timing window, so a note the player is still allowed to hit
# is still marked.
GLOW_LEAD_BEATS = 0.75
GLOW_LEAD_MIN_MS = 450.0
GLOW_TAIL_MS = 220.0

# Studio's active card has a precise detached outline, a close glow and a
# faint wider falloff. Both masks follow its rounded corners.
GLOW_SPREAD = 0.26
GLOW_ALPHA = 110
GLOW_OUTER_SPREAD = 0.65
GLOW_OUTER_ALPHA = 65
# The outline stands off the card by this much of its height, and is drawn in
# the note's OWN colour. Measured across the drawing's active note: fill,
# eight sheet pixels of bare board, three of the same cyan, then the bloom.
# A pale rim on the card -- which is what was here -- desaturates the one
# edge the string's identity lives in, and reads as a white outline.
HALO_GAP = 0.07
HALO_RING_W = 2

GLOW_MS = 420.0
# Seconds of silence during a passage of notes before the app says so.
QUIET_WARN_SECONDS = 2.5
MIN_NOTE_WIDTH_PX = 20
NOTE_HEIGHT_FRACTION = 0.7
NOTE_CORNER_RADIUS = 4

# Left margin for notes that already passed the hit zone (ms)
LEFT_MARGIN_MS = 2000
# Right margin for notes not yet visible (ms)
RIGHT_MARGIN_MS = 500

# Difficulty filter: fret limit cycle values
FRET_LIMITS = [24, 12, 7, 5, 3]

# Slowest practice speed, as a fraction of the written tempo.
TEMPO_MIN = 0.25

# Notes narrower than this are unreadable and nearly unhittable.
MIN_NOTE_WIDTH = 10
# Font size for a fret number that does not fit inside its note.
FRET_LABEL_SMALL = 18


def _get_font(name: str, size: int) -> pygame.font.Font:
    """A font, from the one cache the app has.

    This used to call pygame.font.SysFont on every call, and every call sits
    inside a draw method. The help overlay built four fonts per frame and cost
    12.5 ms of a 16.7 ms budget on a fast machine; on the low-end hardware
    this app is for, that is a dropped frame every frame it is open.
    """
    return design.font(size, mono=name.lower() in ("consolas", "courier new",
                                                   "monospace"))


def format_time(ms: float) -> str:
    """Format milliseconds as M:SS."""
    total_seconds = max(0, int(ms / 1000))
    minutes = total_seconds // 60
    seconds = total_seconds % 60
    return f"{minutes}:{seconds:02d}"


@dataclass
class _Layout:
    """Computed layout dimensions for current surface size."""

    screen_w: int
    screen_h: int
    lane_height: float
    note_h: float
    hit_zone_x: float
    usable_width: float
    pixels_per_ms: float
    visible_window_ms: float
    lane_count: int = 6
    gutter_x: float = 0.0
    lane_top: float = LANE_TOP_MARGIN
    lane_bottom: float = 0.0
    card_h: float = 0.0


class PlayingScreen:
    """Scrolling tab display with playback clock and optional audio matching."""

    def __init__(self, timeline: Timeline, visible_beats: int = 4,
                 hit_zone_fraction: float = 0.20, config: Config | None = None,
                 backing_track: BackingTrack | None = None,
                 progress_tracker: ProgressTracker | None = None,
                 song_key: str = "", song_path: str = "", track_index: int = 0,
                 lesson_practice=None):
        self._timeline = timeline
        self._visible_beats = visible_beats
        self._hit_zone_fraction = hit_zone_fraction
        self._config = config or Config()

        self._tempo_factor = max(TEMPO_MIN, min(1.0, self._config.tempo_factor))
        # Where the capture clock stood when the song clock was last
        # pinned to it. See _convert_timestamps.
        self._audio_anchor_capture_ms = 0.0
        self._audio_time_segments: list[tuple[float, float, float]] = []
        self._capture_preroll = False
        self._preroll_before_ms: float | None = None

        self._playback_ms: float = 0.0
        self._playing = False
        self._last_tick: float | None = None

        self._ms_per_beat = self._timeline.beat_duration_at(0.0)
        self._visible_window_ms = 8000.0
        preparation = getattr(timeline, "_playback_preparation", None)
        if isinstance(preparation, tuple) and len(preparation) == 2:
            self._note_neighbours, self._min_note_gap_ms = preparation
        else:
            self._note_neighbours = self._neighbour_times(self._timeline.notes)
            self._min_note_gap_ms = min(
                (abs(other - stamp) for (stamp, _), neighbours in self._note_neighbours.items()
                 for other in neighbours if other is not None), default=math.inf)

        # Count-in state
        count_in_beats = max(0, self._config.count_in_beats)
        self._count_in_ms = count_in_beats * self._ms_per_beat
        self._last_count_in_beat: int = -1
        self._count_in_target_ms = 0.0
        self._count_in_beat_ms = self._ms_per_beat

        # Audio matching
        self._audio_capture = None  # AudioCapture, created on demand
        self._matcher: NoteMatcher | None = None
        self._feedback = FeedbackRenderer()
        self._audio_enabled = True
        self._last_page: Page | None = None
        self._pointer: tuple[int, int] | None = None
        self._drag: tuple[tuple[int, int], float] | None = None
        _gate = self._config.audio.noise_gate_db
        if _gate is None:
            _gate = self._config.get_instrument().noise_gate_db
        self._noise_gate_db: float = _gate

        # Loop state
        self._loop_start_ms: float | None = None
        self._loop_end_ms: float | None = None
        self._loop_enabled: bool = False

        # Progress tracking
        self._progress_tracker = progress_tracker
        self._song_key = song_key
        self._song_path = str(Path(song_path).resolve()) if song_path else ""
        self._track_index = track_index
        self._last_session_save = 0.0
        self._resume_count_pending = False
        self._attempt_issues: list[str] = []
        self._attempt_uniform_tempo = True
        self._suggested_tempo: float | None = None
        self._section_advice = None
        self._shorter_passage = None
        self._attempt_uniform_wait = True
        self._attempt_uniform_input = True
        self._attempt_input_key = None
        self._last_tempo_offer_key = None
        self._song_completed = False
        self._is_new_best = False
        self._recommendations: list[str] = []
        self._assessment_start_ms = 0.0
        self._full_song_attempt = True
        self._attempt_tempo_factor = self._tempo_factor
        self._audio_disabled_at_ms: float | None = None

        # MIDI backing track
        self._midi_player: MidiPlayer | None = None
        self._backing_muted = not self._config.backing_track_enabled
        # The MIDI output carries two different things: the backing track, and
        # the metronome. It used to be opened only when a tab had a backing
        # track, and none of the ten generated exercises has one -- so on
        # every exercise the count-in was silent and there was no beat to play
        # against at all. The player was judging two-second intervals by eye, from a
        # scrolling picture, in silence. The click is not an accompaniment; it
        # is the thing that makes timing possible.
        self._backing_track_present = bool(backing_track and len(backing_track) > 0)
        self._init_midi_player(backing_track or BackingTrack([]))

        # How many string lanes to draw. The loaded track's own tuning wins,
        # because the file knows better than any profile; the instrument
        # profile is the fallback when a tab carries no tuning.
        selected_instrument = self._config.get_instrument()
        self._tuning: dict[int, int] = dict(
            getattr(self._timeline.metadata, "tuning", None) or selected_instrument.tuning
        )
        self._instrument = with_tuning(selected_instrument, self._tuning)
        from pickhero.curriculum import lesson_for_path, next_lesson_practice
        self._lesson = lesson_for_path(self._song_path) if self._song_path else None
        from pickhero.music import music_for_path
        self._music_piece = music_for_path(self._song_path) if self._song_path else None
        self._lesson_practice = lesson_practice
        self._next_lesson_practice = (next_lesson_practice(
            self._song_path, self._instrument, lesson_practice)
            if self._lesson or self._music_piece else None)
        if self._next_lesson_practice is not None and not self._next_lesson_practice.path.is_file():
            self._next_lesson_practice = None
        self._lane_count: int = len(self._tuning) or self._instrument.string_count

        # If the loaded track is not written for this instrument, say so. A
        # guitar tab on a bass ukulele scores zero on every note and gives the
        # player no clue why, which is the single most discouraging thing the
        # app can do to a beginner.
        self._tuning_warning: str | None = self._check_tuning_match()
        # Notes near the playhead, refreshed once per frame by render().
        self._upcoming: list = []
        # Where each toolbar control was last drawn, for the mouse.
        self._button_rects: dict[str, pygame.Rect] = {}
        # Which beat the metronome has already sounded.
        self._last_beat_played: int = -1

        # Difficulty filter
        self._max_fret: int = self._config.effective_max_fret()
        self._unplayable_count = sum(
            1 for note in self._timeline.notes if note.fret > self._max_fret
        )
        stored = list(self._config.active_strings)
        # Stored lists are whatever length a previous instrument had, so resize
        # rather than index past the end of a shorter one.
        self._active_strings: list[bool] = (
            stored[:self._lane_count]
            + [True] * max(0, self._lane_count - len(stored))
        )

        # Signal level meter
        self._signal_db: float = -120.0
        self._signal_db_smooth: float = -120.0

        # Tuner display
        self._tuner_freq: float = 0.0
        self._tuner_confidence: float = 0.0
        self._tuner_freq_smooth: float = 0.0
        self._tuner_displayed_note: int = -1
        self._tuner_note_stable_frames: int = 0

        # Chord partial credit mode
        self._chord_partial_credit: bool = self._config.chord_partial_credit

        # Help overlay
        self._show_help: bool = False
        self._panel: PracticePanel | None = None
        self._help_was_playing = False
        self._setup_suspended = False

        # When the input first went quiet during a passage of notes.
        self._quiet_since: float | None = None
        # Which warning the strip is showing, decided once per frame. The
        # corner chips on the top string have to know before the strip is
        # drawn, and _quiet_warning() advances a timer, so asking it twice
        # in one frame would be asking a different question the second time.
        self._banner_message: str | None = None

        # Wait mode
        self._wait_mode: bool = self._config.wait_mode
        self._wait_mode_frozen: bool = False
        self._timing_assessed = not self._wait_mode

        from pickhero.ui.test_recording import PracticeRecording
        self._test_recording = PracticeRecording(self)

    def _count_in_tick(self, prev_ms: float) -> None:
        """Keep one uninterrupted click pulse through count-in and song.

        Its own method because the shape of it matters and an indentation is
        not enough to hold that shape. The crossing block was briefly nested
        one level deeper, inside the metronome's branch, and that branch runs
        on every frame AFTER the count-in rather than on the one frame that
        crosses zero. The capture was then torn down and reopened, the
        detector reset and the matcher rebuilt from scratch sixty times a
        second for the whole run. Measured on a note-perfect exercise 04
        driven through the real audio callback: 1850 matchers and 1850 streams
        over 2091 frames, not one detection surviving long enough to be
        scored, 0 hits and 16 misses out of 16. The whole suite passed.
        """
        target = self._count_in_target_ms
        if prev_ms < target:
            # Count-in clicks, at beat boundaries.
            if (self._playback_ms < target and self._count_in_ms > 0
                    and self._midi_player is not None):
                beat_index = int(
                    (self._count_in_ms + self._playback_ms - target) / self._count_in_beat_ms)
                if beat_index > self._last_count_in_beat:
                    self._midi_player.play_click(100)
                    self._last_count_in_beat = beat_index

            # Crossed from negative to non-negative — the song starts.
            if self._playback_ms >= target:
                if self._midi_player is not None:
                    self._midi_player.seek(target)
            else:
                return

        # And the beat through the song itself, not only the count-in.
        # Counting them in and then going silent leaves the beat to be inferred
        # from a moving picture, which is the one thing a beginner cannot yet
        # do. The first beat of a bar is louder, so the bar is audible too.
        if self._midi_player is None or not self._metronome_on():
            return
        if (self._loop_enabled and self._loop_end_ms is not None
                and self._playback_ms >= self._loop_end_ms):
            return
        beats = self._timeline.beats_in_range(
            max(target, prev_ms), self._playback_ms + 0.001)
        # A stalled frame must not burst several historical clicks at once.
        if beats and beats[-1].timestamp_ms > self._last_beat_played:
            beat = beats[-1]
            self._last_beat_played = beat.timestamp_ms
            self._midi_player.play_click(96 if beat.accent else 62)

    def _detector_delay_ms(self) -> float:
        """How long after a note the detection can still arrive.

        Read from the live detector when there is one, so it stays correct if
        the profile or the buffer sizes change.
        """
        capture = getattr(self, "_audio_capture", None)
        if capture is not None:
            try:
                return float(capture.detector.expected_delay_ms)
            except Exception:
                pass
        from pickhero.audio.detector import PitchDetector
        try:
            return float(PitchDetector(instrument=self._instrument).expected_delay_ms)
        except Exception:
            return 0.0

    def _check_tuning_match(self) -> str | None:
        """Return a warning if the track's tuning does not fit the instrument."""
        selected = self._config.get_instrument()
        want = selected.tuning
        got = self._tuning
        if not got or got == want:
            return None
        if len(got) != len(want):
            return t("play.tuning_strings", track=len(got),
                     instrument=selected.name, count=len(want))
        gap = min(got.values()) - min(want.values())
        if abs(gap) >= 3:
            # Two whole sentences under two keys. "N semitones" + "higher"
            # cannot be spliced: English puts the adjective last, German puts
            # it in the middle.
            key = "play.tuning_higher" if gap > 0 else "play.tuning_lower"
            return t(key, count=abs(gap))
        return None

    def _note_passes_filter(self, note: NoteEvent) -> bool:
        """Check if a note passes the difficulty filter."""
        if note.fret > self._max_fret:
            return False
        idx = note.string - 1
        if 0 <= idx < len(self._active_strings) and not self._active_strings[idx]:
            return False
        return True

    def _is_filter_active(self) -> bool:
        """Check if any difficulty filter is active."""
        return (self._max_fret < self._instrument.max_fret
                or not all(self._active_strings))

    def _filter_omits_notes(self) -> bool:
        """Whether manual filters omit notes playable on this instrument."""
        return any(note.fret <= self._instrument.max_fret
                   and not self._note_passes_filter(note)
                   for note in self._timeline.notes)

    @property
    def _end_ms(self) -> float:
        """When the song is actually over.

        Not at the last note. A strike on it reaches the matcher 150-250 ms
        later, once the detector has heard enough of it to name it, and the
        run used to stop the instant the written duration elapsed. The final
        notes were therefore never judged: they stayed pending, and pending
        notes are not in the score at all, so the last bar of every song was
        unplayable for credit and never showed a hit.
        """
        if not self._audio_enabled or self._matcher is None:
            return self._timeline.duration_ms
        return self._timeline.duration_ms + self._matcher.completion_grace_ms

    def _begin_attempt(self, start_ms: float) -> None:
        """Start a new assessment while pause/resume keeps the current one."""
        if self._playback_ms < self._count_in_target_ms:
            start_ms = max(start_ms, self._count_in_target_ms)
        self._assessment_start_ms = max(0.0, start_ms)
        self._full_song_attempt = (
            not self._loop_enabled
            and not any(self._note_passes_filter(note) for note in
                        self._timeline.get_notes_in_range(0.0, self._assessment_start_ms))
            and not self._filter_omits_notes()
        )
        self._attempt_tempo_factor = self._tempo_factor
        self._attempt_uniform_tempo = True
        self._attempt_uniform_wait = True
        self._attempt_uniform_input = True
        self._attempt_input_key = self._input_key()
        self._attempt_issues = []
        self._song_completed = False
        self._timing_assessed = not self._wait_mode
        self._is_new_best = False
        self._weakest_sections = []
        self._recommendations = []
        self._release_wait()
        self._last_beat_played = -1
        if self._matcher is not None:
            self._matcher.reset(
                self._assessment_start_ms,
                self._loop_end_ms if self._loop_enabled else None,
            )
            self._refresh_preroll_boundary()
        self._feedback.reset()
        if self._audio_capture is not None:
            setter = getattr(self._audio_capture, "set_health_window", None)
            if callable(setter):
                setter(self._audio_capture.capture_elapsed_ms() + max(
                    0.0, (self._count_in_target_ms - self._playback_ms) / self._tempo_factor))

    def toggle_play(self) -> None:
        """Toggle play/pause. Restarts with count-in if at beginning or past end."""
        if self._resume_count_pending and not self._playing:
            self._resume_count_pending = False
            self._start_practice(self._playback_ms, force_count_in=True)
            return
        if (self._song_completed or self._playback_ms >= self._end_ms) and not self._playing:
            # Restart from beginning with count-in
            self._count_in_target_ms = 0.0
            self._count_in_beat_ms = self._ms_per_beat
            self._count_in_ms = max(0, self._config.count_in_beats) * self._ms_per_beat
            self._panel = None
            self._playback_ms = -self._count_in_ms if self._count_in_ms > 0 else 0.0
            self._last_count_in_beat = -1
            self._last_beat_played = -1
            self._begin_attempt(0.0)
        elif (self._playback_ms == 0.0 and self._count_in_target_ms == 0.0
              and not self._playing and self._count_in_ms > 0):
            # Starting from the very beginning — add count-in
            self._playback_ms = -self._count_in_ms
            self._last_count_in_beat = -1
            self._last_beat_played = -1
            self._song_completed = False
            self._is_new_best = False
            self._weakest_sections = []
            self._recommendations = []
        self._playing = not self._playing
        if self._playing:
            self._last_tick = time.perf_counter()
            # Open the device before the first count-in click. Opening it at
            # zero lengthens the last count-in beat and grades a player who
            # follows that established pulse early for the entire song.
            if self._audio_enabled:
                self._start_audio()
            if self._midi_player is not None:
                if self._playback_ms >= self._count_in_target_ms:
                    self._midi_player.seek(self._playback_ms)
        else:
            self._last_tick = None
            self._stop_audio(reason='pause')
            if self._midi_player is not None:
                self._midi_player.pause()
            self.save_practice_state()

    def seek(self, ms: float) -> None:
        """Seek to an absolute position in ms, clamped to [0, duration]."""
        self._count_in_target_ms = 0.0
        self._count_in_beat_ms = self._ms_per_beat
        self._count_in_ms = max(0, self._config.count_in_beats) * self._ms_per_beat
        self._playback_ms = max(0.0, min(ms, self._timeline.duration_ms))
        self._begin_attempt(self._playback_ms)
        self._last_tick = time.perf_counter() if self._playing else None
        if self._midi_player is not None:
            self._midi_player.seek(self._playback_ms)
        # Restart audio with new offset if active
        if self._audio_enabled and self._playing:
            self._stop_audio()
            self._start_audio()
        self.save_practice_state()

    def is_playing(self) -> bool:
        return self._playing

    def set_tempo_factor(self, factor: float) -> None:
        """Set tempo scaling factor, clamped and rounded to the nearest 0.05.

        The floor used to be 0.5. On a 165 BPM piece that is still 82 BPM,
        which is no help to a beginner. It now goes down to a quarter speed.
        """
        factor = max(TEMPO_MIN, min(1.0, factor))
        factor = round(factor * 20) / 20  # round to nearest 0.05
        if factor == self._tempo_factor:
            return
        anchor_tick = None
        if self._playing:
            # Bring the old tempo up to the moment of the change. Otherwise
            # a slow event handler pins today's capture time to yesterday's
            # frame and introduces a persistent early timing bias.
            self.update()
            anchor_tick = self._last_tick
        self._tempo_factor = factor
        self._config.tempo_factor = factor
        if self._playback_ms > self._assessment_start_ms and self._matcher is not None:
            self._attempt_uniform_tempo = False
        fresh_start = (not self._playing and self._playback_ms <= self._assessment_start_ms
                       and (self._matcher is None or self._matcher.get_statistics()["total"] == 0))
        self._attempt_tempo_factor = (factor if fresh_start
                                      else min(self._attempt_tempo_factor, factor))
        # Re-pin the clocks rather than resetting. Changing speed does not
        # invalidate the bars already played, and throwing the score away
        # mid-song for reaching for the slow-down key is a poor trade.
        # Make the change effective at the last processed clock tick. If
        # update itself took time, the next normal update must advance that
        # interval through wait/beat/end boundaries instead of bypassing them.
        self._reanchor_audio(preserve_history=True, at_tick=anchor_tick)
        if self._playback_ms < self._count_in_target_ms:
            self._refresh_health_window()
        if self._matcher is not None:
            self._matcher.set_retire_delay(self._detector_delay_ms() * factor)
        self._feedback.reset()
        self._config.save()
        self.save_practice_state()

    def set_noise_gate_db(self, db: float) -> None:
        """Set noise gate threshold, clamped to [-80, -20] and rounded to int."""
        db = max(-80, min(-20, round(db)))
        if db != self._noise_gate_db:
            self._attempt_uniform_input = False
        self._noise_gate_db = db
        self._config.audio.noise_gate_db = db
        if self._audio_capture is not None:
            self._audio_capture.set_noise_gate_db(db)
        self._config.save()

    def update(self) -> None:
        """Advance playback clock by real elapsed time."""
        self._test_recording.observe()
        # Update signal level meter and tuner even when paused (so user can verify signal)
        if self._audio_capture is not None:
            raw_db = self._audio_capture.get_signal_db()
            self._signal_db = raw_db
            self._signal_db_smooth = self._signal_db_smooth * 0.7 + raw_db * 0.3
            freq, conf = self._audio_capture.get_tuner_data()
            self._tuner_freq = freq
            self._tuner_confidence = conf
            if freq > 0 and conf > 0.5:
                # Frequency jump guard: ignore wild jumps (> 50% change)
                if (self._tuner_freq_smooth > 0
                        and abs(freq - self._tuner_freq_smooth) / self._tuner_freq_smooth > 0.5):
                    # Wild jump — use very low alpha to dampen
                    alpha = 0.02
                else:
                    # Adaptive EMA: high confidence → faster, low → slower
                    alpha = 0.10 if conf > 0.8 else 0.03
                if self._tuner_freq_smooth > 0:
                    self._tuner_freq_smooth = self._tuner_freq_smooth * (1 - alpha) + freq * alpha
                else:
                    self._tuner_freq_smooth = freq
                # Note hysteresis: only change displayed note after 8 stable frames (~130ms)
                from pickhero.audio.note_utils import freq_to_midi
                candidate_note = freq_to_midi(self._tuner_freq_smooth)
                if candidate_note != self._tuner_displayed_note:
                    self._tuner_note_stable_frames += 1
                    if self._tuner_note_stable_frames >= 8:
                        self._tuner_displayed_note = candidate_note
                        self._tuner_note_stable_frames = 0
                else:
                    self._tuner_note_stable_frames = 0
            else:
                # Slow decay instead of instant reset
                self._tuner_freq_smooth *= 0.92
                if self._tuner_freq_smooth < 20.0:
                    self._tuner_freq_smooth = 0.0
                    self._tuner_displayed_note = -1
                    self._tuner_note_stable_frames = 0

        if not self._playing:
            return

        health = getattr(self._audio_capture, "health_issue", None)
        if (self._audio_enabled and self._playback_ms >= self._count_in_target_ms
                and callable(health) and (issue := health())):
            self.mark_attempt_unreliable(issue)

        now = time.perf_counter()
        prev_ms = self._playback_ms
        if self._last_tick is not None:
            elapsed_ms = (now - self._last_tick) * 1000.0 * self._tempo_factor
            self._playback_ms += elapsed_ms
        self._last_tick = now
        if now - self._last_session_save >= 5:
            self.save_practice_state()

        # Wait mode: freeze if there are pending notes the player hasn't hit yet
        if (self._wait_mode and self._audio_enabled
                and self._playback_ms >= self._count_in_target_ms and self._matcher is not None):
            pending_at = self._matcher.first_pending_between(
                max(0.0, prev_ms), self._playback_ms)
            if pending_at is not None:
                self._playback_ms = pending_at
                self._last_tick = now
                if not self._wait_mode_frozen:
                    # Only on the way in. This used to run every frame, and
                    # pausing sends an all-notes-off on all sixteen channels,
                    # so waiting on one note pushed roughly a thousand MIDI
                    # messages a second at the synth for as long as it took.
                    self._wait_mode_frozen = True
                    if self._midi_player is not None and not self._backing_muted:
                        self._midi_player.pause()
            elif self._wait_mode_frozen:
                self._release_wait()

        # Count-in: keep clicking and start MIDI when crossing zero.
        self._count_in_tick(prev_ms)

        # Drain capture during count-in too. Only onsets inside the first
        # written note's timing window may cross over from the warm-up.
        if (self._audio_enabled
                and self._audio_capture is not None
                and self._matcher is not None):
            detected = self._audio_capture.get_notes()
            self._convert_timestamps(detected)
            if self._preroll_before_ms is not None:
                detected = [d for d in detected
                            if d.timestamp_ms + self._matcher.audio_offset_ms
                            >= self._preroll_before_ms]
            # While frozen in wait mode, pin detected timestamps to the frozen
            # playback position so matching hits the notes at the hit zone,
            # not future notes that drift ahead as real time passes.
            if self._wait_mode_frozen and detected:
                pinned_ts = self._playback_ms - self._matcher.audio_offset_ms
                for d in detected:
                    d.timestamp_ms = pinned_ts
            results = self._matcher.process_detected_notes(detected, self._playback_ms)
            self._feedback.add_results(results, self._playback_ms)
            self._feedback.cleanup(self._playback_ms)

        # Advance MIDI backing track (only during actual song)
        if (self._playback_ms >= self._count_in_target_ms and self._midi_player is not None
                and not self._wait_mode_frozen):
            midi_ms = (min(self._playback_ms, self._loop_end_ms - .001)
                       if self._loop_enabled and self._loop_end_ms is not None else self._playback_ms)
            self._midi_player.update(midi_ms)

        # Loop check — jump back to start marker when reaching end marker
        # (no count-in on loop)
        if (self._loop_enabled and self._loop_end_ms is not None
                and self._loop_start_ms is not None
                and self._playback_ms >= self._loop_finish_ms()):
            if self._midi_player is not None:
                self._midi_player.pause()
            suggested = self._record_section_attempt(self._loop_end_ms)
            if suggested is not None:
                self._playing = False
                self._last_tick = None
                self._stop_audio()
                self._suggested_tempo = suggested
                self._panel = self._advice_panel(suggested)
                self.save_practice_state()
                return
            if self._playback_ms > self._loop_end_ms + 20:
                self._start_practice(self._loop_start_ms, force_count_in=True)
            else:
                self.seek(self._loop_start_ms)
            return

        if not self._loop_enabled and self._playback_ms >= self._end_ms:
            self._playback_ms = self._end_ms
            self._playing = False
            self._last_tick = None
            if self._midi_player is not None:
                self._midi_player.pause()
            self._stop_audio(recording_complete=True)

            if not self._song_completed:
                self._weakest_sections = []
                self._song_completed = True
                if self._audio_enabled and self._matcher is not None:
                    self._feedback.add_results(self._matcher.finish(), self._playback_ms)
                    stats = self._matcher.get_statistics()
                    stats["timing_assessed"] = self._timing_assessed and not stats.get("timing_unknown", 0)
                    stats["reliable"] = not self._attempt_issues
                    if stats["total"] > 0:
                        weakest = self._matcher.get_weakest_sections()
                        if (self._full_song_attempt and not self._filter_omits_notes()
                                and not self._attempt_issues
                                and self._progress_tracker is not None and self._song_key):
                            self._is_new_best, self._recommendations = (
                                self._progress_tracker.record_detailed_result(
                                    self._song_key, stats,
                                    weakest, self._attempt_tempo_factor,
                                )
                            )
                        elif self._progress_tracker is not None and self._song_key:
                            self._record_section_attempt(self._timeline.duration_ms)
                        self._weakest_sections = weakest
                        self._log_result(stats)
                self.save_practice_state()

    def handle_event(self, event: pygame.event.Event) -> str | None:
        """Handle input. Returns 'menu' to go back, else None."""
        if event.type == pygame.KEYDOWN and event.key == pygame.K_u:
            self._test_recording.open()
            return None
        panel = self._active_panel()
        if panel is not None:
            return self._panel_action(panel.handle_event(event))
        if event.type == pygame.MOUSEMOTION:
            self._pointer = event.pos
            # The button state comes with the event. Released outside the
            # window there is no MOUSEBUTTONUP to receive, and without this
            # the dial would stay stuck to the pointer for the rest of the
            # session -- every later movement silently changing their tempo.
            if event.buttons and not event.buttons[0]:
                self._drag = None
            self._drag_to(event.pos)
            return None
        if event.type == pygame.MOUSEBUTTONUP and event.button == 1:
            return self._drop(event.pos)
        if event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
            self._pointer = event.pos
            if self._grab(event.pos):
                # Held, not yet turned. What it becomes -- a turn or a tap --
                # is decided on release by how far the player moved, so a hand that
                # twitches two pixels does not lose the click it meant.
                return None
            return self._handle_click(event.pos)
        if event.type == pygame.MOUSEWHEEL:
            # The wheel over the tempo control, because that is where a hand
            # already is when it wants to change speed. Over any of its
            # pieces: Studio splits it into a minus and a plus, Hi-fi draws
            # one dial, and both are "the tempo control" to a hand holding a
            # mouse.
            # From the events rather than from pygame.mouse.get_pos(): the
            # wheel event carries no position, and get_pos() answers for the
            # whole display rather than for this surface, which is also why
            # nothing could test this handler before.
            if self._over(self._pointer_pos(),
                          "tempo", "tempo_down", "tempo_up"):
                self.set_tempo_factor(self._tempo_factor + 0.05 * event.y)
            return None
        if event.type != pygame.KEYDOWN:
            return None

        if event.key == pygame.K_SPACE:
            self.toggle_play()
        elif event.key == pygame.K_ESCAPE:
            self.stop_audio()
            return "menu"
        elif event.key == pygame.K_LEFT:
            self.seek(self._playback_ms - self._beat_ms())
        elif event.key == pygame.K_RIGHT:
            self.seek(self._playback_ms + self._beat_ms())
        elif event.key == pygame.K_HOME:
            self.seek(0)
        elif event.key == pygame.K_a:
            self._toggle_audio()
        elif event.key == pygame.K_PAGEDOWN:
            self.set_tempo_factor(self._tempo_factor - 0.05)
        elif event.key == pygame.K_PAGEUP:
            self.set_tempo_factor(self._tempo_factor + 0.05)
        elif event.key == pygame.K_i:
            self._set_loop_start(self._playback_ms)
        elif event.key == pygame.K_o:
            self._set_loop_end(self._playback_ms)
        elif event.key == pygame.K_p:
            self._toggle_loop()
        elif event.key == pygame.K_BACKSPACE:
            self._clear_loop()
        elif event.key == pygame.K_b:
            self._toggle_backing()
        elif event.key == pygame.K_m:
            self._toggle_metronome()
        elif event.key == pygame.K_x:
            self.set_noise_gate_db(self._noise_gate_db - 5)
        elif event.key == pygame.K_c:
            self.set_noise_gate_db(self._noise_gate_db + 5)
        elif event.key == pygame.K_t:
            self._cycle_look()
        elif event.key == pygame.K_f:
            self._cycle_fret_limit()
        elif event.key in tuple(getattr(pygame, f"K_F{i}") for i in range(1, 13)):
            self._toggle_string(tuple(getattr(pygame, f"K_F{i}") for i in range(1, 13)).index(event.key) + 1)
        elif event.key == pygame.K_v:
            self._toggle_chord_mode()
        elif event.key == pygame.K_l:
            self._loop_weakest_section()
        elif event.key == pygame.K_h:
            self._open_help()
        elif event.key == pygame.K_s:
            self._open_setup()
        elif event.key == pygame.K_n:
            self._open_passages()
        elif event.key == pygame.K_w:
            self._toggle_wait_mode()

        return None

    def _page(self, surface: pygame.Surface) -> Page:
        """One frame's worth of everything the look is allowed to see.

        The clock, the counts and the toggles. Not the notes: those are drawn
        against a timeline and a scroll position that only this screen has,
        and handing them over a frame at a time would mean copying several
        hundred objects sixty times a second to tell a painter what it could
        ask for.
        """
        meta = self._timeline.metadata
        width, height = surface.get_size()
        stats = (self._matcher.get_statistics()
                 if self._audio_enabled and self._matcher is not None else None)
        # From the file's own measures, not from the clock. Dividing the
        # duration by a bar length counted an 8 bar exercise as 9: the file
        # ends a quarter of a bar past its last note and the rounding has
        # nowhere to put that.
        measures = self._timeline.measures
        if measures:
            bars = len(measures)
            bar = 1 + sum(1 for m in measures if m.start_ms <= self._playback_ms)
            bar = max(1, min(bars, bar - 1 if bar > 1 else 1))
        else:
            per_bar = max(1, self._beats_per_bar())
            length = self._ms_per_beat * per_bar
            bars = max(1, int(self._timeline.duration_ms / length + 0.999))
            bar = 1 + max(0, int(self._playback_ms / length))
        tuning = " ".join(midi_to_name(self._tuning[s]).rstrip("-0123456789")
                          for s in sorted(self._tuning, reverse=True))
        instrument_label = f"{self._instrument.short_name} ({tuning})"
        subtitle = " · ".join(x for x in (meta.artist or "", meta.track_name or "",
                                          instrument_label) if x)
        return Page(
            width=width, height=height,
            title=meta.title or t("song.untitled"),
            subtitle=subtitle,
            bar=min(bar, bars), bars=bars,
            bpm=int(round(60_000 / self._beat_ms() * self._tempo_factor)),
            elapsed_ms=self._playback_ms,
            total_ms=self._timeline.duration_ms,
            visible_window_ms=self._visible_window_ms,
            timeline_markers=True,
            count_in_remaining=(max(1, math.ceil((self._count_in_target_ms - self._playback_ms)
                                                 / self._count_in_beat_ms))
                                if self._playback_ms < self._count_in_target_ms else 0),
            count_in_progress=((self._count_in_ms + self._playback_ms - self._count_in_target_ms)
                               / self._count_in_beat_ms) % 1.0,
            tempo_min_percent=round(TEMPO_MIN * 100),
            loop_status=self._loop_hud_text() or "",
            timeline_beats=self._timeline.beats_in_range(
                max(0.0, self._playback_ms - self._visible_window_ms),
                max(0.0, self._playback_ms + self._visible_window_ms + 1.0)),
            tempo_percent=int(round(self._tempo_factor * 100)),
            paused=not self._playing,
            accuracy=stats["accuracy_percent"] if stats else None,
            streak=self._feedback.streak,
            # Found means the right note. A semitone off is "close" and has its
            # own count; adding it here put "9 found" beside "0 %" on the same
            # line, and disagreed with the run log, which never counted it.
            found=stats["hits"] if stats else 0,
            expected=stats["total"] if stats else len(self._timeline.notes),
            in_time=stats["in_time"] if stats else 0,
            timing_measured=stats.get("timing_measured", stats["hits"]) if stats else 0,
            timing_unknown=stats.get("timing_unknown", 0) if stats else 0,
            # 0 at the noise gate, 1 at clipping. The meter is a "is anything
            # arriving" cue, so it is scaled from the gate the player set rather than
            # from silence -- below the gate nothing is being heard at all.
            signal=self._signal_level(),
            strings=self._lane_count,
            string_labels=self._string_labels(),
            toggles=[
                ("play.listen", "key.a", self._audio_enabled, "listen"),
                ("play.wait", "key.w", self._wait_mode, "wait"),
                ("play.loop", "key.p", self._loop_enabled, "loop"),
                ("play.metronome", "key.m", self._metronome_on(), "metronome"),
            ],
            banner=self._banner_message,
        )

    # The look owns the board. These answer "who do I ask where a note goes",
    # and they are None when no painter has claimed it -- which is the
    # fallback drawing, and the only case where this screen's own lane
    # arithmetic is still correct.
    _road = None                 # a painter with project()/note_box(depth)
    _stave = None                # a painter with lane_y()/note_box(x, w)

    def _claim_geometry(self, paint) -> None:
        """Work out which kind of board the look drew, once per frame."""
        self._road = paint if hasattr(paint, "project") else None
        self._stave = (paint if self._road is None and hasattr(paint, "lane_y")
                       else None)

    def _playhead_x(self, layout: _Layout) -> float:
        """Where "now" is, from the look if it drew the line itself."""
        if self._stave is not None and self._last_page is not None:
            return self._stave.playhead_x(self._last_page)
        return layout.hit_zone_x

    def _px_per_ms(self, layout: _Layout) -> float:
        """Pixels per millisecond across the board the look actually drew.

        The fallback derives it from the whole window; a painter that insets
        its stave has less room, and using the window's number there slides
        every note past the end of the board it is drawn on.
        """
        if self._stave is not None and self._last_page is not None:
            board = self._stave.board(self._last_page)
            usable = max(1.0, board.right - self._playhead_x(layout))
            return (usable / self._visible_window_ms
                    if self._visible_window_ms > 0 else 1.0)
        return layout.pixels_per_ms

    @staticmethod
    def _neighbour_times(notes) -> dict:
        """Previous and next distinct onset on each string, once per song."""
        strings: dict[int, set[float]] = {}
        for note in notes:
            strings.setdefault(note.string, set()).add(note.timestamp_ms)
        result = {}
        for string, stamps in strings.items():
            ordered = sorted(stamps)
            for index, stamp in enumerate(ordered):
                result[(stamp, string)] = (
                    ordered[index - 1] if index else None,
                    ordered[index + 1] if index + 1 < len(ordered) else None)
        return result

    def _reading_window(self, surface: pygame.Surface, paint) -> float:
        """A stable visual scale: resize or song density, never playback tempo.

        Dense passages get 40-pixel heads plus eight pixels of separation.
        Sparse exercises retain the familiar eight seconds of preview.
        """
        page = Page(*surface.get_size(), strings=self._lane_count)
        if paint is not None and hasattr(paint, "project"):
            # The far end has the smallest separation. Use its projected
            # slope so capsules also have breathing room near the horizon.
            far = paint.project(page, 0, self._lane_count, 1.0)[1]
            near = paint.project(page, 0, self._lane_count, 0.9)[1]
            usable = max(1, (near - far) * 10)
            pitch = paint.note_box(page, 0, self._lane_count, 1.0).height + 8
        elif paint is not None and hasattr(paint, "lane_y"):
            usable = max(1, paint.board(page).right - paint.playhead_x(page))
            pitch = 48
        else:
            width = surface.get_width()
            gutter = GUTTER_W if width >= 900 else (44 if width >= 420 else 0)
            usable = max(1, (width - gutter) * (1.0 - self._hit_zone_fraction))
            pitch = 48
        return max(750.0, min(8000.0, usable * self._min_note_gap_ms / pitch))

    def _string_labels(self) -> list[tuple[int, str, str]]:
        """(number, letter, name) per lane, highest string first.

        From the tuning actually in force -- the instrument's, or the one
        read out of the file's header for an alternate tuning -- so the
        gutter names the strings the player has rather than the ones a default
        guessed at.
        """
        # metadata.tuning, not timeline.tuning -- the latter does not exist,
        # so reading it fell back to the instrument every time and an
        # alternate tuning out of a file header was silently relabelled as
        # the standard one. The same expression is already at line 190.
        tuning = (getattr(self._timeline.metadata, "tuning", None)
                  or getattr(self._instrument, "tuning", {}) or {})
        out = []
        for number in range(1, self._lane_count + 1):
            midi = tuning.get(number)
            name = midi_to_name(midi) if midi is not None else str(number)
            letter = name[:-1] if name and name[-1].isdigit() else name
            out.append((number, letter, name))
        return out

    def _signal_level(self) -> float:
        """The input meter, 0 to 1, measured from their own noise gate up."""
        gate = float(self._noise_gate_db)
        span = max(6.0, -gate)
        return max(0.0, min(1.0, (self._signal_db_smooth - gate) / span))

    def render(self, surface: pygame.Surface) -> None:
        """Draw the full playing screen."""
        look = get_look()
        paint = painter(look.key)
        self._visible_window_ms = self._reading_window(surface, paint)
        layout = self._layout(surface)

        surface.fill(look.bg)
        # One query for the whole frame. The gates and the cards both want the
        # notes around the playhead, and the timeline lookup is the expensive
        # part of drawing them.
        self._upcoming = self._timeline.get_notes_in_range(
            self._playback_ms - 200.0, self._playback_ms + self.GATE_LEAD_MS + 50.0)
        self._due = self._notes_due_now()
        # One decision for the whole frame, for the same reason.
        self._banner_message = self._current_warning()
        page = self._page(surface)
        if paint is not None and hasattr(paint, "play"):
            # The look owns the shell: header, stave or road, gutter, playhead
            # and transport. The notes stay here, because they are drawn
            # against a timeline and a scroll position nothing else has.
            self._last_page = page
            self._claim_geometry(paint)
            self._sync_timing_window()
            paint.play(surface, page)
            # Without this the whole transport was decoration: the look drew
            # the buttons and this screen went on reading a map that only the
            # drawing it replaced ever filled in. Every click fell through.
            self._button_rects = {name: list(rects)
                                  for name, rects in page.hit.items()}
            self._draw_loop_region(surface, layout)
            self._draw_notes(surface, layout)
            self._draw_stage_verdicts(surface, layout)
            if page.count_in_remaining and hasattr(paint, "count_in"):
                paint.count_in(surface, page)
        else:
            self._claim_geometry(None)
            self._draw_lanes(surface, layout)
            self._draw_loop_region(surface, layout)
            self._draw_hit_zone(surface, layout)
            self._draw_notes(surface, layout)
            self._draw_hud(surface, layout)

        panel = self._active_panel()
        if panel is not None:
            panel.render(surface)
        else:
            self._test_recording.draw(surface)

    # -- Pure math helpers (testable without display) --

    def _layout(self, surface: pygame.Surface) -> _Layout:
        """Compute layout from current surface dimensions.

        The window is resizable, so every dimension here is derived. Nothing
        may assume 1280x720.
        """
        w, h = surface.get_size()
        gutter = GUTTER_W if w >= 900 else (44 if w >= 420 else 0)
        lane_top = TOP_BAR_H
        lane_bottom = max(lane_top + 40, h - TOOLBAR_H)
        lane_area = max(1.0, lane_bottom - lane_top)
        lane_height = lane_area / max(self._lane_count, 1)
        # Cap the card height so four lanes on a tall window do not turn into
        # four enormous slabs.
        card_h = min(lane_height * NOTE_HEIGHT_FRACTION, CARD_MAX_H)
        hit_zone_x = gutter + (w - gutter) * self._hit_zone_fraction
        usable_width = max(1.0, w - hit_zone_x)
        pixels_per_ms = (usable_width / self._visible_window_ms
                         if self._visible_window_ms > 0 else 1.0)
        return _Layout(
            screen_w=w,
            screen_h=h,
            lane_height=lane_height,
            note_h=card_h,
            hit_zone_x=hit_zone_x,
            usable_width=usable_width,
            pixels_per_ms=pixels_per_ms,
            visible_window_ms=self._visible_window_ms,
            lane_count=self._lane_count,
            gutter_x=float(gutter),
            lane_top=float(lane_top),
            lane_bottom=float(lane_bottom),
            card_h=float(card_h),
        )

    @staticmethod
    def note_x(note_timestamp_ms: float, playback_ms: float,
               hit_zone_x: float, pixels_per_ms: float) -> float:
        """Calculate the x position of a note."""
        return hit_zone_x + (note_timestamp_ms - playback_ms) * pixels_per_ms

    @staticmethod
    def note_width(duration_ms: float, pixels_per_ms: float) -> float:
        """Calculate note rectangle width, enforcing minimum."""
        return max(duration_ms * pixels_per_ms, CARD_MIN_W)

    # -- Drawing --

    def _draw_lanes(self, surface: pygame.Surface, layout: _Layout) -> None:
        """Lane bands, the string-name gutter, and the bar and beat grid."""
        look = get_look()
        if look.stage == "highway":
            # One ladder, computed where it has always been computed. Passing
            # the notes in instead would put GATE_LEAD_MS and the six steps
            # into highway.py as a second copy that drifts from this one.
            gates = tuple(self._gate_brightness(s)
                          for s in self._lane_order(layout))
            highway.paint(surface, layout, self._lane_order(layout), gates,
                          self._tuning)
            return
        for i in range(layout.lane_count):
            y = layout.lane_top + i * layout.lane_height
            bg = look.stage_lane_even if i % 2 == 0 else look.stage_lane_odd
            pygame.draw.rect(surface, bg,
                             (0, int(y), layout.screen_w, int(layout.lane_height) + 1))

        self._draw_grid(surface, layout)

        if look.string_style == "wire":
            self._draw_wires(surface, layout)
        else:
            # The centre line of each lane carries the string colour, so the
            # row and the notes on it read as belonging together.
            for i in range(layout.lane_count):
                cy = int(layout.lane_top + (i + 0.5) * layout.lane_height)
                colour = design.shade(design.string_colour(i + 1), 0.42)
                pygame.draw.line(surface, colour,
                                 (int(layout.gutter_x), cy), (layout.screen_w, cy), 1)

        if look.note_shape != "circle":
            self._draw_lane_gates(surface, layout)

        if layout.gutter_x > 0:
            self._draw_string_gutter(surface, layout)

    @staticmethod
    def _lane_order(layout: _Layout) -> list[int]:
        """Lowest string at the left, matching the road's labelled targets."""
        return list(range(layout.lane_count, 0, -1))

    # A real string is thicker the lower it sounds. Drawn rather than
    # described: the bass ukulele's E is a visibly fatter wire than its G,
    # and on a page the thickness is as much of a cue as the colour.
    WIRE_MIN_PX = 2

    def _draw_wires(self, surface: pygame.Surface, layout: _Layout) -> None:
        """Strings as strings: round wire, thickest at the bottom."""
        left = int(layout.gutter_x)
        right = layout.screen_w
        for i in range(layout.lane_count):
            cy = int(layout.lane_top + (i + 0.5) * layout.lane_height)
            thickness = self.WIRE_MIN_PX + i
            top = cy - thickness // 2
            # Three shades down the wire, so it reads as round rather than
            # as a rule someone drew.
            for row, shade in enumerate((0.78, 0.55, 0.86)):
                band = pygame.Rect(left, top + row * max(1, thickness // 3),
                                   right - left, max(1, thickness // 3))
                pygame.draw.rect(surface, design.shade((185, 181, 172), shade),
                                 band)
            pygame.draw.rect(surface, design.shade((141, 137, 127), 1.0),
                             (left, top, right - left, thickness), width=0,
                             border_radius=1)

    # How far ahead a gate starts to light up. Long enough to be a warning,
    # short enough that only the note actually arriving is lit.
    GATE_LEAD_MS = 280.0
    GATE_STEPS = 6

    def _draw_lane_gates(self, surface: pygame.Surface, layout: _Layout) -> None:
        """A permanent landing spot per string, which lights as a note nears.

        The screen had one white line across every lane and nothing saying
        "the G is going to land HERE". A beginner has to work out both which
        row and when, from the same moving object. A fixed, string-coloured
        gate answers the first question before the note arrives, so only the
        timing is left to read.

        Everything here is a blit of a pre-baked strip: one dim strip for all
        lanes, plus one bright gate per lane from a six-step ladder. Nothing
        is allocated per frame.
        """
        lanes = layout.lane_count
        card_h = max(12, int(layout.card_h))
        # As wide as the narrowest card that can land in it, so the gate reads
        # as the slot the note fits into rather than a box behind it.
        gate_w = max(CARD_MIN_W + 6, int(card_h * 0.78))
        left = int(layout.hit_zone_x) - gate_w // 2
        top = int(layout.lane_top)
        height = int(layout.lane_bottom - layout.lane_top)
        if height <= 0 or lanes <= 0:
            return

        def paint_idle(strip):
            for i in range(lanes):
                cy = int((i + 0.5) * layout.lane_height)
                rect = pygame.Rect(0, cy - card_h // 2, gate_w, card_h)
                pygame.draw.rect(strip, design.with_alpha(
                    design.string_colour(i + 1), 70), rect,
                    width=2, border_radius=design.RADIUS_LG)

        surface.blit(design.sprite(("gates", (gate_w, height), lanes, card_h),
                                   (gate_w, height), paint_idle), (left, top))

        # The brightening, per lane, driven by the next unplayed note on it.
        for i in range(lanes):
            step = self._gate_brightness(i + 1)
            if step <= 0:
                continue
            cy = int(layout.lane_top + (i + 0.5) * layout.lane_height)
            alpha = 70 + int(185 * step / self.GATE_STEPS)
            size = (gate_w, card_h)

            def paint_lit(layer, n=i + 1, a=alpha, st=step):
                colour = design.string_colour(n)
                # The last two steps fill as well as brighten, so the gate
                # reads as loaded rather than merely outlined. Filling only at
                # the end keeps five quiet lanes quiet.
                if st >= self.GATE_STEPS - 1:
                    pygame.draw.rect(layer, design.with_alpha(colour, 46),
                                     layer.get_rect(),
                                     border_radius=design.RADIUS_LG)
                pygame.draw.rect(layer, design.with_alpha(colour, a),
                                 layer.get_rect(), width=3,
                                 border_radius=design.RADIUS_LG)

            surface.blit(design.sprite(("gate", size, i + 1, alpha, step), size,
                                       paint_lit),
                         (left, cy - card_h // 2))

    def _gate_brightness(self, string: int) -> int:
        """0 to GATE_STEPS, from how close the next note on this lane is."""
        soonest = None
        for note in self._upcoming:
            if note.string != string:
                continue
            gap = note.timestamp_ms - self._playback_ms
            if gap < -40.0 or gap > self.GATE_LEAD_MS:
                continue
            if soonest is None or gap < soonest:
                soonest = gap
        if soonest is None:
            return 0
        nearness = design.ease_out(1.0 - max(0.0, soonest) / self.GATE_LEAD_MS)
        return max(1, int(self.GATE_STEPS * nearness))

    def _draw_string_gutter(self, surface: pygame.Surface, layout: _Layout) -> None:
        """Name every row, permanently.

        The old screen only said which row was which string inside a help
        overlay hidden behind a single-letter key.
        """
        look = get_look()
        pygame.draw.rect(surface, design.shade(look.stage_bg, 0.8),
                         (0, int(layout.lane_top), int(layout.gutter_x),
                          int(layout.lane_bottom - layout.lane_top)))
        for i in range(layout.lane_count):
            string = i + 1
            cy = layout.lane_top + (i + 0.5) * layout.lane_height
            colour = design.string_colour(string)
            midi = self._tuning.get(string)
            name = midi_to_name(midi) if midi is not None else str(string)
            letter = name[:-1] if name and name[-1].isdigit() else name
            wide = layout.gutter_x >= GUTTER_W
            design.text(surface, letter,
                        (int(layout.gutter_x) - (18 if wide else 12),
                         int(cy) - (20 if wide else 12)),
                        colour, design.TEXT_XL if wide else design.TEXT_MD,
                        bold=True, align="right")
            if wide:
                design.text(surface, name, (int(layout.gutter_x) - 18, int(cy) + 7),
                            design.shade(colour, 0.7), design.TEXT_XS, align="right")
        pygame.draw.line(surface, look.stage_bar_line,
                         (int(layout.gutter_x), int(layout.lane_top)),
                         (int(layout.gutter_x), int(layout.lane_bottom)), 1)

    def _beat_ms(self) -> float:
        """Length of one beat in song milliseconds."""
        return self._timeline.beat_duration_at(max(0.0, self._playback_ms))

    def _draw_grid(self, surface: pygame.Surface, layout: _Layout) -> None:
        """Beat and bar lines.

        Timing is the hardest thing for a beginner, and the screen used to
        offer nothing to hold on to between bar numbers. Beats are faint, bars
        are clear and numbered.
        """
        look = get_look()
        top, bottom = int(layout.lane_top), int(layout.lane_bottom)
        beat = self._beat_ms()
        if beat <= 0 or layout.pixels_per_ms <= 0:
            return
        span_left = (layout.hit_zone_x - layout.gutter_x) / layout.pixels_per_ms
        view_start = self._playback_ms - span_left
        view_end = self._playback_ms + self._visible_window_ms

        # A dense passage in a narrow window would draw hundreds of lines.
        if beat * layout.pixels_per_ms >= 8:
            faint = look.stage_beat_line
            positions = [b.timestamp_ms for b in
                         self._timeline.beats_in_range(max(0.0, view_start), view_end)]
            if view_start < 0:
                positions[:0] = [k * self._ms_per_beat for k in range(
                    math.floor(view_start / self._ms_per_beat), 0)]
            last_x = -math.inf
            for position in positions:
                x = self.note_x(position, self._playback_ms,
                                layout.hit_zone_x, layout.pixels_per_ms)
                if x < layout.gutter_x or x > layout.screen_w or x - last_x < 8:
                    continue
                pygame.draw.line(surface, faint, (int(x), top), (int(x), bottom), 1)
                last_x = x

        measures = self._timeline.measures
        if not measures:
            return
        bar_colour = look.stage_bar_line
        label_colour = design.shade(look.hud_text, 0.75)
        for measure in measures:
            if measure.start_ms > view_end or measure.end_ms < view_start:
                continue
            x = self.note_x(measure.start_ms, self._playback_ms,
                            layout.hit_zone_x, layout.pixels_per_ms)
            if x < layout.gutter_x - 2 or x > layout.screen_w:
                continue
            pygame.draw.line(surface, bar_colour, (int(x), top), (int(x), bottom), 1)
            design.text(surface, str(measure.index + 1), (int(x) + 5, top + 3),
                        label_colour, design.TEXT_XS)

    def _draw_hit_zone(self, surface: pygame.Surface, layout: _Layout) -> None:
        """The line notes must be played on, and a pulse marking the beat."""
        if get_look().stage == "highway":
            return   # the rings and the bar are part of the road
        look = get_look()
        x = int(layout.hit_zone_x)
        top, bottom = int(layout.lane_top), int(layout.lane_bottom)

        beat = self._beat_ms()
        if beat > 0 and self._playing and bottom > top:
            # Bright on the beat and fading between, so there is a pulse to
            # play against without a metronome click.
            recent = self._timeline.beats_in_range(
                max(0.0, self._playback_ms - 4 * beat), self._playback_ms + 0.001)
            phase = ((self._playback_ms - recent[-1].timestamp_ms) / beat
                     if recent else (self._playback_ms % beat) / beat)
            strength = max(0.0, 1.0 - phase * 3.0)
            if strength > 0.01:
                # Quantised to four steps so the sprite cache has four entries
                # rather than one per frame. Nobody can see the difference at
                # 60 frames a second; the allocation costs real milliseconds.
                alpha = int(70 * strength) // 18 * 18 + 18
                size = (26, bottom - top)

                def paint(layer, a=alpha):
                    pygame.draw.rect(layer, design.with_alpha(look.hit_zone, a),
                                     layer.get_rect())

                surface.blit(design.sprite(("beat", size, look.hit_zone, alpha),
                                           size, paint), (x - 13, top))

        pygame.draw.line(surface, look.hit_zone, (x, top), (x, bottom), 3)
        self._draw_strays(surface, layout, x, top)

    def _draw_strays(self, surface: pygame.Surface, layout: _Layout,
                     x: int, top: int) -> None:
        """Say what was heard that belonged to no written note.

        Without this, a note played far enough off produces nothing at all
        and then the written note turns red -- which looks exactly like the
        app not having heard them. The player cannot tell whether it is deaf or the player
        is early, and that is the difference between a fault the player can correct
        and one the player cannot.

        Deliberately quiet: grey, small, above the lanes, gone in under a
        second. It is information, not a verdict.
        """
        strays = self._feedback.recent_strays(self._playback_ms)
        if not strays:
            return
        look = get_look()
        age, midi = strays[-1]
        fade = 1.0 - age / self._feedback.STRAY_MS
        label = t("play.heard", note=midi_to_name(midi))
        face = design.font(design.TEXT_XS, bold=True)
        w = face.size(label)[0] + design.SPACE_4
        rect = pygame.Rect(x - w // 2, top - 26, w, 20)
        rect.left = max(int(layout.gutter_x) + 2, rect.left)
        rect.right = min(layout.screen_w - 2, rect.right)
        shade = design.mix(design.ink(look.bg, 0.55), look.bg, 1.0 - fade)
        pygame.draw.rect(surface, design.surface(look.bg, 1), rect,
                         border_radius=design.RADIUS_SM)
        pygame.draw.rect(surface, shade, rect, width=1,
                         border_radius=design.RADIUS_SM)
        design.text(surface, label, (rect.centerx, rect.y + 3), shade,
                    design.TEXT_XS, bold=True, align="center")

    def _draw_notes(self, surface: pygame.Surface, layout: _Layout) -> None:
        """Note cards.

        The fill is always the string colour, so a note never stops telling
        you which string to play. Judgement is carried by the ring and a short
        glow instead of by recolouring the card, which would throw the string
        identity away exactly when the player looks at it.
        """
        view_start = self._playback_ms - LEFT_MARGIN_MS
        view_end = self._playback_ms + self._visible_window_ms + RIGHT_MARGIN_MS
        notes = self._timeline.get_notes_in_range(view_start, view_end)

        look = get_look()
        if look.stage == "highway":
            notes = list(reversed(notes))
        card_h = max(12, int(layout.card_h))
        if look.note_shape == "circle":
            # A disc is as wide as it is tall, and smaller than a card: the
            # page it sits on carries the grid, so the note does not have to
            # be a slab to be found.
            card_h = min(card_h, 54)
        radius = max(design.RADIUS_SM, min(design.RADIUS_LG, card_h // 5))
        next_gap = self._gaps_to_next_note(notes)
        items = []
        order = self._lane_order(layout)

        for note in notes:
            if not self._note_passes_filter(note):
                continue

            x = self.note_x(note.timestamp_ms, self._playback_ms,
                            self._playhead_x(layout), self._px_per_ms(layout))
            depth = 0.0
            if self._road is not None:
                # The road's own projection, asked of the look that drew it.
                # A note placed by anything else lands beside the gate it is
                # supposed to arrive in, which is what happened when this
                # screen kept its own copy of the geometry.
                depth = ((note.timestamp_ms - self._playback_ms)
                         / max(1.0, self._visible_window_ms))
                run_off_ms = max(0.02 * self._visible_window_ms,
                                 self._timing_window_for_marker() + 50.0)
                if depth < -run_off_ms / self._visible_window_ms or depth > 1.0:
                    continue
                if note.string not in order:
                    continue
                rect = self._road.note_box(self._last_page,
                                           order.index(note.string),
                                           len(order), depth)
                neighbours = self._note_neighbours.get((note.timestamp_ms, note.string), ())
                gaps = [abs(self._road.project(
                    self._last_page, order.index(note.string), len(order),
                    (stamp - self._playback_ms) / self._visible_window_ms)[1] - rect.centery)
                    for stamp in neighbours if stamp is not None
                    and -run_off_ms / self._visible_window_ms <=
                    (stamp - self._playback_ms) / self._visible_window_ms <= 1.0]
                if gaps:
                    height = max(1, min(rect.height, int(min(gaps) - 4)))
                    rect.inflate_ip(0, height - rect.height)
                label_size = max(7, min(design.TEXT_LG, rect.height))
            elif self._stave is not None:
                # Same again for the flat looks: the stave was drawn by the
                # painter and it owns where a lane is.
                natural = self._stave.note_size(self._last_page, self._lane_count)
                head_w = self._card_width(note, natural, card_h, next_gap, layout)
                rect = self._stave.note_box(self._last_page, note.string,
                                            self._lane_count, x, head_w)
                if rect.bottom < layout.lane_top or rect.top > layout.screen_h:
                    continue
                digit = (self.DISC_DIGIT if look.note_shape == "circle"
                         else self.CARD_DIGIT)
                label_size = max(design.TEXT_XS,
                                 min(design.TEXT_2XL, int(rect.height * digit)))
            else:
                lane_y = layout.lane_top + (note.string - 1) * layout.lane_height

                head_w = self._card_width(note, card_h * 1.2, card_h, next_gap, layout)
                if look.note_shape == "circle":
                    head_w = min(head_w, card_h)
                height = min(card_h, max(1, round(head_w / 1.2)))
                left = round(x) if look.key == "studio" else round(x) - head_w // 2
                rect = pygame.Rect(left,
                                   round(lane_y + layout.lane_height / 2 - height / 2),
                                   head_w, height)
                digit = (self.DISC_DIGIT if look.note_shape == "circle"
                         else self.CARD_DIGIT)
                label_size = max(design.TEXT_XS,
                                 min(design.TEXT_2XL, int(height * digit)))

            if self._road is None and (rect.right < layout.gutter_x or rect.left > layout.screen_w):
                continue
            items.append((note, rect, label_size, depth))

        # Duration first, all heads second, labels last. A held note cannot
        # paint over the next fret, and chip placement sees every neighbour.
        duration_clip = surface.get_clip()
        if self._stave is not None:
            surface.set_clip(duration_clip.clip(self._stave.board(self._last_page)))
        for note, rect, _, depth in items:
            colour = design.string_colour(note.string)
            if self._road is None:
                end_x = self.note_x(note.timestamp_ms + note.duration_ms, self._playback_ms,
                                   self._playhead_x(layout), self._px_per_ms(layout))
                if end_x > rect.right:
                    self._draw_tail(surface, rect, round(end_x) - rect.right, colour, 85)
            else:
                end_depth = min(1.0, (note.timestamp_ms + note.duration_ms - self._playback_ms)
                                / self._visible_window_ms)
                end = self._road.project(self._last_page, order.index(note.string),
                                         len(order), end_depth)
                pygame.draw.line(surface, design.shade(colour, 0.4), rect.center, end[:2], 3)
        surface.set_clip(duration_clip)
        self._chip_heads = list({tuple(rect): rect for _, rect, _, _ in items}.values())
        self._chip_occupied = []
        self._chip_cells = {}
        for rect in self._chip_heads:
            for cell in self._chip_cell_keys(rect):
                self._chip_cells.setdefault(cell, []).append((rect, True))
        labels = []
        for note, rect, label_size, depth in items:
            colour = design.string_colour(note.string)
            tail_w = 0
            radius = max(2, min(design.RADIUS_LG, rect.height // 5))

            state = self._feedback.match_state(note) if self._audio_enabled else None
            age = (self._feedback.effect_age_ms(note, self._playback_ms)
                   if state is not None else None)
            past = note.timestamp_ms < self._playback_ms

            if (self._road is not None and state is None
                    and 0 <= self._playback_ms - note.timestamp_ms <= 60):
                # At the strike line the capsule becomes the lit landing pad.
                # Never anticipate this pulse: a lit target before the beat
                # teaches an early strike. Keep the approaching note visible.
                self._road.strike_ring(
                    surface, self._last_page, order.index(note.string), len(order),
                    lit=True, name=midi_to_name(self._tuning[note.string]), colour=colour)
                continue

            if state is None and past and self._audio_enabled:
                # Gone by without a verdict yet. Exactly one ghost style, and
                # it keeps the string's hue -- but only as far as the page
                # allows. design.shade(colour, 0.9) measured 1.37:1 on Hi-fi's
                # paper, which made the note the player missed the one the player could not
                # see. readable() darkens it on paper and leaves it alone on
                # the dark stages.
                spent = design.readable(colour, look.stage_bg)
                if tail_w > 0:
                    self._draw_tail(surface, rect, tail_w, colour, 40)
                design.ghost(surface, rect, spent, radius=radius,
                             circle=look.note_shape == "circle")
                if look.key == "studio":
                    paint_studio.note_onset(surface, rect, spent)
                self._draw_fret(surface, rect, note.fret, label_size,
                                spent, outline=False, fit_inside=True)
                continue

            ring, ring_w, glow = None, 3, 0.0
            # A look that brings its own note fill is not using the string
            # palette on the stage, which is the whole reason it may put the
            # stage on paper.
            base = look.note_fill if look.note_fill is not None else colour
            ink = look.note_ink if look.note_ink is not None else (18, 20, 28)
            # A note that has been judged is not due any more: the moment it
            # was marking is over. Without this a missed note wore a red ring
            # and a bloom at the same time -- "play this now" and "you did
            # not" on the same note, which is the app arguing with itself.
            near = None if state is not None else                 self._due.get((note.timestamp_ms, note.string))
            if near is not None and look.note_fill is None and self._road is None:
                # The broad, low-opacity skirt gives the light a soft edge;
                # the closer mask anchors it to the card's actual contour.
                shapes.glow(surface, rect, colour,
                            spread=max(12, round(rect.height * GLOW_OUTER_SPREAD)),
                            alpha=int(GLOW_OUTER_ALPHA * near), radius=radius)
                shapes.glow(surface, rect, colour,
                            spread=max(6, round(rect.height * GLOW_SPREAD)),
                            alpha=int(GLOW_ALPHA * (0.45 + 0.55 * near)),
                            radius=radius)
            if near is not None and look.note_fill is not None:
                base = look.hit_zone
                # The drawing writes this one in near-white, and on_accent
                # now answers near-white for it -- which it did not before
                # the orange was darkened to carry a white numeral at 4.5.
                # A branch that forced the light end would have hidden the
                # day the colour drifted back.
                ink = design.on_accent(look.hit_zone)
                halo = int(round(min(rect.width, rect.height) * 0.78))
                shapes.disc(surface, rect.center, halo,
                            design.mix(look.stage_bg, look.hit_zone,
                                       0.20 + 0.25 * near))
            fill = base
            if state is not None:
                # From the look, not from a shared constant. design.HIT is
                # tuned for a dark stage and measures 1.55:1 on Hi-fi's
                # paper -- a verdict nobody can see is not a verdict.
                ring = {MatchType.HIT: look.feedback_hit,
                        MatchType.EARLY: look.feedback_hit,
                        MatchType.LATE: look.feedback_hit,
                        MatchType.CLOSE: look.feedback_close,
                        MatchType.MISS: look.feedback_miss}.get(state)
                ring_w = 5 if state in ON_PITCH else 3
                # The halo fires for a hit and for nothing else.
                #
                # A verdict is already encoded three ways -- the ring's
                # colour, its thickness, and whether the fill is darkened --
                # and a coloured cloud round the note is a fourth and the
                # loudest of them. With no microphone running every note
                # eventually misses, so the board filled with red clouds,
                # which is the app shouting about everything the player has not
                # played. Finding the note is the thing being learnt; being
                # told off for a note the player never reached for teaches nothing.
                #
                # A hit still blooms, because that one is worth saying.
                if state in ON_PITCH and age is not None and 0.0 <= age < GLOW_MS:
                    glow = 1.0 - age / GLOW_MS
                if state == MatchType.MISS:
                    fill = design.mix(base, look.stage_bg, 0.55)
            elif past:
                fill = design.mix(base, look.stage_bg, 0.45)

            if tail_w > 0:
                self._draw_tail(surface, rect, tail_w, fill, 150)
            if self._road is not None:
                self._road.capsule(
                    surface, self._last_page, order.index(note.string), len(order),
                    depth, colour=fill, ring=ring,
                    halo=state is None or glow > 0, ring_width=ring_w if ring else None,
                    rect=rect)
            elif look.note_shape == "circle":
                self._draw_disc(surface, rect, fill, ring, ring_w, glow)
            else:
                pill = look.note_shape == "pill"
                design.card(surface, rect, fill,
                            radius=rect.height // 2 if pill else radius,
                            ring=ring, ring_width=ring_w, glow=glow)
            if near is not None and look.note_fill is None and self._road is None:
                # The drawing's mark is an outline standing OFF the card in
                # the note's own colour, with a few pixels of board showing
                # between the two -- measured across its edge: fill, then
                # eight sheet pixels of board, then three of the same cyan,
                # then the bloom. Not a pale rim on the card, which is what
                # this drew and which desaturated the one edge the string's
                # identity lives in.
                gap = max(2, round(rect.height * HALO_GAP))
                outline = rect.inflate((gap + HALO_RING_W) * 2,
                                       (gap + HALO_RING_W) * 2)
                shapes.rounded_rect(surface, colour, outline, width=HALO_RING_W,
                                    border_radius=radius + gap + HALO_RING_W)
            if look.key == "studio":
                edge = design.mix(fill, look.ink_1, 0.65 if not past else 0.25)
                paint_studio.note_onset(surface, rect, edge)
            self._draw_fret(surface, rect, note.fret, label_size, ink,
                            outline=False, fit_inside=True)

            # The chips overlap the card's own top strip, so they are drawn
            # after it rather than before. The card keeps its colour through
            # all of this: the player found the note, and only the chip says when.
            labels.append((note, rect, label_size, state))

        # The nearest notes get first choice of the remaining label space.
        labels.sort(key=lambda item: abs(item[0].timestamp_ms - self._playback_ms))
        for note, rect, label_size, state in labels:
            verdict = None
            if state in (MatchType.EARLY, MatchType.LATE):
                verdict = self._draw_timing_note(surface, rect, state, note,
                                                 layout, label_size)
            self._draw_note_name(surface, rect, note, layout, label_size,
                                 verdict)

        # Nothing here any more. Every look marks the note that is due on the
        # note itself: Hi-fi colours it orange on a soft halo, the card looks
        # bloom it and brighten its rim. The ring that used to be drawn round
        # it was a second mark for the same thing, and once the bloom was
        # there it read as a circle somebody had left on the board.

    def _draw_stage_verdicts(self, surface: pygame.Surface, layout: _Layout) -> None:
        if self._road is None or not self._audio_enabled:
            return
        order = self._lane_order(layout)
        look = get_look()
        colours = {MatchType.HIT: look.feedback_hit, MatchType.EARLY: look.feedback_hit,
                   MatchType.LATE: look.feedback_hit, MatchType.CLOSE: look.feedback_close,
                   MatchType.MISS: look.feedback_miss}
        for note, state, age in self._feedback.recent_lane_verdicts():
            if note.string not in order or not self._note_passes_filter(note):
                continue
            # Hold fully readable, then fade out over the last quarter-second.
            strength = min(1.0, max(0.0, (LANE_VERDICT_MS - age) / 250.0))
            self._road.strike_feedback(surface, self._last_page,
                order.index(note.string), len(order),
                t("stage.verdict.hit" if state in ON_PITCH else "stage.verdict." + state.value),
                "hit" if state in ON_PITCH else state.value, colours[state], strength,
                timing_label=t("stage.verdict." + state.value)
                    if state in (MatchType.EARLY, MatchType.LATE) else "",
                timing_direction=state.value)

    @staticmethod
    def _gaps_to_next_note(notes: list[NoteEvent]) -> dict[tuple[float, int], float]:
        """How long until the next note on the same string, per note."""
        per_string: dict[int, list[float]] = {}
        for note in notes:
            per_string.setdefault(note.string, []).append(note.timestamp_ms)
        gaps: dict[tuple[float, int], float] = {}
        for string, stamps in per_string.items():
            ordered = sorted(set(stamps))
            for i, stamp in enumerate(ordered):
                nxt = ordered[i + 1] if i + 1 < len(ordered) else None
                gaps[(stamp, string)] = (nxt - stamp) if nxt is not None else 1e9
        return gaps

    def _card_width(self, note: NoteEvent, natural_w: float, card_h: int,
                    next_gap: dict[tuple[float, int], float],
                    layout: _Layout) -> int:
        """Card width, never wider than the space before the next note.

        A fast passage used to draw 34 px cards 20 px apart, which merged the
        whole bar into one blob and made the fret numbers unreadable.
        """
        want = max(CARD_MIN_W, min(int(natural_w), int(card_h * 1.2)))
        neighbours = self._note_neighbours.get((note.timestamp_ms, note.string), ())
        gaps = [abs(stamp - note.timestamp_ms) for stamp in neighbours if stamp is not None]
        gap_ms = min(gaps, default=next_gap.get((note.timestamp_ms, note.string), 1e9))
        room = gap_ms * self._px_per_ms(layout) - 8.0
        return max(1, min(want, int(room)))

    # -- Corner chips on a note card ---------------------------------

    # A chip sits with its middle on the card's top edge: half above it,
    # half on the card's own empty top strip. That halves the room it needs
    # above the lane, and it is the only reason a readable size fits at all.
    # Measured room above the top string's card is 30 / 20 / 15 / 12 px
    # across the four real window sizes, while a 28 px chip drawn this way
    # asks for 14 of them.
    CHIP_PAD_X = 10
    CHIP_PAD_Y = 3
    CHIP_ARROW_W = 20

    def _chip_floor(self, layout: _Layout) -> int:
        """The lowest y a corner chip may start at.

        The warning strip occupies the band under the top bar whenever it
        has something to say, and the quiet one fires 2.5 s after the player stops
        playing -- every time the player pauses to work something out. Moving the
        floor down while the strip is up keeps the top string's chips from
        being swallowed by it, and keeps them from jumping when it appears.
        """
        floor = int(layout.lane_top) + 1
        if self._banner_message is not None:
            floor = max(floor, TOP_BAR_H + BANNER_H + 1)
        return floor

    # A fret digit's ink is 0.70 of its nominal size, measured across every
    # size the card ever asks for (0.692 to 0.731). Half of that, rounded up
    # a little, is how far the number really reaches from the card's middle.
    # Reserving the whole nominal size instead cost 5 px at six strings and
    # 7 px at four, which was enough to suppress every chip on a 1024 px
    # window -- a blank screen caused by arithmetic, not by lack of room.
    FRET_INK_HALF = 0.37

    @staticmethod
    def _chip_cell_keys(rect):
        """Small spatial buckets keep label work local, even for large imports."""
        for x in range(rect.left // 64, rect.right // 64 + 1):
            for y in range(rect.top // 64, rect.bottom // 64 + 1):
                yield x, y

    def _register_chip(self, rect):
        self._chip_occupied.append(rect)
        for cell in self._chip_cell_keys(rect):
            self._chip_cells.setdefault(cell, []).append((rect, False))

    def _chip_rect(self, rect: pygame.Rect, w: int, h: int, right: bool,
                   layout: _Layout, label_size: int,
                   bottom: bool = False) -> pygame.Rect | None:
        """Place a chip at a corner of a card, or refuse to.

        Returns None when it would have to cover the fret number. That
        number is the one thing the player actually has to read, so it wins every
        contest for the same pixels.
        """
        left_edge = int(layout.gutter_x) + 1
        x = rect.right - w if right else rect.left
        x = max(left_edge, min(x, max(left_edge, layout.screen_w - w - 1)))
        reach = int(label_size * self.FRET_INK_HALF)
        # A card has a flat top edge and an empty strip under it, so a chip
        # may sit half on it. A disc has neither: half on means the chip is
        # over the curve, which is where the ink is, and the name ends up
        # printed across the note instead of beside it. So on a disc it
        # clears the shape entirely.
        overlap = (0 if get_look().note_shape == "circle" else
                   min(h // 2, max(0, rect.height // 2 - reach - 2)))
        if bottom:
            y = min(rect.bottom - overlap, int(layout.lane_bottom) - h - 1)
            if y < rect.centery + reach + 1:
                return None
        else:
            y = max(rect.top - h + overlap, self._chip_floor(layout))
            if y + h > rect.centery - reach - 1:
                return None
        candidate = pygame.Rect(x, y, w, h)
        padded = candidate.inflate(4, 2)
        for cell in self._chip_cell_keys(padded):
            for other, is_head in getattr(self, "_chip_cells", {}).get(cell, ()):
                if is_head and other == rect:
                    continue
                if (candidate if is_head else padded).colliderect(other):
                    return None
        return candidate

    def _draw_note_name(self, surface: pygame.Surface, rect: pygame.Rect,
                        note: NoteEvent, layout: _Layout, label_size: int,
                        taken: pygame.Rect | None) -> None:
        """The note's own name at the card's upper right.

        The fret number says where to put a finger. It does not say what
        comes out, and on four strings the same fret is four different
        notes. Naming it is also what lets their argue with the detector: when
        the app reports the wrong note, the player can see what it should have been.

        Drawn in the string colour with the same dark ink as the fret
        number, measured 5.66:1 at worst, on violet. The chip keeps full
        saturation even when the card behind it is dimmed, because the
        dimmed fill measures 1.78:1 -- unreadable on exactly the notes the player
        most wants to check.
        """
        look = get_look()
        label = midi_to_name(note.midi_note)
        size = design.TEXT_SM
        tw, th = design.font(size, bold=True).size(label)
        w, h = tw + self.CHIP_PAD_X, th + self.CHIP_PAD_Y * 2
        chip = self._chip_rect(rect, w, h, True, layout, label_size)
        if chip is None or (taken is not None and chip.colliderect(taken)):
            # A verdict is wide and it owns the top of the card while it
            # lasts. Rather than drop the name -- which the player asked for, and
            # which is the thing that lets their check the detector -- it goes
            # to the bottom corner underneath. The room below a card is the
            # same as the room above it, because the card is centred.
            chip = self._chip_rect(rect, w, h, True, layout, label_size,
                                   bottom=True)
        if chip is None:
            return
        if hasattr(self, "_chip_occupied"):
            self._register_chip(chip)
        colour = design.string_colour(note.string)
        if look.note_fill is not None:
            # A look that brings its own note colour has moved the string's
            # colour off the stage and into the gutter -- which is the whole
            # reason its stage can be paper. Twelve saturated chips put it
            # straight back, and on a page whose discipline is that almost
            # nothing is a filled shape they become the loudest thing on it.
            # Same word, same place, no box: readable() keeps enough of the
            # hue to still say which string.
            design.text(surface, label, (chip.centerx, chip.y + self.CHIP_PAD_Y),
                        design.readable(colour, look.stage_bg), size,
                        bold=True, align="center")
            return
        pygame.draw.rect(surface, colour, chip, border_radius=design.RADIUS_SM)
        pygame.draw.rect(surface, design.shade(colour, 0.55), chip, width=1,
                         border_radius=design.RADIUS_SM)
        design.text(surface, label, (chip.centerx, chip.y + self.CHIP_PAD_Y),
                    design.FRET_INK, size, bold=True, align="center")

    def _draw_timing_note(self, surface: pygame.Surface, rect: pygame.Rect,
                          state: MatchType, note: NoteEvent,
                          layout: _Layout,
                          label_size: int) -> pygame.Rect | None:
        """Say how the player was off, in words, at the card's upper left.

        "Zu früh" is advice. Red is a scolding, and it was the same red as
        having played nothing at all. The wording is graded because the
        difference between a hair early and a whole beat early is the
        difference between a habit to nudge and a note in the wrong place.

        It used to sit beside the note at TEXT_XS, 18 px of ink, which reads
        as a squeak. It is normal size now and drops one step at most: a
        verdict too small to read is the same as no verdict, so when the
        room runs out the WORD gets shorter rather than the letters smaller.

        An arrow still points the way the player has to move, so the verdict never
        rests on the colour alone.
        """
        error = self._feedback.timing_error_ms(note)
        if error is None:
            return None
        early = state is MatchType.EARLY
        slight = abs(error) <= 2.0 * self._timing_window_for_marker()
        plain = "play.early" if early else "play.late"
        graded = "play.slightly_early" if early else "play.slightly_late"
        wanted = [graded, plain] if slight else [plain]

        for size in (design.TEXT_MD, design.TEXT_SM):
            for key in wanted:
                label = t(key)
                tw, th = design.font(size, bold=True).size(label)
                chip = self._chip_rect(
                    rect, tw + self.CHIP_ARROW_W + self.CHIP_PAD_X,
                    th + self.CHIP_PAD_Y * 2, False, layout, label_size)
                if chip is not None:
                    if hasattr(self, "_chip_occupied"):
                        self._register_chip(chip)
                    self._paint_timing_chip(surface, chip, label, size, early)
                    return chip
        return None

    def _paint_timing_chip(self, surface: pygame.Surface, chip: pygame.Rect,
                           label: str, size: int, early: bool) -> None:
        timing = get_look().feedback_early
        pygame.draw.rect(surface, timing, chip,
                         border_radius=design.RADIUS_SM)
        ink = design.on_accent(timing)
        # The arrow points along the timeline to where their strike actually
        # landed. Notes travel right to left, so earlier is left and later is
        # right: the arrow and the word say the same thing, and neither of
        # them is the colour. It deliberately does NOT point at the
        # correction -- an arrow that said "play later" next to the words
        # "zu früh" reads as a contradiction to someone still counting.
        cy = chip.centery
        tip = chip.x + 6 if early else chip.right - 6
        back = chip.x + 15 if early else chip.right - 15
        pygame.draw.polygon(surface, ink,
                            [(tip, cy), (back, cy - 6), (back, cy + 6)])
        text_x = chip.x + self.CHIP_ARROW_W if early else chip.x + 5
        design.text(surface, label, (text_x, chip.y + self.CHIP_PAD_Y), ink,
                    size, bold=True)

    # How tall the fret digit is as a fraction of the note it sits in.
    # Measured off the Hi-fi drawing: a 72 px disc carries an 18 px numeral,
    # which is a quarter. This screen was setting it at four fifths, so the
    # digit filled the disc edge to edge and the note stopped reading as an
    # object with a number on it. A face's cap height is about 0.70 of its
    # nominal size, hence the 0.40 rather than 0.28.
    DISC_DIGIT = 0.40
    CARD_DIGIT = 0.62            # a card has corners to spare; a disc has none

    @staticmethod
    def _draw_disc(surface: pygame.Surface, rect: pygame.Rect,
                   fill: tuple[int, int, int],
                   ring: tuple[int, int, int] | None, ring_width: int,
                   glow: float) -> None:
        """A note as a disc on a wire, the way a good amplifier would do it.

        Antialiased, and standing on a soft shadow. pygame.draw.circle has no
        antialiasing, so at this size a note is a polygon with visible steps
        down its sides, and without the shadow it reads as a hole cut in the
        page rather than an object lying on it. On the look that put the
        stage on paper those two are most of the difference between a drawing
        and a wireframe.
        """
        look = get_look()
        centre = rect.center
        r = min(rect.width, rect.height) // 2
        if glow > 0.0 and ring is not None:
            design.circle_ring(surface, centre, r + 10, ring, 2,
                               alpha=int(150 * glow))
        shapes.disc(surface, centre, r, fill, ring=ring, ring_width=ring_width,
                    shadow=design.ink(look.stage_bg, 1.0),
                    shadow_drop=max(1, round(r * SHADOW_DROP)),
                    shadow_spread=max(4, round(r * SHADOW_SPREAD)),
                    shadow_drift=round(r * SHADOW_DRIFT),
                    shadow_alpha=SHADOW_ALPHA)

    @staticmethod
    def _draw_tail(surface: pygame.Surface, head: pygame.Rect, width: int,
                   colour: tuple[int, int, int], alpha: int) -> None:
        """The bar showing how long a note is held."""
        h = max(6, head.height // 3)
        clip = surface.get_clip().clip(surface.get_rect())
        left = max(head.right - 4, clip.left)
        right = min(head.right + width, clip.right)
        if right <= left:
            return
        rect = pygame.Rect(left, head.centery - h // 2, right - left, h).clip(clip)
        if not rect.width or not rect.height:
            return
        def paint(layer):
            pygame.draw.rect(layer, design.with_alpha(colour, alpha),
                             layer.get_rect(), border_radius=h // 2)

        surface.blit(design.sprite(("tail", rect.size, colour, alpha, h),
                                   rect.size, paint), rect.topleft)

    @staticmethod
    def _draw_fret(surface: pygame.Surface, rect: pygame.Rect, fret: int,
                   size: int, colour: tuple[int, int, int],
                   outline: bool = True, fit_inside: bool = False) -> None:
        """The fret number, centred, always drawn.

        It is the one thing the player actually has to read, and it used to be
        dropped whenever the card was too narrow to contain it.
        """
        label = str(fret)
        if rect.width >= CARD_LABEL_INSIDE_W or fit_inside:
            face = design.font(size, bold=True)
            minimum = 5 if fit_inside else design.TEXT_XS
            while (face.size(label)[0] + 6 > rect.width
                   or (fit_inside and face.size(label)[1] > rect.height)) and size > minimum:
                size = max(minimum, size - 1 if fit_inside else size - 4)
                face = design.font(size, bold=True)
            surf = face.render(label, True, colour)
            pos = (rect.centerx - surf.get_width() // 2,
                   rect.centery - surf.get_height() // 2)
            halo = (245, 247, 252) if outline else None
        else:
            # Too narrow to hold the number. Putting it above keeps it
            # readable; dropping it, as the original did, removes the one
            # thing the player actually has to read.
            face = design.font(design.TEXT_SM, bold=True)
            surf = face.render(label, True, (236, 240, 248))
            pos = (rect.centerx - surf.get_width() // 2,
                   rect.y - surf.get_height() - 1)
            halo = (12, 14, 20)
        if halo is not None:
            shadow = face.render(label, True, halo)
            for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                surface.blit(shadow, (pos[0] + dx, pos[1] + dy))
        surface.blit(surf, pos)

    def _notes_due_now(self) -> dict[tuple[float, int], float]:
        """(timestamp, string) -> how lit it is, 0 to 1. Once per frame.

        It used to light a note only inside the timing window, which is about
        120 ms: seven frames, too fast to screenshot and far too late to aim
        by. The point of marking a note is to say "this one, next" while
        there is still time to get a finger there, so it comes up over most
        of a beat as the note approaches, stays full while the player can still hit
        it, and goes out shortly after.

        Only the next note on each string. In a run of sixteenths half the
        board is inside the lead at any moment, and a board where everything
        glows marks nothing.
        """
        window = max(60.0, self._timing_window_for_marker())
        lead = max(GLOW_LEAD_MIN_MS, self._beat_ms() * GLOW_LEAD_BEATS)
        tail = window + GLOW_TAIL_MS
        best: dict[int, tuple[float, float]] = {}
        for note in self._timeline.get_notes_in_range(
                self._playback_ms - tail, self._playback_ms + lead):
            if not self._note_passes_filter(note):
                continue
            offset = note.timestamp_ms - self._playback_ms
            if offset > 0:
                # Coming. Full by the time it reaches the window.
                lit = 1.0 - max(0.0, offset - window) / max(1.0, lead - window)
            else:
                lit = 1.0 - max(0.0, -offset - window) / max(1.0, tail - window)
            lit = max(0.0, min(1.0, lit))
            if lit <= 0.0:
                continue
            # The nearest one on this string wins, measured from the
            # playhead, so a note still coming does not steal the mark from
            # one the player is being judged on.
            seen = best.get(note.string)
            if seen is None or abs(offset) < seen[1]:
                best[note.string] = (note.timestamp_ms, abs(offset), lit)[:2]                     + (lit,)
        return {(stamp, string): lit
                for string, (stamp, _away, lit) in best.items()}

    def _timing_window_for_marker(self) -> float:
        if self._matcher is not None:
            return float(getattr(self._matcher, "_timing_window_ms", 120.0))
        return float(max(self._config.timing_window_ms, self._instrument.timing_window_ms))

    def _sync_timing_window(self) -> None:
        """At least the complete visible Stage note can cross the target."""
        if self._matcher is None or not callable(getattr(self._matcher, "set_timing_window", None)):
            return
        window = max(self._config.timing_window_ms, self._instrument.timing_window_ms)
        if get_look().key == "stage" and getattr(self, "_last_page", None) is not None:
            from pickhero.ui.timing_window import stage_head_window_ms
            window = max(window, stage_head_window_ms(
                self._last_page, self._lane_count, self._visible_window_ms))
        if math.isclose(window, self._matcher._timing_window_ms, abs_tol=.01):
            return
        self._matcher.set_timing_window(window)
        self._refresh_preroll_boundary()
        if self._playback_ms > self._assessment_start_ms:
            # A resized/switched view can widen its visual timing window.
            # Such attempts must not masquerade as comparable practice runs.
            self._attempt_uniform_input = False
        else:
            self._attempt_input_key = self._input_key()

    def _draw_hud(self, surface: pygame.Surface, layout: _Layout) -> None:
        """Everything outside the lanes."""
        self._draw_top_bar(surface, layout)
        self._draw_toolbar(surface, layout)
        self._draw_banners(surface, layout)
        if self._playback_ms < 0:
            self._draw_count_in(surface, layout)

    # -- Top bar --

    def _draw_top_bar(self, surface: pygame.Surface, layout: _Layout) -> None:
        """Title, position in the piece, and elapsed time.

        Each block measures itself and the middle one gives way first, because
        at 560 px the tempo used to be written straight across the subtitle.
        """
        look = get_look()
        w = layout.screen_w
        meta = self._timeline.metadata
        pygame.draw.rect(surface, design.chrome(look.bg), (0, 0, w, TOP_BAR_H))
        pygame.draw.line(surface, design.stroke(look.bg, 0),
                         (0, TOP_BAR_H - 1), (w, TOP_BAR_H - 1), 1)

        # Right block first: it is the one with a fixed width.
        # Clamped, because the clock runs a little past the last note so the
        # final strikes can still be judged. Showing 1:32 / 1:31 is nonsense.
        current = format_time(
            max(0.0, min(self._playback_ms, self._timeline.duration_ms)))
        total = format_time(self._timeline.duration_ms)
        time_label = f"{current} / {total}"
        time_w = design.font(design.TEXT_MD, mono=True).size(time_label)[0]
        right_w = max(time_w, 150) + design.SPACE_4
        design.text(surface, time_label, (w - design.SPACE_4, 10),
                    design.shade(look.hud_text, 0.85), design.TEXT_MD,
                    mono=True, align="right")
        bar_w = min(150, right_w - design.SPACE_4)
        bx = w - design.SPACE_4 - bar_w
        pygame.draw.rect(surface, design.sunken(look.bg), (bx, 38, bar_w, 6),
                         border_radius=3)
        if self._timeline.duration_ms > 0:
            frac = max(0.0, min(1.0, self._playback_ms / self._timeline.duration_ms))
            if frac > 0:
                pygame.draw.rect(surface, look.hud_accent,
                                 (bx, 38, int(bar_w * frac), 6), border_radius=3)

        # Centre block: bar number and tempo, only if it fits between the others.
        bar = self._current_bar()
        centre_lines = []
        if bar is not None:
            centre_lines.append((t("hud.bar", bar=bar), design.TEXT_LG, True,
                                 look.hud_text))
        pct = int(round(self._tempo_factor * 100))
        # Two whole lines rather than a base line with a tail glued on.
        bpm = int(round(60_000 / self._beat_ms()))
        tempo_line = (t("hud.tempo", bpm=bpm) if pct == 100
                      else t("hud.tempo_scaled", bpm=bpm, percent=pct))
        centre_lines.append((tempo_line, design.TEXT_XS, False,
                             design.shade(look.hud_text, 0.7)))
        centre_w = max(design.font(sz, bold=b).size(txt)[0]
                       for txt, sz, b, _ in centre_lines)

        title = meta.title or t("song.untitled")
        left_avail = w - right_w - centre_w - design.SPACE_8 * 2
        show_centre = left_avail > 120
        if not show_centre:
            left_avail = w - right_w - design.SPACE_4

        design.text(surface, self._ellipsise(title, design.TEXT_LG, True, left_avail),
                    (design.SPACE_4, 10), look.hud_text, design.TEXT_LG, bold=True)
        subtitle = " · ".join(x for x in (
            meta.artist or "", meta.track_name or "", self._instrument.name) if x)
        design.text(surface, self._ellipsise(subtitle, design.TEXT_XS, False, left_avail),
                    (design.SPACE_4, 36), design.shade(look.hud_text, 0.65),
                    design.TEXT_XS)

        if show_centre:
            y = 8
            for text_value, size, bold, colour in centre_lines:
                design.text(surface, text_value, (w // 2, y), colour, size,
                            bold=bold, align="center")
                y += 28 if bold else 18
            loop_info = self._loop_hud_text()
            streak = self._feedback.streak
            if self._audio_enabled and streak >= 3:
                design.text(surface, t("hud.streak", count=streak), (w // 2, y),
                            look.feedback_hit, design.TEXT_XS, bold=True,
                            align="center")
            elif loop_info:
                design.text(surface, loop_info, (w // 2, y),
                            look.hud_accent if self._loop_enabled
                            else design.shade(look.hud_text, 0.7),
                            design.TEXT_XS, align="center")

    @staticmethod
    def _ellipsise(value: str, size: int, bold: bool, max_width: float) -> str:
        """Trim text to fit, rather than letting it run under its neighbour."""
        if max_width <= 0:
            return ""
        face = design.font(size, bold=bold)
        if face.size(value)[0] <= max_width:
            return value
        out = value
        while out and face.size(out + "…")[0] > max_width:
            out = out[:-1]
        return (out + "…") if out else ""

    def _current_bar(self) -> int | None:
        for measure in self._timeline.measures:
            if measure.start_ms <= self._playback_ms <= measure.end_ms:
                return measure.index + 1
        return None

    # -- Toolbar --

    def _draw_toolbar(self, surface: pygame.Surface, layout: _Layout) -> None:
        """Labelled controls that shrink to fit.

        Single letters crammed into one status line are readable only to
        whoever wrote them. But the window is resizable, so the row has to
        give way rather than overlap: the least important controls drop out
        first, and their keyboard shortcuts keep working regardless.
        """
        look = get_look()
        w, h = layout.screen_w, layout.screen_h
        top = h - TOOLBAR_H
        pygame.draw.rect(surface, design.chrome(look.bg), (0, top, w, TOOLBAR_H))
        pygame.draw.line(surface, design.stroke(look.bg, 0), (0, top), (w, top), 1)

        y = top + 12
        bh = TOOLBAR_H - 24
        gap = design.SPACE_2
        playing = self._playing and not self._wait_mode_frozen

        # (priority, width, kind, payload). Lower priority drops out first.
        # The key-cap legends are translated (English says "Space", German
        # says "Leer") but the BINDINGS never move. A shortcut that changed
        # with the interface language would break the muscle memory the player has
        # already built. That is a decision, not an oversight.
        items: list[tuple[int, int, str, tuple]] = [
            (7, 80, "pill", (t("play.menu"), t("key.escape"), False, "menu")),
            (9, 104, "pill", (t("play.pause") if playing else t("play.play"),
                              t("key.space"), playing, "play")),
            (4, 76, "pill", (t("play.restart"), t("key.home"), False, "restart")),
            (8, 168, "tempo", ()),
            (5, 104, "toggle", (t("play.listen"), t("key.a"),
                                self._audio_enabled, "listen")),
            (3, 104, "toggle", (t("play.wait"), t("key.w"),
                                self._wait_mode, "wait")),
            (2, 108, "toggle", (t("play.loop"), t("key.i_o"),
                                self._loop_enabled, "loop")),
        ]
        if self._midi_player is not None:
            # The metronome ranks above the backing track: it is what makes
            # the timing playable, not an accompaniment.
            items.append((2, 118, "toggle",
                          (t("play.metronome"), t("key.m"),
                           self._metronome_on(), "metronome")))
            if self._backing_track_present:
                items.append((1, 120, "toggle",
                              (t("play.backing"), t("key.b"),
                               not self._backing_muted, "backing")))

        # Reserve the right-hand side for help and the input meter.
        help_w = 74
        meter_w = 190 if self._audio_capture is not None else 0
        reserved = help_w + (meter_w + gap if meter_w else 0) + design.SPACE_4 * 2
        budget = max(0, w - reserved - design.SPACE_4)

        chosen = sorted(items, key=lambda it: -it[0])
        while chosen and sum(it[1] + gap for it in chosen) - gap > budget:
            chosen.pop()
        order = {id(it): i for i, it in enumerate(items)}
        chosen.sort(key=lambda it: order[id(it)])

        self._button_rects.clear()
        x = design.SPACE_4
        for _, width, kind, payload in chosen:
            rect = pygame.Rect(x, y, width, bh)
            x += width + gap
            if kind == "tempo":
                self._draw_tempo_control(surface, rect)
                # Two halves, so the minus and the plus are separate targets.
                self._button_rects.setdefault("tempo_down", []).append(
                    pygame.Rect(rect.x, rect.y, rect.width // 2, rect.height))
                self._button_rects.setdefault("tempo_up", []).append(
                    pygame.Rect(rect.centerx, rect.y, rect.width // 2,
                                rect.height))
            elif kind == "toggle":
                label, hint, on, name = payload
                design.pill(surface, rect, label, hint)
                design.toggle_dot(surface, (rect.x + 11, rect.y + 13), on)
                self._button_rects.setdefault(name, []).append(rect)
            else:
                label, hint, active, name = payload
                design.pill(surface, rect, label, hint, active=active,
                            accent=look.hud_accent)
                self._button_rects.setdefault(name, []).append(rect)

        help_rect = pygame.Rect(w - design.SPACE_4 - help_w, y, help_w, bh)
        design.pill(surface, help_rect, t("play.help"), t("key.h"))
        self._button_rects.setdefault("help", []).append(help_rect)
        if meter_w:
            self._draw_input_meter(surface, help_rect.x - gap - meter_w,
                                   y, meter_w, bh)

    # Every name _handle_click answers to. A painter reports where it drew
    # a control under one of these; anything else it reports is drawn and
    # dead, which is the defect that made the whole transport decoration.
    HANDLED = ("menu", "play", "restart", "tempo", "tempo_down", "tempo_up",
               "listen", "wait", "loop", "loop_start", "loop_end", "loop_clear",
               "metronome", "backing", "help", "setup", "device", "check", "calibrate")

    @classmethod
    def _handled_clicks(cls) -> tuple[str, ...]:
        return cls.HANDLED

    def _pointer_pos(self) -> tuple[int, int]:
        """Where the mouse is, from the last event that said so."""
        if self._pointer is not None:
            return self._pointer
        try:
            return pygame.mouse.get_pos()
        except Exception:
            return (-1, -1)

    # How far the player has to move before a press on the dial counts as turning
    # it rather than tapping it. Below this a tap still steps by 0.05, which
    # is what it did before there was any dragging.
    DRAG_SLOP = 4
    # Pixels of travel for the whole range, bottom to top. 240 puts the full
    # quarter-speed-to-full-speed sweep inside a comfortable forearm movement
    # and still lets a 0.05 step be found: one step is 16 px.
    DRAG_TRAVEL = 240.0

    def _grab(self, pos) -> bool:
        """Take hold of the dial if the press landed on it."""
        if not self._over(pos, "tempo"):
            return False
        self._drag = (pos, self._tempo_factor)
        return True

    def _drag_to(self, pos) -> None:
        """Turn the dial while the button is down.

        Vertically, which is how every dial in every audio application works
        and is the only mapping that stays usable on a small knob: following
        the angle instead means the value leaps the moment the pointer
        crosses the centre, and the centre is where their hand already is.
        """
        if self._drag is None:
            return
        (start_x, start_y), began = self._drag
        if abs(pos[1] - start_y) < self.DRAG_SLOP and                 abs(pos[0] - start_x) < self.DRAG_SLOP:
            return
        # Up is faster, which matches the dial's own tick sweeping clockwise.
        moved = (start_y - pos[1]) / self.DRAG_TRAVEL
        self.set_tempo_factor(began + moved * (1.0 - TEMPO_MIN))

    def _drop(self, pos) -> str | None:
        """Let go. A press that never moved was a tap after all."""
        if self._drag is None:
            return None
        (start_x, start_y), _began = self._drag
        self._drag = None
        if abs(pos[0] - start_x) < self.DRAG_SLOP and                 abs(pos[1] - start_y) < self.DRAG_SLOP:
            return self._handle_click(pos)
        return None

    def _over(self, pos, *names: str) -> bool:
        """Whether `pos` is on any rect reported under any of `names`.

        A name holds a list of rects. Treating it as one was how the wheel
        over the tempo control took the app down with an AttributeError --
        the same list change that broke the settings, in the one handler
        nothing swept.
        """
        for name in names:
            for rect in self._button_rects.get(name, ()):
                if rect.collidepoint(pos):
                    return True
        return False

    def _handle_click(self, pos) -> str | None:
        """Everything the toolbar draws is a control, so everything is a target.

        The screen ignored every mouse event. The whole row -- play, restart,
        listen, wait, loop, the metronome, and a tempo readout with a minus
        and a plus drawn either side of it -- looked like controls and was
        decoration. Anyone who reached for the plus and got nothing would
        reasonably conclude the tempo could not be changed at all, which is
        what happened.
        """
        # An unknown name used to swallow the click: the loop fell into the
        # chain below, matched nothing, and returned. A look that marks a
        # decorative region -- Hi-fi marks its board, Stage its banner --
        # then covered every control underneath it.
        panel = self._active_panel()
        if panel is not None:
            event = pygame.event.Event(pygame.MOUSEBUTTONDOWN, button=1, pos=pos)
            return self._panel_action(panel.handle_event(event))
        handled = set(self.HANDLED)
        for name, rects in reversed(list(self._button_rects.items())):
            if name not in handled:
                continue
            if not any(rect.collidepoint(pos) for rect in rects):
                continue
            if name == "menu":
                self.stop_audio()
                return "menu"
            if name == "play":
                self.toggle_play()
            elif name == "restart":
                self.seek(0)
            elif name == "tempo_down":
                self.set_tempo_factor(self._tempo_factor - 0.05)
            elif name == "tempo_up":
                self.set_tempo_factor(self._tempo_factor + 0.05)
            elif name == "tempo":
                # One dial rather than a minus and a plus, which is what
                # Hi-fi draws. It turns the way a real one does: the left of
                # it anticlockwise, the right of it clockwise.
                hit = next(r for r in rects if r.collidepoint(pos))
                step = -0.05 if pos[0] < hit.centerx else 0.05
                self.set_tempo_factor(self._tempo_factor + step)
            elif name == "listen":
                self._toggle_audio()
            elif name == "wait":
                self._toggle_wait_mode()
            elif name == "loop":
                self._toggle_loop()
            elif name == "loop_start":
                self._set_loop_start(self._playback_ms)
            elif name == "loop_end":
                self._set_loop_end(self._playback_ms)
            elif name == "loop_clear":
                self._clear_loop()
            elif name == "metronome":
                self._toggle_metronome()
            elif name == "backing":
                self._toggle_backing()
            elif name == "help":
                self._open_help()
            elif name == "setup":
                self._open_setup()
            elif name in ("device", "check", "calibrate"):
                self.suspend_for_setup()
                return name
            return None
        return None

    def _draw_tempo_control(self, surface: pygame.Surface, rect: pygame.Rect) -> None:
        look = get_look()
        pygame.draw.rect(surface, design.surface(look.bg, 1), rect,
                         border_radius=design.RADIUS_MD)
        pygame.draw.rect(surface, design.stroke(look.bg, 1), rect, width=1,
                         border_radius=design.RADIUS_MD)
        design.text(surface, "−", (rect.x + 14, rect.centery - 13),
                    design.shade(look.hud_text, 0.85), design.TEXT_LG, bold=True)
        design.text(surface, "+", (rect.right - 26, rect.centery - 13),
                    design.shade(look.hud_text, 0.85), design.TEXT_LG, bold=True)
        design.text(surface,
                    t("hud.percent", percent=int(round(self._tempo_factor * 100))),
                    (rect.centerx, rect.y + 9), look.hud_text,
                    design.TEXT_SM, bold=True, align="center")
        design.text(surface, t("play.tempo_hint"), (rect.centerx, rect.y + 31),
                    design.shade(look.hud_text, 0.55), design.TEXT_XS, align="center")

    def _draw_input_meter(self, surface: pygame.Surface, x: int, y: int,
                          width: int, height: int) -> None:
        """Microphone level and the note being heard.

        Small while everything is fine. When it is not, the banner above takes
        over, because "too quiet" is the failure that makes a beginner blame
        themselves.
        """
        look = get_look()
        design.text(surface, t("play.microphone"), (x, y),
                    design.shade(look.hud_text, 0.55),
                    design.TEXT_XS)
        db = self._signal_db_smooth
        gate = self._noise_gate_db
        bar = pygame.Rect(x, y + 18, width, 10)
        pygame.draw.rect(surface, design.sunken(look.bg), bar, border_radius=5)
        frac = max(0.0, min(1.0, (db + 60.0) / 60.0))
        colour = (look.feedback_hit if db >= gate
                  else design.shade(look.hud_text, 0.45))
        if db > -3.0:
            colour = look.feedback_miss
        if frac > 0:
            pygame.draw.rect(surface, colour,
                             (bar.x, bar.y, int(bar.width * frac), bar.height),
                             border_radius=5)
        gx = bar.x + int(bar.width * max(0.0, min(1.0, (gate + 60.0) / 60.0)))
        pygame.draw.line(surface, design.shade(look.hud_text, 0.8),
                         (gx, bar.y - 2), (gx, bar.bottom + 2), 2)

        note = "—"
        if self._tuner_displayed_note > 0:
            note = midi_to_name(self._tuner_displayed_note)
            _, cents = freq_to_cents_deviation(self._tuner_freq_smooth)
            note = f"{note}  {cents:+.0f}¢"
        design.text(surface, note, (x + width, y), design.shade(look.hud_text, 0.85),
                    design.TEXT_XS, mono=True, align="right")

    # -- Banners --

    def _current_warning(self) -> str | None:
        """Which warning the strip should show, asked exactly once per frame.

        _quiet_warning() is not a question, it is a timer: it records when the
        input first went quiet. Calling it a second time inside one frame is
        therefore not free, which is why the answer is computed in render()
        and stored rather than fetched wherever it is wanted.
        """
        save_error = getattr(self._progress_tracker, "last_save_error", None)
        if save_error in ("write", "capacity"):
            return t("passage.progress_" + save_error)
        message = t("quality." + self._attempt_issues[0]) if self._attempt_issues else self._tuning_warning
        if message is None:
            message = self._unplayable_warning()
        if message is None:
            message = self._quiet_warning()
        if message is None:
            message = self._high_gate_warning()
        return message

    def _draw_banners(self, surface: pygame.Surface, layout: _Layout) -> None:
        if self._banner_message is None:
            return
        rect = pygame.Rect(0, TOP_BAR_H, layout.screen_w, BANNER_H)
        design.banner(surface, rect, self._banner_message)

    def _unplayable_warning(self) -> str | None:
        """Say how much of the tab lies past the end of the fretboard.

        Those notes are not drawn and not scored. Dropping them silently makes
        a piece look easier than it is and leaves gaps the player cannot
        explain.
        """
        if self._unplayable_count <= 0:
            return None
        # One key with a plural dict, not a hand-rolled singular branch: a
        # language with three plural forms cannot be expressed by an if.
        return t("play.unplayable", count=self._unplayable_count)

    def _high_gate_warning(self) -> str | None:
        """Say that the gate is set high here, not only on the check screen.

        The check screen has shown this all along -- but only to someone who
        went looking for it. Measured on a real take: a gate of -26 dB against
        plucks peaking between -8 and -32 dB swallowed a third of the low E
        strikes, and the score on the same piece fell by more than half.
        Nothing said why.

        _quiet_warning cannot cover this. It resets the instant the signal
        clears the gate, so a take whose loud notes are fine and whose quiet
        ones are lost never trips it: the average looks healthy while half the
        notes vanish. The low strings go first, being quietest at a room mic.

        The threshold is check_menu's, imported rather than copied so the two
        screens cannot come to disagree about what counts as high.
        """
        from pickhero.ui.check_menu import HIGH_GATE_DB

        if not self._audio_enabled or self._audio_capture is None:
            return None
        gate = self._noise_gate_db
        if gate is None or gate < HIGH_GATE_DB:
            return None
        return t("play.high_gate", gate=f"{gate:.0f}")

    def _quiet_warning(self) -> str | None:
        """Say so when the microphone has heard nothing for a while.

        Silence during a passage of notes looks exactly like playing badly,
        and a beginner will assume it is their fault. It is usually the level.
        """
        if not self._audio_enabled or self._audio_capture is None:
            self._quiet_since = None
            return None
        if not self._playing or self._playback_ms < self._count_in_target_ms:
            self._quiet_since = None
            return None
        if not self._timeline.get_notes_in_range(
                self._playback_ms - 3000.0, self._playback_ms + 500.0):
            self._quiet_since = None
            return None
        now = time.perf_counter()
        if self._signal_db_smooth >= self._noise_gate_db:
            self._quiet_since = None
            return None
        if self._quiet_since is None:
            self._quiet_since = now
            return None
        if now - self._quiet_since < QUIET_WARN_SECONDS:
            return None
        return t("play.too_quiet")

    # -- Count-in --

    def _draw_count_in(self, surface: pygame.Surface, layout: _Layout) -> None:
        """The moment a beginner is most likely to lose the thread."""
        look = get_look()
        beat = self._beat_ms()
        if beat <= 0:
            return
        remaining = -self._playback_ms
        number = max(1, int(math.ceil(remaining / beat)))
        cx = int(layout.gutter_x + (layout.screen_w - layout.gutter_x) / 2)
        cy = int((layout.lane_top + layout.lane_bottom) / 2)

        size = (layout.screen_w, int(layout.lane_bottom - layout.lane_top))
        tint = design.with_alpha(design.scrim(look.bg), 190)
        surface.blit(design.sprite(("veil", size, tint), size,
                                   lambda layer: layer.fill(tint)),
                     (0, int(layout.lane_top)))

        # The ring empties over the current beat, so the count is felt as well
        # as read.
        phase = 1.0 - ((remaining % beat) / beat)
        design.circle_ring(surface, (cx, cy), 84, design.shade(look.hud_accent, 0.5), 4)
        design.circle_ring(surface, (cx, cy), 84 - int(6 * phase), look.hud_accent, 4)
        design.text(surface, str(number), (cx, cy - 46), look.hud_text,
                    design.TEXT_3XL, bold=True, align="center")
        design.text(surface, t("play.count_in"), (cx, cy + 108),
                    design.shade(look.hud_text, 0.8), design.TEXT_MD, align="center")

    def _timing_advice(self, stats: dict) -> str:
        """One sentence about their pulse, or nothing.

        Only said when there is enough to say it from: a median over three
        notes is noise, and being told to correct a fault the player does not have
        is worse than being told nothing.
        """
        median = stats.get("timing_median_ms")
        if (median is None or stats.get("timing_measured", stats.get("hits", 0)) < 6
                or stats.get("timing_unknown", 0)
                or not self._timing_assessed or self._attempt_issues):
            return ""
        if not self._attempt_uniform_tempo:
            # There is no single conversion from a mixed-tempo song median
            # to elapsed milliseconds. Keep the measured in-time count.
            return t("play.timing_varied")
        elapsed_median = median / self._attempt_tempo_factor
        inside = abs(median) <= self._timing_window_for_marker()
        if inside and abs(elapsed_median) <= 25:
            # Opposite errors can cancel in the median. A centred average
            # alone must not call a scattered performance steady.
            return t("play.timing_solid" if stats.get("in_time", 0) / stats["hits"] >= 0.8
                     else "play.timing_mixed")
        if inside:
            key = "play.timing_within_early" if median < 0 else "play.timing_within_late"
        else:
            key = "play.timing_early" if median < 0 else "play.timing_late"
        return t(key, ms=abs(int(round(elapsed_median))))

    def _log_result(self, stats: dict) -> None:
        """One line per run, where it can be read afterwards.

        Nobody can watch them play. The log is how a real session -- their own
        hands, their own room, their own microphone -- reaches anyone who has to
        work out why something is wrong.
        """
        self._test_recording.result(stats)
        try:
            median = stats.get("timing_median_ms")
            log_error(
                "Durchgang: {song} | {hits}/{total} Toene gefunden, "
                "{in_time} im Takt, {early} zu frueh, {late} zu spaet, "
                "{close} knapp, {misses} verfehlt | Median {median} | "
                "Tempo {tempo}% | {instrument} @ {rate} Hz | "
                "look={look} timing_window_song_ms={window:g} scoring_policy=2 "
                "timing_measured={measured} timing_unknown={unknown}".format(
                    song=self._song_key or self._timeline.metadata.title or "?",
                    hits=stats.get("hits", 0), total=stats.get("total", 0),
                    in_time=stats.get("in_time", 0), early=stats.get("early", 0),
                    late=stats.get("late", 0), close=stats.get("close", 0),
                    misses=stats.get("misses", 0),
                    measured=stats.get("timing_measured", stats.get("hits", 0)),
                    unknown=stats.get("timing_unknown", 0),
                    median=("-" if median is None else f"{median:+.0f} ms"),
                    tempo=int(round(self._tempo_factor * 100)),
                    instrument=self._instrument.key,
                    look=get_look().key, window=self._timing_window_for_marker(),
                    rate=(self._audio_capture.sample_rate
                          if self._audio_capture is not None else "?"),
                )
                + (" | audio_timing=" + json.dumps(self._audio_capture.timing_diagnostics(),
                                                  sort_keys=True)
                   if self._audio_capture is not None
                   and callable(getattr(self._audio_capture, "timing_diagnostics", None)) else "")
            )
        except Exception as exc:
            log_error(f"Durchgang konnte nicht protokolliert werden: {exc}")

    def _pause_for_panel(self) -> bool:
        """Pause at the last processed clock point, without resetting assessment."""
        was_playing = self._playing
        if was_playing:
            self.update()
            if self._playing:
                self.toggle_play()
        self._drag = None
        return was_playing and not self._song_completed

    def _active_panel(self) -> PracticePanel | None:
        if self._show_help and (self._panel is None or self._panel.kind != "help"):
            self._help_was_playing = self._pause_for_panel()
            self._panel = self._help_panel()
        elif not self._show_help and self._song_completed and self._panel is None:
            self._panel = self._result_panel()
        return self._panel

    def _open_help(self) -> None:
        self._panel = None
        self._show_help = True
        self._active_panel()

    def _open_setup(self) -> None:
        self._close_panel_resources()
        self._pause_for_panel()
        self._show_help = False
        self._help_was_playing = False
        sections = [("", [t("practice.panel_keys")])]
        if self._next_lesson_practice is not None:
            sections.append((t("practice.result.next_step"), [self._next_lesson_hint()]))
        self._panel = PracticePanel("setup", t("practice.setup"),
            t("practice.setup.subtitle"), sections, [
                Action("device", t("menu.device"), pygame.K_d),
                Action("check", t("menu.check"), pygame.K_c),
                Action("calibrate", t("menu.tuner"), pygame.K_k),
                (Action("next_lesson", t("practice.next")) if self._next_lesson_practice is not None
                 else Action("passages", t("passage.button"), pygame.K_n)),
                Action("wizard", t("passage.guide_short"), pygame.K_F2),
                Action("close", t("passage.back"))])

    def _help_panel(self) -> PracticePanel:
        strings = [t("practice.help.string", string=string, note=midi_to_name(midi))
                   for string, midi in sorted(self._tuning.items())]
        controls = [t("play.help_keys_1"), t("play.help_keys_2"),
                    t("play.help_keys_3"), t("play.help_keys_4", count=min(12, self._lane_count)),
                    t("play.help_keys_5"), t("practice.help.loop_keys"),
                    t("practice.help.setup_keys"), t("passage.help"), t("practice.panel_keys")]
        panel = PracticePanel("help", t("play.help_title"), t("practice.help.subtitle"), [
            (t("play.help_reading"), [t("practice.help.reading_" + get_look().key),
                                    t("play.help_track_2"), t("play.help_colour"),
                                    t("play.help_dimmed")]),
            (t("play.help_rows", count=self._lane_count, instrument=self._instrument.short_name),
             [t("play.help_rows_intro")] + strings),
            (t("play.help_scoring"), [t("play.help_scoring_intro"), t("play.help_hit"),
                                     t("play.help_close"), t("play.help_miss")]),
            (t("play.help_controls"), controls)], [
                Action("close", t("practice.resume") if self._help_was_playing else t("practice.close")),
                Action("history", t("session.history")),
                Action("timing", t("timing.button"), pygame.K_F3),
                Action("passages", t("passage.button"), pygame.K_n),
                Action("wizard", t("passage.guide_short"), pygame.K_F2),
                Action("setup", t("practice.setup"), pygame.K_s)])
        panel.header_action = Action("record", t("record.header"), pygame.K_u)
        return panel

    def _history_panel(self) -> PracticePanel:
        record = self._progress_tracker.get_best(self._song_key) if self._progress_tracker and self._song_key else None
        entries = record.section_attempts if record is not None else []
        lines = [t("session.history_entry", start=format_time(entry["start_ms"]),
                   end=format_time(entry["end_ms"]), tempo=round(entry["tempo_factor"] * 100),
                   pitch=round(entry["accuracy"]),
                   timing=(t("hud.percent", percent=round(entry["timing_percent"]))
                           if entry["timing_assessed"] else "—"),
                   status=(t("play.timing_unknown_count", count=entry["timing_unknown"])
                           if entry.get("timing_unknown", 0) else
                           t("session.history_valid" if entry["reliable"] and entry["timing_assessed"]
                             else "session.history_unassessed"))) for entry in reversed(entries)]
        return PracticePanel("history", t("session.history"), self._timeline.metadata.title,
                             [("", lines or [t("session.history_empty")])],
                             [Action("close", t("session.back_help"))])

    def _result_panel(self) -> PracticePanel:
        sections = []
        metrics = []
        save_error = getattr(self._progress_tracker, "last_save_error", None)
        if save_error in ("write", "capacity"):
            sections.append((t("passage.error"), [t("passage.progress_" + save_error)]))
        if self._attempt_issues:
            sections.append((t("session.unreliable"), [t("session.unreliable_hint")]
                             + [t("quality." + reason) for reason in self._attempt_issues]))
        if self._audio_enabled and self._matcher is not None:
            stats = self._matcher.get_statistics()
            metrics.append((t("practice.result.pitch"), f"{stats['accuracy_percent']:.0f}%", ""))
            if self._timing_assessed:
                measured = stats.get("timing_measured", stats["hits"])
                unknown = stats.get("timing_unknown", 0)
                metrics.append((t("play.timing_unknown") if unknown and not measured
                                else t("play.in_time", in_time=stats['in_time'], total=measured),
                                f"{stats['in_time']} / {measured}" if measured else "—",
                                t("play.timing_unknown_count", count=unknown) if unknown
                                else self._timing_advice(stats) or ""))
                if unknown:
                    sections.append((t("play.timing_unknown"), [t("play.timing_unknown_help")]))
            else:
                metrics.append((t("practice.result.timing_unassessed"), "—",
                                t("play.help_keys_5")))
            tally = [t("play.tally", hits=stats["hits"], close=stats["close"], misses=stats["misses"])]
            if self._is_new_best and not save_error:
                tally.append(t("play.new_best"))
            sections.append(("", tally))
        else:
            sections.append((t("play.unscored"), [t("play.unscored_hint")]))
        if self._next_lesson_practice is not None:
            sections.append((t("practice.result.next_step"), [self._next_lesson_hint()]))
        next_steps = []
        weak = getattr(self, "_weakest_sections", [])
        if weak:
            start, end, accuracy = weak[0]
            # The assessment describes the actual weak bars; the practice
            # action may add neighbouring bars to give the player context.
            if start == end:
                next_steps.append(t("play.weakest_single", bar=start + 1,
                                    percent=f"{accuracy:.0f}"))
            else:
                next_steps.append(t("play.weakest", start=start + 1, end=end + 1,
                                    percent=f"{accuracy:.0f}"))
        next_steps.extend(self._recommendations)
        if next_steps:
            heading = t("practice.repeat_optional") if self._lesson or self._music_piece else t("practice.result.next_step")
            sections.append((heading, next_steps))
        actions = []
        if self._next_lesson_practice is not None:
            actions.append(Action("next_lesson", t("practice.next")))
        elif self._lesson is not None or self._music_piece is not None:
            actions.append(Action("lessons", t("session.lessons")))
        actions.append(Action("replay", t("practice.replay"), pygame.K_HOME))
        if weak and self._timeline.measures:
            actions.append(Action("weak", t("practice.weak"), pygame.K_l))
        actions.append(Action("menu", t("practice.menu")))
        actions.append(Action("timing", t("timing.button"), pygame.K_F3))
        if self._test_recording.take or self._test_recording.error:
            sections.append((t("record.title"), [t("record.result_hint")]))
            actions.append(Action("record", t("record.button"), pygame.K_u))
        title = t("session.uncertain_title") if self._attempt_issues else t("play.done")
        panel = PracticePanel("result", title, self._timeline.metadata.title,
                              sections, actions)
        panel.metrics = metrics
        return panel

    def _next_lesson_hint(self) -> str:
        from pickhero.curriculum import lesson_for_path
        request = self._next_lesson_practice
        if request is None:
            return ""
        if request.path.resolve() == Path(self._song_path).resolve():
            return t("practice.next_step_hint", title=request.step.title)
        lesson = lesson_for_path(request.path)
        if lesson is None:
            from pickhero.music import music_for_path
            piece, version = music_for_path(request.path)
            return t("music.next_hint", title=piece.title, version=t(f"music.version.{version}"))
        return t("practice.next_lesson_hint", number=f"{lesson.order:02d}", title=lesson.title)

    def _panel_action(self, action: str | None) -> str | None:
        if action is None:
            return None
        if self._test_recording.action(action):
            return None
        kind = self._panel.kind if self._panel else ""
        if action == "close":
            self._panel = None
            if kind == "help":
                self._show_help = False
                resume, self._help_was_playing = self._help_was_playing, False
                if resume and not self._song_completed:
                    self.toggle_play()
            elif kind == "history":
                self._show_help = True
                self._panel = self._help_panel()
            elif kind == "result":
                self.stop_audio()
                return "menu"
            elif kind == "loop_progress":
                self._start_practice(self._loop_start_ms or 0.0, force_count_in=True)
        elif action == "setup":
            self._open_setup()
        elif action == "history":
            self._show_help = False
            self._panel = self._history_panel()
        elif action == "passages":
            self._open_passages()
        elif action.startswith("open_passage:"):
            identifier = action.split(":", 1)[1]
            item = next((item for item in self._progress_tracker.saved_passages(self._song_key)
                         if item["id"] == identifier), None)
            if item and 0 <= item["state"]["loop_start_ms"] < item["state"]["loop_end_ms"] <= self._timeline.duration_ms:
                self._panel.close()
                state = dict(item["state"], song_path=self._song_path)
                self._panel = None
                self.restore_practice_state(state)
            else:
                self._panel = PracticePanel("passages", t("passage.title"), self._timeline.metadata.title,
                    [("", [t("passage.error.state")])], [Action("close", t("passage.close"))])
        elif action == "timing":
            # App snapshots the capture before releasing the input stream.
            return action
        elif action in ("device", "check", "calibrate", "wizard"):
            self.suspend_for_setup()
            return action
        elif action == "replay":
            self._panel = None
            self._loop_start_ms = self._loop_end_ms = None
            self._loop_enabled = False
            self._start_practice(0.0)
        elif action == "weak":
            self._loop_weakest_section()
        elif action in ("accept_tempo", "keep_tempo"):
            if action == "accept_tempo" and self._suggested_tempo is not None:
                self.set_tempo_factor(self._suggested_tempo)
            self._panel = None
            self._suggested_tempo = None
            self._start_practice(self._loop_start_ms or 0.0, force_count_in=True)
        elif action == "smaller_passage" and self._shorter_passage is not None:
            self._loop_start_ms, self._loop_end_ms = self._shorter_passage
            self._loop_enabled = True
            self._panel = None
            self._suggested_tempo = None
            self._start_practice(self._loop_start_ms, force_count_in=True)
        elif action == "next_lesson" and self._next_lesson_practice is not None:
            self._panel = None
            return "next_lesson"
        elif action == "lessons":
            self._panel = None
            self.stop_audio()
            return "lessons"
        elif action == "menu":
            self._panel = None
            self.stop_audio()
            return "menu"
        return None

    def _open_passages(self) -> None:
        from pickhero.ui.passage_menu import PassagePanel
        self._pause_for_panel()
        self._show_help = False
        self._help_was_playing = False
        if self._progress_tracker is None or not self._song_key or not self._song_path:
            self._panel = PracticePanel("passages", t("passage.title"), self._timeline.metadata.title,
                [("", [t("passage.error.state")])], [Action("close", t("passage.close"))])
            return
        self._panel = PassagePanel(self._progress_tracker, self._song_key,
                                   self._named_passage_state(), self._timeline.metadata.title,
                                   input_key=self._input_key())

    def _named_passage_state(self) -> dict:
        start, end = self._loop_start_ms, self._loop_end_ms
        if start is None or end is None or not 0 <= start < end <= self._timeline.duration_ms:
            measures = self._timeline.measures
            if not measures:
                return {}
            index = next((i for i, measure in enumerate(measures)
                          if measure.start_ms <= max(0, self._playback_ms) < measure.end_ms),
                         len(measures) - 1)
            first = min(index, max(0, len(measures) - 2))
            last = min(len(measures) - 1, first + 1)
            start, end = measures[first].start_ms, measures[last].end_ms
        return {"song_path": self._song_path, "instrument": self._config.instrument,
                "track_index": self._track_index, "position_ms": start,
                "loop_start_ms": start, "loop_end_ms": end, "loop_enabled": True,
                "tempo_factor": self._tempo_factor, "wait_mode": self._wait_mode,
                "active_strings": list(self._active_strings), "max_fret": self._max_fret,
                "chord_partial_credit": self._chord_partial_credit, "completed": False}

    def _smaller_loop(self):
        if self._loop_start_ms is None or self._loop_end_ms is None:
            return None
        measures = self._timeline.measures
        available = [i for i, m in enumerate(measures)
                     if m.start_ms >= self._loop_start_ms and m.end_ms <= self._loop_end_ms]
        if len(available) <= 2:
            return None
        weak = self._matcher.get_weakest_sections() if self._matcher is not None else []
        start = next((int(entry[0]) for entry in weak if int(entry[0]) in available), available[0])
        start = max(available[0], min(start, available[-1] - 1))
        return measures[start].start_ms, measures[start + 1].end_ms

    def _advice_panel(self, suggested):
        easing = self._section_advice and self._section_advice["kind"] == "ease"
        actions = []
        if not easing and self._next_lesson_practice is not None:
            actions.append(Action("next_lesson", t("practice.next")))
        if suggested != self._tempo_factor:
            actions.append(Action("accept_tempo", t("passage.slower") if easing
                                  else t("session.accept_tempo")))
        if easing and self._shorter_passage is not None:
            actions.append(Action("smaller_passage", t("passage.smaller"), pygame.K_l))
        actions.extend([Action("keep_tempo", t("session.keep_tempo")),
                        Action("menu", t("practice.menu"))])
        lines = [t("passage.ease_hint") if easing else t("session.tempo_offer",
                    current=round(self._tempo_factor * 100), target=round(suggested * 100))]
        if easing and suggested != self._tempo_factor:
            lines.append(t("passage.slower_hint", current=round(self._tempo_factor * 100),
                           target=round(suggested * 100)))
        if easing and self._shorter_passage is not None:
            lines.append(t("passage.smaller_hint", start=format_time(self._shorter_passage[0]),
                           end=format_time(self._shorter_passage[1])))
        panel = PracticePanel("loop_progress", t("passage.ease_title") if easing else t("session.stable_loops"),
                             self._timeline.metadata.title, [("", lines)], actions)
        # Nothing changes until a deliberate click/key; Enter follows the
        # highlighted keep option instead of accepting a recommendation.
        for index, action in enumerate(actions):
            if action.name == "keep_tempo":
                panel.focus = index
        return panel

    def suspend_for_setup(self) -> None:
        """Release the microphone, keeping the same matcher and song position."""
        self._close_panel_resources()
        self._pause_for_panel()
        self._stop_audio()
        self._audio_capture = None
        self._setup_suspended = True
        self._panel = None
        self._show_help = False
        self._help_was_playing = False

    def resume_from_setup(self, config: Config | None = None) -> None:
        """Apply new input settings; returning to a song always stays paused."""
        if config is not None:
            self._config = config
        self._stop_audio()
        self._audio_capture = None
        gate = self._config.audio.noise_gate_db
        self._noise_gate_db = self._instrument.noise_gate_db if gate is None else gate
        self._playing = False
        self._last_tick = None
        self._setup_suspended = False
        self._panel = None

    def _start_practice(self, start_ms: float, *, force_count_in: bool = False) -> None:
        if self._playing:
            self.toggle_play()
        self.seek(start_ms)
        self._count_in_target_ms = self._playback_ms
        self._count_in_beat_ms = self._timeline.beat_duration_at(start_ms)
        beats = max(0, self._config.count_in_beats)
        if force_count_in and not beats:
            beats = 4
        self._count_in_ms = beats * self._count_in_beat_ms
        self._playback_ms = self._count_in_target_ms - self._count_in_ms
        self._last_count_in_beat = -1
        self._last_beat_played = -1
        self._playing = True
        self._last_tick = time.perf_counter()
        if self._midi_player is not None:
            self._midi_player.pause()
        if self._audio_enabled:
            self._start_audio()
        if not beats and self._midi_player is not None:
            self._midi_player.seek(start_ms)

    def _draw_completion_overlay(self, surface: pygame.Surface, layout: _Layout) -> None:
        self._result_panel().render(surface)

    def _draw_help_overlay(self, surface: pygame.Surface, layout: _Layout) -> None:
        self._help_panel().render(surface)

    # -- Difficulty filter --

    def _cycle_fret_limit(self) -> None:
        """Cycle through fret limit options."""
        limits = [f for f in FRET_LIMITS if f <= self._instrument.max_fret]
        if self._instrument.max_fret not in limits:
            limits = [self._instrument.max_fret] + limits
        try:
            idx = limits.index(self._max_fret)
            self._max_fret = limits[(idx + 1) % len(limits)]
        except ValueError:
            self._max_fret = limits[0]
        self._unplayable_count = sum(
            1 for note in self._timeline.notes if note.fret > self._max_fret
        )
        self._config.max_fret = self._max_fret
        self._config.save()
        self._reset_matcher_for_filter()
        self.save_practice_state()

    def _toggle_string(self, string: int) -> None:
        """Toggle a string on/off in the difficulty filter."""
        idx = string - 1
        if not 0 <= idx < len(self._active_strings):
            return  # F5/F6 on a 4-string instrument
        self._active_strings[idx] = not self._active_strings[idx]
        # Don't allow all strings to be off
        if not any(self._active_strings):
            self._active_strings[idx] = True
            return
        self._config.active_strings = list(self._active_strings)
        self._config.save()
        self._reset_matcher_for_filter()
        self.save_practice_state()

    def _reset_matcher_for_filter(self) -> None:
        """Reset matcher when filter changes mid-song."""
        if self._song_completed:
            self.seek(0.0)
        else:
            self._begin_attempt(max(0.0, self._playback_ms))
        if self._matcher:
            self._matcher.note_filter = self._note_passes_filter

    def _filter_hud_text(self) -> str | None:
        """Return difficulty filter text for HUD, or None if default."""
        parts = []
        # Compare against the instrument's own fret count, not a guitar's 24,
        # otherwise a bass ukulele always looks like it has a filter applied.
        if self._max_fret < self._instrument.max_fret:
            parts.append(f"Fret: 0-{self._max_fret}")
        if not all(self._active_strings):
            strs = " ".join(
                str(i + 1) if on else "_"
                for i, on in enumerate(self._active_strings)
            )
            parts.append(f"Strings: {strs}")
        return "  |  ".join(parts) if parts else None

    # -- Look --

    def _cycle_look(self) -> None:
        """Toggle between dark and light theme."""
        name = cycle_look()
        self._config.look = name
        self._config.save()

    # -- Chord mode --

    def _toggle_chord_mode(self) -> None:
        """Toggle chord partial credit on/off."""
        self._chord_partial_credit = not self._chord_partial_credit
        self._config.chord_partial_credit = self._chord_partial_credit
        self._config.save()
        if self._matcher:
            self._matcher.chord_partial_credit = self._chord_partial_credit
        self._reset_matcher_for_filter()
        self.save_practice_state()

    # -- Wait mode --

    def _toggle_wait_mode(self) -> None:
        """Toggle wait mode on/off."""
        self._wait_mode = not self._wait_mode
        self._attempt_uniform_wait = False
        if self._wait_mode:
            # Frozen detections are pinned to the written note. Their timing
            # cannot justify a speed increase, even if wait is later disabled.
            self._timing_assessed = False
        self._config.wait_mode = self._wait_mode
        self._config.save()
        if not self._wait_mode:
            self._release_wait()
        self.save_practice_state()

    def _release_wait(self) -> None:
        """Resume both clocks after either playing the note or disabling wait."""
        if not self._wait_mode_frozen:
            return
        self._wait_mode_frozen = False
        self._reanchor_audio()
        self._last_tick = time.perf_counter() if self._playing else None
        if self._midi_player is not None:
            self._midi_player.seek(self._playback_ms)

    # -- Loop weakest section --

    def _weak_measure_bounds(self) -> tuple[int, int]:
        measures = self._timeline.measures
        start, end, _ = self._weakest_sections[0]
        start = max(0, min(start, len(measures) - 1))
        end = min(len(measures) - 1, max(start + 1, min(end, start + 3)))
        if end == start and start:
            start -= 1
        return start, end

    def _loop_weakest_section(self) -> None:
        """Start two to four weak bars with a fresh, audible count-in."""
        if not getattr(self, "_weakest_sections", []) or not self._song_completed:
            return
        measures = self._timeline.measures
        if not measures:
            return
        first, last = self._weak_measure_bounds()
        self._loop_start_ms = measures[first].start_ms
        self._loop_end_ms = measures[last].end_ms
        self._loop_enabled = True
        self._panel = None
        self._show_help = False
        self._start_practice(self._loop_start_ms, force_count_in=True)

    # -- Loop control --

    def _set_loop_start(self, ms: float) -> None:
        """Set loop start marker. Auto-swap if after end, auto-enable when both set."""
        self._loop_start_ms = max(0.0, ms)
        if self._timeline.duration_ms > 0:
            self._loop_start_ms = min(self._loop_start_ms, self._timeline.duration_ms)
        if self._loop_end_ms is not None and self._loop_start_ms > self._loop_end_ms:
            self._loop_start_ms, self._loop_end_ms = self._loop_end_ms, self._loop_start_ms
        self._enforce_min_loop()
        if self._loop_start_ms is not None and self._loop_end_ms is not None:
            self._loop_enabled = True
            self.seek(self._loop_start_ms)
        self.save_practice_state()

    def _set_loop_end(self, ms: float) -> None:
        """Set loop end marker. Auto-swap if before start, auto-enable when both set."""
        self._loop_end_ms = max(0.0, ms)
        if self._timeline.duration_ms > 0:
            self._loop_end_ms = min(self._loop_end_ms, self._timeline.duration_ms)
        if self._loop_start_ms is not None and self._loop_end_ms < self._loop_start_ms:
            self._loop_start_ms, self._loop_end_ms = self._loop_end_ms, self._loop_start_ms
        self._enforce_min_loop()
        if self._loop_start_ms is not None and self._loop_end_ms is not None:
            self._loop_enabled = True
            self.seek(self._loop_start_ms)
        self.save_practice_state()

    def _enforce_min_loop(self) -> None:
        """Ensure loop region is at least one beat long."""
        if self._loop_start_ms is not None and self._loop_end_ms is not None:
            beat_ms = self._timeline.beat_duration_at(self._loop_start_ms)
            if self._loop_end_ms - self._loop_start_ms < beat_ms:
                self._loop_end_ms = self._loop_start_ms + beat_ms
            duration = self._timeline.duration_ms
            if duration > 0 and self._loop_end_ms > duration:
                self._loop_end_ms = duration
                self._loop_start_ms = max(0.0, min(self._loop_start_ms, duration - beat_ms))

    def _toggle_loop(self) -> None:
        """Enable/disable a reusable passage; create two bars if unmarked."""
        if self._loop_enabled:
            self._loop_enabled = False
            self._begin_attempt(max(0.0, self._playback_ms))
            self._full_song_attempt = False
            self.save_practice_state()
            return
        if self._loop_start_ms is None or self._loop_end_ms is None:
            measures = self._timeline.measures
            if measures:
                index = next((i for i, measure in enumerate(measures)
                              if measure.start_ms <= max(0.0, self._playback_ms) < measure.end_ms),
                             len(measures) - 1)
                first = min(index, max(0, len(measures) - 2))
                last = min(len(measures) - 1, first + 1)
                self._loop_start_ms = measures[first].start_ms
                self._loop_end_ms = measures[last].end_ms
            else:
                length = self._timeline.beat_duration_at(max(0, self._playback_ms)) * 8
                self._loop_start_ms = max(0.0, min(self._playback_ms,
                                                  self._timeline.duration_ms - length))
                self._loop_end_ms = min(self._timeline.duration_ms, self._loop_start_ms + length)
        if self._loop_end_ms <= self._loop_start_ms:
            return
        self._loop_enabled = True
        self.seek(self._loop_start_ms)

    def _clear_loop(self) -> None:
        was_enabled = self._loop_enabled
        self._loop_start_ms = self._loop_end_ms = None
        self._loop_enabled = False
        if was_enabled:
            self._begin_attempt(max(0.0, self._playback_ms))
            self._full_song_attempt = False
        self.save_practice_state()

    def mark_attempt_unreliable(self, reason: str) -> None:
        """Latch a measured input fault until the next attempt begins."""
        allowed = {"input_disconnected", "input_stalled", "input_overflow", "input_clipping"}
        if reason in allowed and reason not in self._attempt_issues:
            self._attempt_issues.append(reason)
            self._is_new_best = False
            self._recommendations = []

    def _refresh_health_window(self) -> None:
        """Assess device faults from the practice start, excluding count-in."""
        capture = self._audio_capture
        setter = getattr(capture, "set_health_window", None)
        if callable(setter):
            boundary = self._audio_anchor_capture_ms + max(
                0.0, (self._count_in_target_ms - self._playback_ms) / self._tempo_factor)
            setter(boundary)

    def _loop_finish_ms(self) -> float:
        end = self._loop_end_ms or 0.0
        if not self._audio_enabled or self._matcher is None:
            return end
        last = max((note.timestamp_ms for note in self._timeline.get_notes_in_range(
            self._assessment_start_ms, end) if self._note_passes_filter(note)
                    and self._matcher.get_note_state(note) == MatchType.PENDING), default=-math.inf)
        # A near-boundary onset can arrive after the visible end. Do not reset
        # its matcher before capture has had one complete analysis window.
        return max(end, last + self._matcher.completion_grace_ms)

    def _record_section_attempt(self, end_ms: float) -> float | None:
        if (not self._audio_enabled or self._matcher is None
                or self._progress_tracker is None or not self._song_key):
            return None
        self._feedback.add_results(self._matcher.finish(), self._playback_ms)
        stats = self._matcher.get_statistics()
        stats.update(timing_assessed=self._timing_assessed and not stats.get("timing_unknown", 0),
                     reliable=not self._attempt_issues)
        context = {
            "start_ms": self._assessment_start_ms, "end_ms": end_ms,
            "tempo_factor": self._attempt_tempo_factor,
            "filter_key": json.dumps([self._active_strings, self._max_fret,
                                      self._chord_partial_credit], separators=(",", ":")),
            "uniform_tempo": self._attempt_uniform_tempo,
            "wait_mode": self._wait_mode,
            "uniform_wait": self._attempt_uniform_wait,
            "input_key": self._input_key(),
            "uniform_input": (self._attempt_uniform_input
                              and self._attempt_input_key == self._input_key()),
            "complete_loop": bool(self._loop_enabled
                                  and self._assessment_start_ms == self._loop_start_ms),
        }
        self._progress_tracker.record_section_result(self._song_key, stats, context)
        self._section_advice = self._progress_tracker.claim_section_advice(self._song_key)
        if self._section_advice is not None:
            self._shorter_passage = self._smaller_loop() if self._section_advice["kind"] == "ease" else None
            target = self._section_advice["target_tempo"]
            if target != self._tempo_factor or self._shorter_passage is not None:
                return target
        return None

    def save_practice_state(self) -> None:
        """Persist choices and a resume point, never an unfinished best score."""
        if self._progress_tracker is None or not self._song_key or not self._song_path:
            return
        position = max(self._count_in_target_ms, self._playback_ms, 0.0)
        if self._loop_enabled and self._loop_end_ms is not None and position >= self._loop_end_ms:
            position = self._loop_start_ms or 0.0
        position = min(position, self._timeline.duration_ms)
        measure = next((i for i, m in enumerate(self._timeline.measures)
                        if m.start_ms <= position < m.end_ms), 0)
        self._progress_tracker.save_session(self._song_key, {
            "song_path": self._song_path, "instrument": self._config.instrument,
            "track_index": self._track_index, "position_ms": position, "measure": measure,
            "loop_start_ms": self._loop_start_ms, "loop_end_ms": self._loop_end_ms,
            "loop_enabled": self._loop_enabled, "tempo_factor": self._tempo_factor,
            "active_strings": list(self._active_strings), "max_fret": self._max_fret,
            "wait_mode": self._wait_mode, "chord_partial_credit": self._chord_partial_credit,
            "completed": self._song_completed,
        })
        self._last_session_save = time.perf_counter()

    def restore_practice_state(self, state: dict) -> bool:
        """Restore a validated session, paused; the next start includes count-in."""
        from pickhero.progress import validate_session
        state = validate_session(state)
        if (not state or state["instrument"] != self._config.instrument
                or state["track_index"] != self._track_index):
            return False
        self._close_panel_resources()
        self._playing = False
        self._stop_audio()
        self._last_tick = None
        self._panel = None
        self._show_help = False
        self._tempo_factor = state.get("tempo_factor", self._tempo_factor)
        self._max_fret = min(state.get("max_fret", self._max_fret), self._instrument.max_fret)
        self._unplayable_count = sum(
            1 for note in self._timeline.notes if note.fret > self._max_fret
        )
        strings = state.get("active_strings", self._active_strings)
        self._active_strings = list(strings[:self._lane_count]) + [True] * max(0, self._lane_count - len(strings))
        if not any(self._active_strings):
            self._active_strings = [True] * self._lane_count
        self._wait_mode = state.get("wait_mode", self._wait_mode)
        self._chord_partial_credit = state.get("chord_partial_credit", self._chord_partial_credit)
        self._loop_start_ms = state.get("loop_start_ms")
        self._loop_end_ms = state.get("loop_end_ms")
        if self._loop_start_ms is not None and not 0 <= self._loop_start_ms < self._timeline.duration_ms:
            self._loop_start_ms = None
        if self._loop_end_ms is not None and not 0 < self._loop_end_ms <= self._timeline.duration_ms:
            self._loop_end_ms = None
        if (self._loop_start_ms is not None and self._loop_end_ms is not None
                and self._loop_end_ms <= self._loop_start_ms):
            self._loop_start_ms = self._loop_end_ms = None
        self._loop_enabled = bool(state.get("loop_enabled") and self._loop_start_ms is not None
                                  and self._loop_end_ms is not None
                                  and 0 <= self._loop_start_ms < self._loop_end_ms <= self._timeline.duration_ms)
        position = 0.0 if state.get("completed") else min(state["position_ms"], self._timeline.duration_ms)
        if self._loop_enabled and not self._loop_start_ms <= position < self._loop_end_ms:
            position = self._loop_start_ms
        self.seek(position)
        self._resume_count_pending = True
        self._config.tempo_factor = self._tempo_factor
        self._config.active_strings = list(self._active_strings)
        self._config.max_fret = self._max_fret
        self._config.wait_mode = self._wait_mode
        self._config.chord_partial_credit = self._chord_partial_credit
        self._config.save()
        return True

    def _loop_hud_text(self) -> str | None:
        """Return loop status text for HUD, or None if no markers."""
        if self._loop_start_ms is not None and self._loop_end_ms is not None:
            start = format_time(self._loop_start_ms)
            end = format_time(self._loop_end_ms)
            # Four whole lines. The "(off)" tail is part of its own sentence,
            # not a suffix glued onto the other one.
            key = "play.loop_on" if self._loop_enabled else "play.loop_off"
            return t(key, start=start, end=end)
        if self._loop_start_ms is not None:
            return t("play.loop_from", start=format_time(self._loop_start_ms))
        if self._loop_end_ms is not None:
            return t("play.loop_to", end=format_time(self._loop_end_ms))
        return None

    def _draw_loop_region(self, surface: pygame.Surface, layout: _Layout) -> None:
        """Draw loop markers and shaded region between them."""
        if self._loop_start_ms is None and self._loop_end_ms is None:
            return

        look = get_look()
        lane_top = int(LANE_TOP_MARGIN)
        lane_bottom = int(LANE_TOP_MARGIN + layout.lane_count * layout.lane_height)
        lane_h = lane_bottom - lane_top

        marker_color = look.loop_marker if self._loop_enabled else look.loop_marker_disabled
        region_color = look.loop_region if self._loop_enabled else look.loop_region_disabled

        if self._stave is not None or self._road is not None:
            module = self._stave or self._road
            board = module.board(self._last_page)
            old_clip = surface.get_clip()
            surface.set_clip(board.clip(old_clip))
            if self._stave is not None:
                def position(stamp):
                    return round(self.note_x(stamp, self._playback_ms,
                                             self._playhead_x(layout), self._px_per_ms(layout)))
                if self._loop_start_ms is not None and self._loop_end_ms is not None:
                    left = max(board.left, position(self._loop_start_ms))
                    right = min(board.right, position(self._loop_end_ms))
                    if right > left:
                        size = (right - left, board.height)
                        layer = design.sprite(("loop-flat", size, region_color), size,
                                              lambda image: image.fill(region_color))
                        surface.blit(layer, (left, board.top))
                for stamp in (self._loop_start_ms, self._loop_end_ms):
                    if stamp is not None:
                        x = position(stamp)
                        pygame.draw.line(surface, marker_color,
                                         (x, board.top), (x, board.bottom), 2)
            else:
                def edge(stamp):
                    depth = (stamp - self._playback_ms) / self._visible_window_ms
                    _, y, scale = module.project(self._last_page, 0, self._lane_count, depth)
                    # Lane outer edges, in exactly the same projection as notes.
                    x0, _, _ = module.project(self._last_page, 0, self._lane_count, depth)
                    x1, _, _ = module.project(self._last_page, self._lane_count - 1,
                                               self._lane_count, depth)
                    half_lane = (x1 - x0) / max(1, self._lane_count - 1) / 2
                    return (round(x0 - half_lane), y), (round(x1 + half_lane), y)
                if self._loop_start_ms is not None and self._loop_end_ms is not None:
                    begin = max(self._playback_ms, self._loop_start_ms)
                    end = min(self._playback_ms + self._visible_window_ms, self._loop_end_ms)
                    if end > begin:
                        left0, right0 = edge(begin)
                        left1, right1 = edge(end)
                        layer = pygame.Surface(surface.get_size(), pygame.SRCALPHA)
                        pygame.draw.polygon(layer, region_color, [left0, right0, right1, left1])
                        surface.blit(layer, (0, 0))
                for stamp in (self._loop_start_ms, self._loop_end_ms):
                    if stamp is not None and 0 <= stamp - self._playback_ms <= self._visible_window_ms:
                        pygame.draw.line(surface, marker_color, *edge(stamp), 2)
            surface.set_clip(old_clip)
            return

        # Draw shaded region between both markers
        if self._loop_start_ms is not None and self._loop_end_ms is not None:
            x_start = int(self.note_x(self._loop_start_ms, self._playback_ms,
                                      layout.hit_zone_x, layout.pixels_per_ms))
            x_end = int(self.note_x(self._loop_end_ms, self._playback_ms,
                                    layout.hit_zone_x, layout.pixels_per_ms))
            # Clamp to screen
            x_start = max(0, min(x_start, layout.screen_w))
            x_end = max(0, min(x_end, layout.screen_w))
            if x_end > x_start:
                size = (layout.screen_w, lane_h)
                band = design.sprite(("loop", size, region_color), size,
                                     lambda layer: layer.fill(region_color))
                surface.blit(band, (x_start, lane_top),
                             area=pygame.Rect(0, 0, x_end - x_start, lane_h))

        # Draw start marker
        if self._loop_start_ms is not None:
            x = int(self.note_x(self._loop_start_ms, self._playback_ms,
                                layout.hit_zone_x, layout.pixels_per_ms))
            if 0 <= x <= layout.screen_w:
                pygame.draw.line(surface, marker_color, (x, lane_top), (x, lane_bottom), 2)
                # Right-pointing triangle at top
                pygame.draw.polygon(surface, marker_color, [
                    (x, lane_top), (x + 10, lane_top + 7), (x, lane_top + 14),
                ])

        # Draw end marker
        if self._loop_end_ms is not None:
            x = int(self.note_x(self._loop_end_ms, self._playback_ms,
                                layout.hit_zone_x, layout.pixels_per_ms))
            if 0 <= x <= layout.screen_w:
                pygame.draw.line(surface, marker_color, (x, lane_top), (x, lane_bottom), 2)
                # Left-pointing triangle at top
                pygame.draw.polygon(surface, marker_color, [
                    (x, lane_top), (x - 10, lane_top + 7), (x, lane_top + 14),
                ])

    # -- Audio control --

    def _toggle_audio(self) -> None:
        """Toggle audio capture on/off."""
        self._audio_enabled = not self._audio_enabled
        if self._audio_enabled:
            if self._song_completed:
                self.seek(0.0)
            if (self._audio_disabled_at_ms is not None
                    and self._playback_ms > self._audio_disabled_at_ms):
                self._begin_attempt(max(0.0, self._playback_ms))
                self._full_song_attempt = False
            self._audio_disabled_at_ms = None
            if self._playing:
                if self._playback_ms < self._count_in_target_ms:
                    # Starting a device can block: begin a fresh audible
                    # count-in after that operation instead of stretching it.
                    self._playback_ms = self._count_in_target_ms - self._count_in_ms
                    self._last_count_in_beat = -1
                self._start_audio()
            else:
                # Start capture for signal monitoring even while paused
                self._start_capture_only()
        else:
            self._audio_disabled_at_ms = max(0.0, self._playback_ms)
            self._release_wait()
            self._stop_audio()

    def _convert_timestamps(self, detected: list) -> None:
        """Put detections on the song clock.

        A detection carries wall-clock milliseconds since the capture started.
        The song clock runs at tempo_factor times that, from wherever playback
        stood when the capture began — the matcher holds that starting point
        as its audio offset.

        This used to multiply the whole capture time by the current factor. As
        long as the factor never changed that happened to work; the moment it
        did, every millisecond already elapsed was rescaled retroactively.
        Dropping to half speed a minute into a song threw detections back to
        the thirty-second mark, where there was nothing to match, and nothing
        was ever scored again for the rest of the run.
        """
        base = self._audio_anchor_capture_ms
        for d in detected:
            if self._audio_time_segments and self._matcher is not None:
                # A detector report can arrive after a tempo change while
                # referring to a strike before it. Use the segment in which
                # the strike happened, not the speed when its report arrived.
                segment = self._audio_time_segments[0]
                for candidate in reversed(self._audio_time_segments):
                    if candidate[0] <= d.timestamp_ms:
                        segment = candidate
                        break
                capture_ms, song_ms, factor = segment
                d.timestamp_ms = (song_ms + (d.timestamp_ms - capture_ms) * factor
                                  - self._matcher.audio_offset_ms)
            else:
                d.timestamp_ms = (d.timestamp_ms - base) * self._tempo_factor

    def _reanchor_audio(self, *, preserve_history: bool = False,
                        at_tick: float | None = None) -> None:
        """Pin the song clock to the capture clock at this instant."""
        if self._audio_capture is None or self._matcher is None:
            return
        self._audio_anchor_capture_ms = self._audio_capture.capture_elapsed_ms()
        if at_tick is not None:
            self._audio_anchor_capture_ms -= (time.perf_counter() - at_tick) * 1000
        self._matcher.audio_offset_ms = (
            self._playback_ms
            + self._config.audio_latency_offset_ms * self._tempo_factor
        )
        segment = (self._audio_anchor_capture_ms, self._matcher.audio_offset_ms,
                   self._tempo_factor)
        # Tempo changes preserve the clock's past. A resume after a frozen
        # wait establishes a fresh mapping, including late reports from the
        # just-ended wait, as it did before tempo segments were introduced.
        if not preserve_history:
            self._audio_time_segments.clear()
        if self._audio_time_segments and self._audio_time_segments[-1][0] == segment[0]:
            self._audio_time_segments[-1] = segment
        else:
            self._audio_time_segments.append(segment)
        self._test_recording.anchor()

    def _refresh_preroll_boundary(self) -> None:
        """Keep the warm-up cutoff consistent with the current note filters."""
        self._preroll_before_ms = None
        if not self._capture_preroll or self._matcher is None:
            return
        first_note = next((note for note in self._timeline.notes
                           if note.timestamp_ms >= self._assessment_start_ms
                           and self._note_passes_filter(note)), None)
        if first_note is not None:
            # Warm-up filtering ends at the practice start. Later early attempts keep
            # their ordinary pitch credit even when the score has an intro.
            self._preroll_before_ms = min(
                self._count_in_target_ms, first_note.timestamp_ms - self._matcher._reach_ms)

    def _start_audio(self) -> None:
        """Restart capture while preserving the current assessment on resume."""
        try:
            from pickhero.audio.input import AudioCapture
            if self._audio_capture is None:
                self._audio_capture = AudioCapture(self._config, instrument=self._instrument)
            self._audio_capture.start()
            current_input = self._input_key()
            if self._playback_ms <= self._assessment_start_ms:
                self._attempt_input_key = current_input
            elif self._attempt_input_key != current_input:
                self._attempt_uniform_input = False
            self._audio_anchor_capture_ms = 0.0
            self._audio_time_segments.clear()
            if self._matcher is None:
                self._matcher = NoteMatcher(
                    self._timeline,
                    timing_window_ms=max(self._config.timing_window_ms,
                                         self._instrument.timing_window_ms),
                    chord_threshold_ms=self._config.chord_threshold_ms,
                    note_filter=self._note_passes_filter,
                    chord_partial_credit=self._chord_partial_credit,
                )
                self._matcher.reset(
                    self._assessment_start_ms,
                    self._loop_end_ms if self._loop_enabled else None,
                )
            self._matcher.set_retire_delay(self._detector_delay_ms() * self._tempo_factor)
            self._sync_timing_window()
            self._capture_preroll = self._playback_ms < self._count_in_target_ms
            self._refresh_preroll_boundary()
            self._test_recording.started()
            self._reanchor_audio()
            self._refresh_health_window()
            if self._playing:
                self._last_tick = time.perf_counter()
        except Exception as e:
            log_error(f"Audio start failed: {e}")
            if self._test_recording.series.label:
                self._test_recording.error = 'input_start_failed'
                self._test_recording.series.end('error')
            self.mark_attempt_unreliable("input_disconnected")
            self._audio_enabled = False
            self._audio_disabled_at_ms = max(0.0, self._playback_ms)

    def _start_capture_only(self) -> None:
        """Start audio capture for signal monitoring (no matcher)."""
        try:
            from pickhero.audio.input import AudioCapture
            if self._audio_capture is None:
                self._audio_capture = AudioCapture(self._config, instrument=self._instrument)
            self._audio_capture.start()
        except Exception as e:
            log_error(f"Audio capture start failed: {e}")
            self._audio_enabled = False

    def _stop_audio(self, *, recording_complete=False, reason='interrupted') -> None:
        """Stop audio capture."""
        if self._audio_capture is not None:
            self._audio_capture.stop()
        self._test_recording.stop(reached_end=recording_complete, reason=reason)

    def stop_audio(self) -> None:
        """Public method to stop audio (called on state transitions)."""
        self._close_panel_resources()
        self.save_practice_state()
        self._stop_audio(reason='leave_song')
        self._audio_enabled = False
        if self._midi_player is not None:
            self._midi_player.close()
            self._midi_player = None

    def _close_panel_resources(self) -> None:
        closer = getattr(self._panel, "close", None)
        if callable(closer):
            closer()

    def _input_key(self):
        from pickhero.practice_advice import input_fingerprint
        return input_fingerprint(self._config, self._instrument, self._audio_capture,
                                 timing_window_ms=self._timing_window_for_marker())

    # -- MIDI backing track --

    def _init_midi_player(self, backing_track: BackingTrack) -> None:
        """Create and open MidiPlayer. Silently continues if MIDI unavailable."""
        try:
            player = MidiPlayer(backing_track)
            if player.open():
                player.set_muted(self._backing_muted)
                self._midi_player = player
            else:
                player.close()
        except Exception as e:
            log_error(f"MIDI player init failed: {e}")

    def _beats_per_bar(self) -> int:
        """Beats in a bar, from the measures the tab actually carries.

        Derived rather than assumed: a bar divided by the beat is the time
        signature's numerator, and a piece in three would otherwise get its
        accent on the wrong beat, which is worse than no accent at all.
        """
        measures = self._timeline.measures
        beat = self._beat_ms()
        if not measures or beat <= 0:
            return 4
        measure = next((m for m in measures if m.start_ms <= self._playback_ms < m.end_ms),
                       measures[0])
        beats = self._timeline.beats_in_range(measure.start_ms, measure.end_ms)
        return max(1, len(beats))

    def _metronome_on(self) -> bool:
        """Whether the click should sound during the run.

        Separate from the backing track: muting an accompaniment is a taste,
        losing the beat is losing the exercise.
        """
        return bool(getattr(self._config, "metronome", True))

    def _toggle_metronome(self) -> None:
        self._config.metronome = not self._metronome_on()
        self._config.save()

    def _toggle_backing(self) -> None:
        """Toggle backing track mute on/off."""
        if self._midi_player is None:
            return
        self._backing_muted = not self._backing_muted
        self._midi_player.set_muted(self._backing_muted)
