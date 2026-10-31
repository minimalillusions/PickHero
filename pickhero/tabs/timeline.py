"""Song timeline data structures.

NoteEvent represents a single note in a tab. Timeline holds a sorted sequence
of NoteEvents and provides efficient range queries for the game loop.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field

from pickhero.tabs.import_safety import (
    MAX_DURATION_MS, MAX_NOTES, MAX_WORK, finite_range, tempo,
)


# Six is the common case, not the limit. Seven- and eight-string guitars are
# ordinary instruments and their tabs are ordinary files; rejecting them here
# raised a ValueError out of the loader and the song simply failed to open.
MAX_STRINGS = 12


@dataclass(frozen=True)
class NoteEvent:
    """A single note event in the song timeline."""

    timestamp_ms: float
    duration_ms: float
    midi_note: int
    string: int  # 1 is the highest-pitched string
    fret: int    # 0=open
    measure: int = 0  # measure index (0-based)

    def __post_init__(self):
        finite_range(self.timestamp_ms, 0, MAX_DURATION_MS, "timestamp_ms")
        finite_range(self.duration_ms, 0, MAX_DURATION_MS, "duration_ms")
        finite_range(self.end_ms, 0, MAX_DURATION_MS, "note end")
        for name in ("midi_note", "string", "fret", "measure"):
            if not isinstance(getattr(self, name), int):
                raise ValueError(f"{name} must be an integer")
        if not 0 <= self.midi_note <= 127:
            raise ValueError(f"midi_note must be 0-127, got {self.midi_note}")
        if not 1 <= self.string <= MAX_STRINGS:
            raise ValueError(f"string must be 1-{MAX_STRINGS}, got {self.string}")
        if not 0 <= self.fret <= 127:
            raise ValueError(f"fret must be 0-127, got {self.fret}")
        if self.measure < 0:
            raise ValueError("measure must be nonnegative")

    @property
    def end_ms(self) -> float:
        return self.timestamp_ms + self.duration_ms


@dataclass(frozen=True)
class MeasureInfo:
    """Time range for a single measure/bar."""
    index: int
    start_ms: float
    end_ms: float

    def __post_init__(self):
        finite_range(self.start_ms, 0, MAX_DURATION_MS, "measure start")
        finite_range(self.end_ms, self.start_ms, MAX_DURATION_MS, "measure end")


@dataclass(frozen=True)
class BeatInfo:
    """A notated pulse, with a bar accent independent of tempo changes."""
    timestamp_ms: float
    measure: int = 0
    beat: int = 0

    def __post_init__(self):
        finite_range(self.timestamp_ms, 0, MAX_DURATION_MS, "beat timestamp")
        if self.measure < 0 or self.beat < 0:
            raise ValueError("beat and measure must be nonnegative")

    @property
    def accent(self) -> bool:
        return self.beat == 0


@dataclass
class SongMetadata:
    """Metadata extracted from a GP file."""

    title: str = ""
    artist: str = ""
    album: str = ""
    track_name: str = ""
    tempo: float = 120
    tuning: dict[int, int] = field(default_factory=dict)
    track_index: int = 0


class Timeline:
    """Sorted collection of NoteEvents with efficient range queries."""

    def __init__(self, notes: list[NoteEvent], metadata: SongMetadata | None = None,
                 measures: list[MeasureInfo] | None = None,
                 beats: list[BeatInfo] | None = None,
                 tempo_changes: list[tuple[float, float]] | None = None):
        if len(notes) > MAX_NOTES:
            raise ValueError("Too many notes")
        self._notes = sorted(notes, key=lambda n: (n.timestamp_ms, n.string))
        self._timestamps = [n.timestamp_ms for n in self._notes]
        self.metadata = metadata or SongMetadata()
        tempo(self.metadata.tempo)
        self._measures = sorted(measures or [], key=lambda m: m.start_ms)
        self._cursor = 0
        self._max_duration = max((n.duration_ms for n in self._notes), default=0.0)
        self._duration_ms = max(max((n.end_ms for n in self._notes), default=0.0),
                                max((m.end_ms for m in self._measures), default=0.0))
        changes = {0.0: float(self.metadata.tempo)}
        for at, bpm in tempo_changes or []:
            finite_range(at, 0, MAX_DURATION_MS, "tempo time")
            changes[at] = tempo(bpm)
        self._tempo_times = sorted(changes)
        self._tempos = [changes[at] for at in self._tempo_times]
        self._beats = sorted(beats if beats is not None else self._default_beats(),
                             key=lambda b: b.timestamp_ms)
        self._beat_times = [b.timestamp_ms for b in self._beats]

    def __len__(self) -> int:
        return len(self._notes)

    def __repr__(self) -> str:
        title = self.metadata.title or "Untitled"
        return f"Timeline('{title}', {len(self)} notes, {self.duration_ms:.0f}ms)"

    @property
    def notes(self) -> list[NoteEvent]:
        return list(self._notes)

    @property
    def measures(self) -> list[MeasureInfo]:
        return list(self._measures)

    @property
    def duration_ms(self) -> float:
        return self._duration_ms

    def _default_beats(self) -> list[BeatInfo]:
        """Quarter pulses for exercises/older callers without a score beat map."""
        beats = []
        measures = self._measures or [MeasureInfo(0, 0.0, self.duration_ms)]
        for measure in measures:
            at, index = measure.start_ms, 0
            while at < measure.end_ms:
                if len(beats) >= MAX_WORK:
                    raise ValueError("Too many beats")
                measure_index = measure.index if self._measures else index // 4
                beat_index = index if self._measures else index % 4
                beats.append(BeatInfo(at, measure_index, beat_index))
                # Integrate a quarter note through any intervening tempo change.
                remaining = 1.0
                while remaining > 1e-10:
                    tempo_index = max(0, bisect.bisect_right(self._tempo_times, at) - 1)
                    step = remaining * 60_000 / self._tempos[tempo_index]
                    if tempo_index + 1 < len(self._tempo_times):
                        boundary = self._tempo_times[tempo_index + 1]
                        if at + step > boundary:
                            remaining -= (boundary - at) * self._tempos[tempo_index] / 60_000
                            at = boundary
                            continue
                    at += step
                    break
                index += 1
        return beats

    def beats_in_range(self, start_ms: float, end_ms: float) -> list[BeatInfo]:
        """Return score pulses in [start_ms, end_ms), including bar accents."""
        left = bisect.bisect_left(self._beat_times, start_ms)
        right = bisect.bisect_left(self._beat_times, end_ms)
        return self._beats[left:right]

    def beat_duration_at(self, time_ms: float) -> float:
        """Milliseconds per quarter note at the requested song time."""
        index = max(0, bisect.bisect_right(self._tempo_times, time_ms) - 1)
        return 60_000 / self._tempos[index]

    def get_notes_in_range(self, start_ms: float, end_ms: float) -> list[NoteEvent]:
        """Return notes whose timestamp_ms falls within [start_ms, end_ms)."""
        left = bisect.bisect_left(self._timestamps, start_ms)
        right = bisect.bisect_left(self._timestamps, end_ms)
        return self._notes[left:right]

    def get_active_notes_at_time(self, time_ms: float, window_ms: float = 100.0) -> list[NoteEvent]:
        """Return notes that overlap the window [time_ms - window_ms, time_ms + window_ms].

        A note is active if its sounding range [timestamp_ms, end_ms] overlaps the window.
        """
        window_start = time_ms - window_ms
        window_end = time_ms + window_ms

        # Notes that start before the window ends and can still be sounding
        # when it begins. The left bound used to be the start of the song, so
        # this walked every note played so far on every call, once per
        # detection, sixty times a second.
        right = bisect.bisect_right(self._timestamps, window_end)
        left = bisect.bisect_left(self._timestamps,
                                  window_start - self._max_duration)

        result = []
        for i in range(left, right):
            note = self._notes[i]
            if note.end_ms >= window_start and note.timestamp_ms <= window_end:
                result.append(note)
        return result

    def get_notes_struck_near(self, time_ms: float,
                              reach_ms: float) -> list[NoteEvent]:
        """Notes whose WRITTEN MOMENT is within `reach_ms` of `time_ms`.

        Different from get_active_notes_at_time, which asks whether a note is
        still sounding. A strike belongs to the moment a note begins: a half
        note lasts two seconds, and striking it a second and a half in is not
        a late attempt at it, it is a note in the wrong place. Matching on the
        sounding range made the tolerance depend on how long the note was
        written to be held, so the same lateness was forgiven on a half note
        and refused on a sixteenth.
        """
        left = bisect.bisect_left(self._timestamps, time_ms - reach_ms)
        right = bisect.bisect_right(self._timestamps, time_ms + reach_ms)
        return self._notes[left:right]

    def seek(self, time_ms: float) -> None:
        """Move the cursor to the first note at or after time_ms."""
        self._cursor = bisect.bisect_left(self._timestamps, time_ms)

    def get_next_notes(self, count: int = 1) -> list[NoteEvent]:
        """Return up to `count` notes from the cursor, advancing it."""
        end = min(self._cursor + count, len(self._notes))
        result = self._notes[self._cursor:end]
        self._cursor = end
        return result
