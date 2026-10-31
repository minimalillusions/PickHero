"""Small, measurable lessons built from the app's own instrument exercises.

The plan is derived from existing results. Opening it never changes progress,
and a microphone-based score is never presented as a judgement of technique.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pickhero.audio.midi_playback import (
    NOTE_OFF,
    NOTE_ON,
    PROGRAM_CHANGE,
    BackingTrack,
    MidiEvent,
)
from pickhero.exercises import (
    _SLUGS,
    EXERCISE_DIRNAME,
    GM_BASS,
    GM_GUITAR,
    Exercise,
    build_exercises,
)
from pickhero.i18n import t
from pickhero.progress import song_key
from pickhero.validation import number


@dataclass(frozen=True)
class Lesson:
    order: int
    slug: str

    @property
    def title(self) -> str:
        return t(f"exercise.{self.slug}.title")

    @property
    def goal(self) -> str:
        return t(f"lesson.{self.slug}.goal")

    @property
    def instruction(self) -> str:
        return t(f"exercise.{self.slug}.subtitle")

    @property
    def timing_required(self) -> bool:
        return self.order > 2

    @property
    def criterion(self) -> str:
        return t("lesson.criterion_rhythm" if self.timing_required else "lesson.criterion_pitch")

    @property
    def filename(self) -> str:
        return f"{self.order:02d} {self.slug}.gp5"


LESSONS = tuple(Lesson(i, slug) for i, slug in enumerate(_SLUGS, 1))


@dataclass(frozen=True)
class PracticeStep:
    """An explicit, optional preparation for one lesson's complete attempt."""
    key: str
    first_bar: int
    stop_bar: int
    tempo_factor: float
    wait_mode: bool
    loop: bool = True

    @property
    def title(self) -> str:
        return t(f"lesson.step.{self.key}.title")

    @property
    def instruction(self) -> str:
        return t(f"lesson.step.{self.key}.instruction")


@dataclass(frozen=True)
class LessonPractice:
    path: Path
    step: PracticeStep


