"""Every lesson prepares its actual written music without changing the score."""
from copy import deepcopy

import pytest

from pickhero import i18n
from pickhero.audio.midi_playback import NOTE_OFF, NOTE_ON
from pickhero.curriculum import (
    LESSONS,
    Lesson,
    LessonPractice,
    PracticeStep,
    build_demo,
    exercise_for_lesson,
    practice_state,
    practice_steps,
)
from pickhero.exercises import EXERCISE_DIRNAME, build_exercises, write_exercises
from pickhero.instruments import GUITAR, PROFILES
from pickhero.tabs.loader import load_gp_file


@pytest.fixture(params=list(PROFILES.values()), ids=lambda profile: profile.key)
def instrument(request):
    return request.param


def _sounding(demo):
    return [event for event in demo.track.events
            if event.event_type == NOTE_ON and event.channel == 0]


def test_each_lesson_has_short_preparations_and_one_explicit_full_step(instrument):
    lengths = [2, 2, 3, 3, 3, 3, 3, 5, 3, 4]
    for lesson, expected_count in zip(LESSONS, lengths):
        exercise = exercise_for_lesson(lesson, instrument)
        steps = practice_steps(lesson, instrument)
        assert len(steps) == expected_count
        assert len({step.key for step in steps}) == len(steps)
        assert steps[-1] == PracticeStep("full", 0, len(exercise.bars), 1, False, loop=False)
        for step in steps[:-1]:
            assert 0 <= step.first_bar < step.stop_bar <= len(exercise.bars)
            assert step.loop
            assert step.stop_bar - step.first_bar < len(exercise.bars)
            assert .5 <= step.tempo_factor <= 1


def test_existing_scale_steps_are_unchanged(instrument):
    assert practice_steps(LESSONS[7], instrument) == (
        PracticeStep("lower", 0, 1, .5, True),
        PracticeStep("upper", 1, 2, .5, True),
        PracticeStep("up", 0, 2, .75, False),
        PracticeStep("down", 2, 4, .75, False),
        PracticeStep("full", 0, 8, 1, False, loop=False),
    )


def test_whole_note_introductions_keep_normal_tempo_and_actual_string_count(instrument):
    single = practice_steps(LESSONS[0], instrument)[0]
    assert (single.first_bar, single.stop_bar, single.tempo_factor, single.wait_mode) == (0, 1, 1, True)
    round_step = practice_steps(LESSONS[1], instrument)[0]
    assert (round_step.first_bar, round_step.stop_bar) == (0, instrument.string_count)
    assert round_step.tempo_factor == 1 and round_step.wait_mode
    exercise = exercise_for_lesson(LESSONS[1], instrument)
    demo = build_demo(exercise, instrument, round_step.stop_bar)
    assert [event.data1 for event in _sounding(demo)] == [
        instrument.tuning[string] for string in instrument.strings_low_to_high()]
    assert demo.bars == instrument.string_count
    assert demo.duration_ms - demo.count_in_ms == instrument.string_count * 4000


def test_open_string_preparations_include_the_entire_up_and_down_round(instrument):
    exercise = exercise_for_lesson(LESSONS[4], instrument)
    up = instrument.strings_low_to_high()
    for step in practice_steps(LESSONS[4], instrument)[:-1]:
        selected = [note.string for bar in exercise.bars[step.first_bar:step.stop_bar] for note in bar]
        assert selected == up + list(reversed(up))
        assert step.stop_bar == (3 if instrument == GUITAR else 2)


@pytest.mark.parametrize("lesson_index,settings", [
    (2, [(0, 1, .5, False), (0, 1, .75, False)]),
    (3, [(0, 1, .5, True), (0, 1, .75, False)]),
    (5, [(0, 1, .5, True), (0, 1, .75, False)]),
    (6, [(0, 1, .5, True), (0, 1, .75, False)]),
    (8, [(0, 1, .5, False), (0, 1, .75, False)]),
    (9, [(0, 1, .5, True), (0, 1, .5, False), (0, 1, .75, False)]),
])
def test_fingering_and_rhythm_preparations_have_distinct_conditions(instrument, lesson_index, settings):
    actual = practice_steps(LESSONS[lesson_index], instrument)[:-1]
    assert [(step.first_bar, step.stop_bar, step.tempo_factor, step.wait_mode)
            for step in actual] == settings


def test_first_eighth_note_step_has_the_familiar_sixty_attacks_per_minute(instrument):
    exercise = exercise_for_lesson(LESSONS[8], instrument)
    step = practice_steps(LESSONS[8], instrument)[0]
    demo = build_demo(exercise, instrument, step.stop_bar, tempo_factor=step.tempo_factor)
    notes = _sounding(demo)
    assert len(notes) == 8
    assert not step.wait_mode
    assert [right.timestamp_ms - left.timestamp_ms
            for left, right in zip(notes, notes[1:])] == [1000] * 7


def test_every_step_demo_and_saved_range_match_the_gp_score(instrument, tmp_path):
    paths = write_exercises(instrument, tmp_path / EXERCISE_DIRNAME)
    original_bytes = {path: path.read_bytes() for path in paths}
    original_exercises = deepcopy(build_exercises(instrument))
    for lesson, path in zip(LESSONS, paths):
        exercise = exercise_for_lesson(lesson, instrument)
        timeline = load_gp_file(path, track_index=0, instrument=instrument)
        for step in practice_steps(lesson, instrument):
            demo = build_demo(exercise, instrument, step.stop_bar - step.first_bar,
                              first_bar=step.first_bar, tempo_factor=step.tempo_factor)
            expected = [note for note in timeline.notes
                        if step.first_bar <= note.measure < step.stop_bar]
            sounding = _sounding(demo)
            assert demo.bars == step.stop_bar - step.first_bar
            assert [event.data1 for event in sounding] == [note.midi_note for note in expected]
            start = timeline.measures[step.first_bar].start_ms
            end = timeline.measures[step.stop_bar - 1].end_ms
            for event, note in zip(sounding, expected):
                assert event.timestamp_ms == pytest.approx(
                    demo.count_in_ms + (note.timestamp_ms - start) / step.tempo_factor)
            assert demo.duration_ms == pytest.approx(demo.count_in_ms + (end - start) / step.tempo_factor)
            assert sum(event.channel == 9 and event.event_type == NOTE_ON
                       for event in demo.track.events) == 4
            assert sum(event.channel == 0 and event.event_type == NOTE_OFF
                       for event in demo.track.events) == len(expected)
            state = practice_state(LessonPractice(path, step), timeline, instrument)
            assert state["position_ms"] == start
            assert state["loop_enabled"] == step.loop
            assert state["loop_start_ms"] == (start if step.loop else None)
            assert state["loop_end_ms"] == (end if step.loop else None)
            assert state["tempo_factor"] == step.tempo_factor
            assert state["wait_mode"] == step.wait_mode
    assert {path: path.read_bytes() for path in paths} == original_bytes
    assert build_exercises(instrument) == original_exercises


def test_full_guitar_open_strings_demo_reaches_all_twelve_bars():
    exercise = exercise_for_lesson(LESSONS[1], GUITAR)
    step = practice_steps(LESSONS[1], GUITAR)[-1]
    demo = build_demo(exercise, GUITAR, step.stop_bar)
    assert len(exercise.bars) == demo.bars == len(_sounding(demo)) == 12
    expected = [GUITAR.tuning[string] for string in GUITAR.strings_low_to_high()] * 2
    assert [event.data1 for event in _sounding(demo)] == expected
    assert demo.duration_ms == 52_000  # Four-beat count-in, twelve whole notes.


@pytest.mark.parametrize("bars", [0, -1, 1.5, 2.0, True, False, None, "2", float("nan"), float("inf")])
def test_demo_rejects_invalid_bar_counts(bars):
    exercise = exercise_for_lesson(LESSONS[1], GUITAR)
    with pytest.raises(ValueError, match="Invalid lesson demo"):
        build_demo(exercise, GUITAR, bars)


def test_demo_bounds_large_valid_counts_to_the_actual_end():
    exercise = exercise_for_lesson(LESSONS[1], GUITAR)
    demo = build_demo(exercise, GUITAR, 1_000_000, first_bar=10)
    assert demo.bars == 2
    assert [event.data1 for event in _sounding(demo)] == [59, 64]
    assert demo.duration_ms == 12_000


def test_unknown_lesson_does_not_acquire_an_accidental_practice_plan():
    assert practice_steps(Lesson(1, "unknown"), GUITAR) == ()
    assert practice_steps(Lesson(99, "open_string"), GUITAR) == ()


@pytest.mark.parametrize("language", ["de", "en"])
def test_new_step_copy_is_complete_and_language_specific(language):
    before = i18n.current_language()
    try:
        i18n.set_language(language)
        for lesson in LESSONS:
            for step in practice_steps(lesson, GUITAR):
                for suffix in ("title", "instruction"):
                    key = f"lesson.step.{step.key}.{suffix}"
                    text = getattr(step, suffix)
                    assert text == i18n.t(key)
                    assert text and text != key
                    assert "{" not in text and "\ufffd" not in text
    finally:
        i18n.set_language(before)