def practice_steps(lesson: Lesson, instrument) -> tuple[PracticeStep, ...]:
    """Prepare the actual written lesson in short, freely chosen passages.

    Whole-note introductions retain 60 BPM; halving them would make each
    note last eight seconds. Rhythm lessons progress through a slower pulse,
    while new fingerings can first be explored without a timing judgement.
    """
    if lesson not in LESSONS:
        return ()
    bars = len(exercise_for_lesson(lesson, instrument).bars)
    # The written open-string round has two notes per string (up and down).
    # It spans two full bars on four strings and three on six strings.
    round_bars = min(bars, (2 * instrument.string_count + 3) // 4)
    preparations = {
        "open_string": (
            PracticeStep("open_single", 0, 1, 1.0, True),
        ),
        "all_open": (
            PracticeStep("open_round", 0, min(bars, instrument.string_count), 1.0, True),
        ),
        "quarters_one_string": (
            PracticeStep("quarters_slow", 0, 1, .5, False),
            PracticeStep("quarters_pulse", 0, 1, .75, False),
        ),
        "string_change": (
            PracticeStep("change_wait", 0, 1, .5, True),
            PracticeStep("change_pulse", 0, 1, .75, False),
        ),
        "open_up_down": (
            PracticeStep("open_cycle_wait", 0, round_bars, .5, True),
            PracticeStep("open_cycle_pulse", 0, round_bars, .75, False),
        ),
        "first_frets": (
            PracticeStep("frets_up_wait", 0, 1, .5, True),
            PracticeStep("frets_up_pulse", 0, 1, .75, False),
        ),
        "frets_back": (
            PracticeStep("frets_down_wait", 0, 1, .5, True),
            PracticeStep("frets_down_pulse", 0, 1, .75, False),
        ),
        "scale_one_octave": (
            PracticeStep("lower", 0, 1, .5, True),
            PracticeStep("upper", 1, 2, .5, True),
            PracticeStep("up", 0, 2, .75, False),
            PracticeStep("down", 2, 4, .75, False),
        ),
        "eighths": (
            PracticeStep("eighths_slow", 0, 1, .5, False),
            PracticeStep("eighths_pulse", 0, 1, .75, False),
        ),
        "first_pattern": (
            PracticeStep("pattern_wait", 0, 1, .5, True),
            PracticeStep("pattern_slow", 0, 1, .5, False),
            PracticeStep("pattern_pulse", 0, 1, .75, False),
        ),
    }[lesson.slug]
    return preparations + (PracticeStep("full", 0, bars, 1.0, False, loop=False),)


def practice_state(request: LessonPractice, timeline, instrument) -> dict:
    """Translate the selected step into the normal saved-practice contract.

    Bar bounds come from the imported timeline, not from a second clock.
    Preparation is always a passage; only the explicit full step can certify
    the lesson's full-song goal. This function itself never saves a result.
    """
    lesson = lesson_for_path(request.path)
    step = request.step
    from pickhero.music import music_for_path, music_practice
    piece = music_for_path(request.path)
    allowed = (practice_steps(lesson, instrument) if lesson is not None else
               (music_practice(request.path, instrument).step,) if piece is not None else ())
    if (step not in allowed
            or not 0 <= step.first_bar < step.stop_bar <= len(timeline.measures)):
        raise ValueError("Lesson practice step does not match the loaded score")
    start = timeline.measures[step.first_bar].start_ms
    end = timeline.measures[step.stop_bar - 1].end_ms
    return {
        "song_path": str(request.path.resolve()), "instrument": instrument.key,
        "track_index": timeline.metadata.track_index, "position_ms": start,
        "loop_start_ms": start if step.loop else None,
        "loop_end_ms": end if step.loop else None,
        "loop_enabled": step.loop, "tempo_factor": step.tempo_factor,
        "wait_mode": step.wait_mode, "active_strings": [True] * instrument.string_count,
        "max_fret": instrument.max_fret, "completed": False,
    }


def next_lesson_practice(path: str | Path, instrument,
                         practice: LessonPractice | None = None) -> LessonPractice | None:
    """Move forward in the chosen sequence, independently of assessment.

    The last preparation leads to the full lesson. After a full lesson (or
    a normal library launch), offer the following full lesson. Earlier
    unfinished goals never redirect this navigation backwards.
    """
    path = Path(path)
    lesson = lesson_for_path(path)
    if lesson is None:
        from pickhero.music import next_music_practice
        return next_music_practice(path, instrument, practice)
    if practice is not None:
        steps = practice_steps(lesson, instrument)
        if practice.path.resolve() != path.resolve() or practice.step not in steps:
            return None
        index = steps.index(practice.step)
        if index + 1 < len(steps):
            return LessonPractice(path, steps[index + 1])
    if lesson.order >= len(LESSONS):
        return None
    following = LESSONS[lesson.order]
    return LessonPractice(path.with_name(following.filename),
                          practice_steps(following, instrument)[-1])


@dataclass(frozen=True)
class LessonState:
    lesson: Lesson
    path: Path
    complete: bool = False
    best_accuracy: float = 0.0
    attempts: int = 0
    last_played: str = ""


@dataclass(frozen=True)
class LearningPlan:
    lessons: tuple[LessonState, ...]
    review: LessonState
    next_lesson: LessonState

    @property
    def completed(self) -> int:
        return sum(state.complete for state in self.lessons)


def lesson_for_path(path: str | Path) -> Lesson | None:
    """Only known generated exercise names participate in this curriculum."""
    path = Path(path)
    if path.parent.name.casefold() != EXERCISE_DIRNAME.casefold():
        return None
    return next((lesson for lesson in LESSONS if path.name == lesson.filename), None)


def _meets_goal(lesson: Lesson, record) -> bool:
    if record is None:
        return False
    milestone = "goal_rhythm_met" if lesson.timing_required else "goal_pitch_met"
    if getattr(record, milestone, False) is True:
        return True
    for entry in record.tempo_history:
        # Historical records without a reliability assessment remain useful
        # history, but cannot certify a newly introduced learning criterion.
        if not isinstance(entry, dict) or entry.get("reliable") is not True:
            continue
        accuracy = entry.get("accuracy")
        tempo = entry.get("tempo_factor")
        if not number(accuracy, 80, 100) or not number(tempo, 1.0, 2.0):
            continue
        if lesson.timing_required and (
            entry.get("timing_assessed") is not True
            or not number(entry.get("timing_percent"), 60, 100)
        ):
            continue
        return True
    return False


def build_learning_plan(instrument, songs_dir: str | Path, progress) -> LearningPlan:
    """Derive a next step and a short review without locking any lesson."""
    from pickhero.library import content_identity

    states = []
    for lesson in LESSONS:
        path = Path(songs_dir) / EXERCISE_DIRNAME / lesson.filename
        try:
            identity = content_identity(path)
        except (OSError, ValueError):
            identity = None
        record = progress.get_best(song_key(path, instrument.key, content_id=identity))
        states.append(LessonState(
            lesson, path, _meets_goal(lesson, record),
            record.best_accuracy if record else 0.0,
            record.attempts if record else 0,
            record.last_played if record else "",
        ))
    following = next((state for state in states if not state.complete), states[-1])
    practiced = [state for state in states if state.attempts and state != following]
    # Return to the most recent familiar exercise; this is an invitation,
    # never a daily streak or a penalty for being away.
    review = max(practiced, key=lambda state: state.last_played) if practiced else states[0]
    return LearningPlan(tuple(states), review, following)


@dataclass(frozen=True)
class LessonDemo:
    track: BackingTrack
    duration_ms: float
    count_in_ms: float
    bars: int


def build_demo(exercise: Exercise, instrument, bars: int = 2, *,
               first_bar: int = 0, tempo_factor: float = 1.0) -> LessonDemo:
    """Preview the actual played notes, with a four-beat count-in.

    Generated exercises have one selected track; a normal backing track
    excludes it. Scheduling the steps directly makes this example audible
    without recording or scoring the listener.
    """
    if (type(first_bar) is not int or not 0 <= first_bar < len(exercise.bars)
            or type(bars) is not int or bars < 1
            or not number(tempo_factor, .25, 1.0)):
        raise ValueError("Invalid lesson demo range or tempo")
    quarter = 60000.0 / (exercise.bpm * tempo_factor)
    events = [MidiEvent(-1.0, 0, PROGRAM_CHANGE,
                        GM_BASS if instrument.midi_min < 40 else GM_GUITAR)]
    for beat in range(4):
        events.extend((MidiEvent(beat * quarter, 9, NOTE_ON, 76, 100 if beat == 0 else 75),
                       MidiEvent(beat * quarter + 45, 9, NOTE_OFF, 76)))
    count_in = 4 * quarter
    selected_bars = exercise.bars[first_bar:min(len(exercise.bars), first_bar + bars)]
    position = count_in
    for bar in selected_bars:
        for step in bar:
            duration = quarter * 4 / step.value
            if step.string is not None:
                pitch = instrument.tuning[step.string] + step.fret
                events.extend((MidiEvent(position, 0, NOTE_ON, pitch, 92),
                               MidiEvent(position + duration * .9, 0, NOTE_OFF, pitch)))
            position += duration
    return LessonDemo(BackingTrack(events), position, count_in, len(selected_bars))


def build_file_demo(timeline, instrument, bars: int = 4, *,
                    limit_ms: float = 20_000.0) -> LessonDemo:
    """Preview a player's own file: its first bars with notes, as written.

    The same shape as build_demo -- the instrument's sound, four clicks, then
    the notes -- but read from a Timeline the import worker has already
    decoded, never from the file. A file from elsewhere is only ever parsed
    in that worker process, and a preview is no reason to change that.

    It begins at the first bar that holds a note, so a part that enters
    after a long intro is not previewed as twenty seconds of silence, and it
    stops after `bars` bars or `limit_ms`, whichever comes first.
    """
    notes = sorted(timeline.notes, key=lambda note: note.timestamp_ms)
    if not notes or type(bars) is not int or bars < 1:
        raise ValueError("Nothing to preview in this part")
    measures = [m for m in timeline.measures if m.end_ms > notes[0].timestamp_ms]
    if measures:
        start_ms = min(measures[0].start_ms, notes[0].timestamp_ms)
        end_ms = measures[min(bars, len(measures)) - 1].end_ms
    else:
        start_ms = notes[0].timestamp_ms
        end_ms = notes[-1].timestamp_ms + notes[-1].duration_ms
    end_ms = min(end_ms, start_ms + limit_ms)
    # A count-in at the tempo the preview starts in. Clamped, because a file
    # can claim any tempo at all and four clicks of three seconds each would
    # read as a preview that never starts.
    beat = min(1500.0, max(200.0, float(timeline.beat_duration_at(start_ms))))
    events = [MidiEvent(-1.0, 0, PROGRAM_CHANGE,
                        GM_BASS if instrument.midi_min < 40 else GM_GUITAR)]
    for index in range(4):
        events.extend((MidiEvent(index * beat, 9, NOTE_ON, 76, 100 if index == 0 else 75),
                       MidiEvent(index * beat + 45, 9, NOTE_OFF, 76)))
    count_in = 4 * beat
    for note in notes:
        if not start_ms <= note.timestamp_ms < end_ms or not 0 <= note.midi_note <= 127:
            continue
        at = count_in + note.timestamp_ms - start_ms
        length = max(40.0, min(note.duration_ms, end_ms - note.timestamp_ms) * .9)
        events.extend((MidiEvent(at, 0, NOTE_ON, note.midi_note, 92),
                       MidiEvent(at + length, 0, NOTE_OFF, note.midi_note)))
    taken = sum(1 for m in measures if start_ms <= m.start_ms < end_ms) or 1
    return LessonDemo(BackingTrack(events), count_in + end_ms - start_ms, count_in, taken)


def exercise_for_lesson(lesson: Lesson, instrument) -> Exercise:
    return build_exercises(instrument)[lesson.order - 1]
