"""Learning goals, audible examples and navigation without physical devices."""
from copy import deepcopy
import os
from pathlib import Path
from unittest.mock import Mock

import pygame
import pytest

from pickhero import i18n
from pickhero.audio.midi_playback import NOTE_OFF, NOTE_ON
from pickhero.config import Config
from pickhero.curriculum import (
    LESSONS,
    build_demo,
    build_learning_plan,
    exercise_for_lesson,
    lesson_for_path,
)
from pickhero.exercises import EXERCISE_DIRNAME, ensure_exercises, write_exercises
from pickhero.instruments import BASS_UKULELE, GUITAR, PROFILES
from pickhero.library import content_identity
from pickhero.progress import ProgressTracker, SongRecord, song_key
from pickhero.ui.colors import set_look
from pickhero.ui.lesson_menu import LessonScreen


@pytest.fixture(autouse=True)
def display_state():
    pygame.font.init()
    old_language = i18n.current_language()
    i18n.set_language("de")
    set_look("studio")
    yield
    i18n.set_language(old_language)
    set_look("studio")


def record(accuracy=90, tempo=1.0, timing=70, reliable=True, assessed=True):
    return SongRecord(best_accuracy=accuracy, attempts=1, last_played="2026-10-03", tempo_history=[{
        "attempt": 1, "accuracy": accuracy, "tempo_factor": tempo,
        "timing_percent": timing, "timing_assessed": assessed, "reliable": reliable,
    }])


def store(progress, directory, lesson, result, instrument=BASS_UKULELE):
    path = directory / EXERCISE_DIRNAME / lesson.filename
    identity = content_identity(path) if path.exists() else None
    progress._data[song_key(path, instrument.key, content_id=identity)] = result


def key(screen, code, mod=0):
    return screen.handle_event(pygame.event.Event(pygame.KEYDOWN, key=code, mod=mod))


def click(screen, action):
    return screen.handle_event(pygame.event.Event(
        pygame.MOUSEBUTTONDOWN, button=1, pos=screen._panel.hit[action].center))


def test_fresh_plan_has_ten_ordered_freely_available_instrument_lessons(tmp_path):
    tracker = ProgressTracker()
    plan = build_learning_plan(BASS_UKULELE, tmp_path, tracker)
    assert [state.lesson.order for state in plan.lessons] == list(range(1, 11))
    assert plan.next_lesson == plan.review == plan.lessons[0]
    assert plan.completed == 0
    assert tracker._data == {}
    for state in plan.lessons:
        assert lesson_for_path(state.path) == state.lesson
    assert lesson_for_path(tmp_path / "My songs" / LESSONS[0].filename) is None
    assert lesson_for_path(tmp_path / EXERCISE_DIRNAME / "01 not_an_exercise.gp5") is None


@pytest.mark.parametrize("language", ["de", "en"])
def test_all_lessons_have_complete_localized_goals_and_instructions(language):
    i18n.set_language(language)
    for lesson in LESSONS:
        for value in (lesson.title, lesson.goal, lesson.instruction, lesson.criterion):
            assert value and not value.startswith(("lesson.", "exercise."))
            assert "{" not in value


@pytest.mark.parametrize("accuracy,tempo,timing,reliable,assessed,complete", [
    (80, 1, 60, True, True, True),
    (79, 1, 100, True, True, False),
    (100, .99, 100, True, True, False),
    (100, 1, 59, True, True, False),
    (100, 1, 100, False, True, False),
    (100, 1, 100, True, False, False),
])
def test_rhythm_completion_requires_pitch_timing_tempo_and_signal(
        tmp_path, accuracy, tempo, timing, reliable, assessed, complete):
    tracker = ProgressTracker()
    store(tracker, tmp_path, LESSONS[2], record(accuracy, tempo, timing, reliable, assessed))
    assert build_learning_plan(BASS_UKULELE, tmp_path, tracker).lessons[2].complete is complete


def test_pitch_intro_has_no_unannounced_timing_goal(tmp_path):
    tracker = ProgressTracker()
    store(tracker, tmp_path, LESSONS[0], record(timing=0, assessed=False))
    plan = build_learning_plan(BASS_UKULELE, tmp_path, tracker)
    assert plan.lessons[0].complete
    assert plan.next_lesson == plan.lessons[1]
    assert plan.review == plan.lessons[0]


def test_legacy_unknown_quality_and_sections_do_not_certify_goals(tmp_path):
    tracker = ProgressTracker()
    old = record()
    old.tempo_history[0].pop("reliable")
    old.section_attempts = [{"accuracy_percent": 100, "tempo_factor": 1, "reliable": True}]
    store(tracker, tmp_path, LESSONS[0], old)
    assert not build_learning_plan(BASS_UKULELE, tmp_path, tracker).lessons[0].complete
    old.tempo_history.clear()
    assert not build_learning_plan(BASS_UKULELE, tmp_path, tracker).lessons[0].complete


def test_plan_is_read_only_and_instrument_specific(tmp_path):
    tracker = ProgressTracker()
    store(tracker, tmp_path, LESSONS[0], record(), instrument=GUITAR)
    previous = deepcopy(tracker._data)
    tracker._save = Mock(side_effect=AssertionError("Plan must not write history"))
    assert build_learning_plan(GUITAR, tmp_path, tracker).completed == 1
    assert build_learning_plan(BASS_UKULELE, tmp_path, tracker).completed == 0
    assert tracker._data == previous


def test_content_identity_follows_relocated_generated_lessons(tmp_path):
    old_dir = tmp_path / "old"
    paths = write_exercises(BASS_UKULELE, old_dir / EXERCISE_DIRNAME)
    tracker = ProgressTracker()
    store(tracker, old_dir, LESSONS[0], record())
    new_dir = tmp_path / "new" / EXERCISE_DIRNAME
    new_dir.mkdir(parents=True)
    for path in paths:
        (new_dir / path.name).write_bytes(path.read_bytes())
    assert build_learning_plan(BASS_UKULELE, new_dir.parent, tracker).completed == 1


def test_replacement_at_same_path_never_claims_old_learning_goal(tmp_path):
    paths = write_exercises(BASS_UKULELE, tmp_path / EXERCISE_DIRNAME)
    tracker = ProgressTracker()
    path = paths[0]
    tracker._data[song_key(path, BASS_UKULELE.key)] = record()
    # App/Menu migrates the old path history once; the learning plan must not
    # independently copy it again after different content appears here.
    tracker.migrate_content_identity(path, BASS_UKULELE.key, 0, content_identity(path))
    assert build_learning_plan(BASS_UKULELE, tmp_path, tracker).completed == 1
    path.write_bytes(b"different practice material")
    assert build_learning_plan(BASS_UKULELE, tmp_path, tracker).completed == 0


def test_regeneration_repairs_missing_lesson_and_keeps_custom_file(tmp_path):
    ensure_exercises(BASS_UKULELE, tmp_path)
    folder = tmp_path / EXERCISE_DIRNAME
    missing = folder / LESSONS[4].filename
    missing.unlink()
    custom = folder / "my own exercise.gp5"
    custom.write_bytes(b"preserve user material")
    assert ensure_exercises(BASS_UKULELE, tmp_path)
    assert missing.is_file()
    assert custom.read_bytes() == b"preserve user material"


def test_language_regeneration_retains_goals_but_different_instrument_does_not(tmp_path):
    tracker = ProgressTracker()
    ensure_exercises(BASS_UKULELE, tmp_path, tracker)
    store(tracker, tmp_path, LESSONS[0], record())
    original_key = next(iter(tracker._data))
    i18n.set_language("en")
    assert ensure_exercises(BASS_UKULELE, tmp_path, tracker)
    assert build_learning_plan(BASS_UKULELE, tmp_path, tracker).completed == 1
    assert original_key in tracker._data
    assert ensure_exercises(GUITAR, tmp_path, tracker)
    assert build_learning_plan(GUITAR, tmp_path, tracker).completed == 0
    assert ensure_exercises(BASS_UKULELE, tmp_path, tracker)
    assert build_learning_plan(BASS_UKULELE, tmp_path, tracker).completed == 1


def test_new_goal_survives_language_roundtrip_and_truncated_attempt_history(tmp_path):
    tracker = ProgressTracker()
    ensure_exercises(BASS_UKULELE, tmp_path, tracker)
    lesson = LESSONS[2]
    store(tracker, tmp_path, lesson, record(accuracy=50))
    i18n.set_language("en")
    ensure_exercises(BASS_UKULELE, tmp_path, tracker)
    path = tmp_path / EXERCISE_DIRNAME / lesson.filename
    key = song_key(path, BASS_UKULELE.key, content_id=content_identity(path))
    tracker.record_detailed_result(key, {"accuracy_percent": 95, "total": 32, "hits": 30,
        "timing_percent": 80, "timing_assessed": True, "reliable": True}, [], 1)
    achieved = tracker.get_best(key)
    achieved.tempo_history = record(accuracy=40).tempo_history * 50
    tracker._save()
    i18n.set_language("de")
    ensure_exercises(BASS_UKULELE, tmp_path, tracker)
    tracker = ProgressTracker()
    state = build_learning_plan(BASS_UKULELE, tmp_path, tracker).lessons[2]
    assert state.complete
    assert state.best_accuracy == 95
    assert state.attempts == 2


@pytest.mark.parametrize("instrument", list(PROFILES.values()), ids=lambda item: item.key)
def test_every_demo_plays_written_pitch_and_rhythm_for_this_instrument(instrument):
    for lesson in LESSONS:
        exercise = exercise_for_lesson(lesson, instrument)
        demo = build_demo(exercise, instrument)
        events = demo.track.events
        notes = [event for event in events if event.event_type == NOTE_ON and event.channel == 0]
        steps = [step for bar in exercise.bars[:2] for step in bar]
        assert [event.data1 for event in notes] == [instrument.tuning[s.string] + s.fret for s in steps]
        quarter = 60000 / exercise.bpm
        expected = 4 * quarter
        for step, event in zip(steps, notes):
            assert event.timestamp_ms == pytest.approx(expected)
            expected += quarter * 4 / step.value
        assert demo.duration_ms == pytest.approx(expected)
        assert sum(event.event_type == NOTE_ON and event.channel == 9 for event in events) == 4
        assert sum(event.event_type == NOTE_OFF and event.channel == 0 for event in events) == len(notes)


@pytest.mark.parametrize("look", ["studio", "stage", "hifi"])
@pytest.mark.parametrize("size", [(320, 240), (640, 480), (1280, 720)])
@pytest.mark.parametrize("language", ["de", "en"])
def test_three_views_have_visible_actions_scrollable_text_and_no_overlap(tmp_path, look, size, language):
    i18n.set_language(language)
    set_look(look)
    screen = LessonScreen(Config(instrument="bass_ukulele"), ProgressTracker(), tmp_path)
    surface = pygame.Surface(size)
    for mode in ("catalog", "detail"):
        screen._mode = mode
        screen._refresh_panel()
        screen.render(surface)
        panel = screen._panel
        actions = [panel.hit[action.name] for action in panel.actions]
        assert all(surface.get_rect().contains(rect) for rect in actions)
        assert all(not a.colliderect(b) for i, a in enumerate(actions) for b in actions[i + 1:])
        assert panel.body.height >= 20
        assert all(not rect.colliderect(panel.body) for rect in actions)
        if mode != "catalog" and size == (320, 240):
            assert panel.max_scroll > 0
            screen.handle_event(pygame.event.Event(pygame.MOUSEWHEEL, y=-10))
            assert panel.scroll > 0


def test_catalog_keyboard_and_wheel_reach_last_lesson_and_open_it(tmp_path):
    screen = LessonScreen(Config(), ProgressTracker(), tmp_path)
    surface = pygame.Surface((320, 240))
    screen.render(surface)
    click(screen, "catalog_down")
    assert screen._catalog_scroll > 0
    click(screen, "catalog_up")
    assert screen._catalog_scroll == 0
    key(screen, pygame.K_END)
    screen.render(surface)
    assert screen._selected == 9
    assert screen._panel.hit["lesson:9"].height > 0
    assert key(screen, pygame.K_RETURN) == tmp_path / EXERCISE_DIRNAME / LESSONS[9].filename
    key(screen, pygame.K_o)
    assert screen._mode == "detail"
    assert key(screen, pygame.K_p) == tmp_path / EXERCISE_DIRNAME / LESSONS[9].filename
    key(screen, pygame.K_ESCAPE)
    assert screen._mode == "catalog"
    screen.render(surface)
    screen.handle_event(pygame.event.Event(pygame.MOUSEWHEEL, y=100))
    screen.render(surface)
    assert "lesson:0" in screen._panel.hit
    click(screen, "lesson:0")
    assert screen._selected == 0 and screen._mode == "catalog"
    assert key(screen, pygame.K_RETURN) == tmp_path / EXERCISE_DIRNAME / LESSONS[0].filename


def test_catalog_and_detail_mouse_navigation_and_tab_focus(tmp_path):
    screen = LessonScreen(Config(), ProgressTracker(), tmp_path)
    surface = pygame.Surface((640, 480))
    screen.render(surface)
    click(screen, "open")
    assert screen._mode == "detail"
    screen.render(surface)
    key(screen, pygame.K_TAB, pygame.KMOD_SHIFT)
    key(screen, pygame.K_SPACE)
    assert screen._mode == "catalog"
    screen.render(surface)
    assert click(screen, "back") == "back"


def test_demo_is_explicit_unscored_stoppable_and_closed_before_practice(tmp_path, monkeypatch):
    tracker = ProgressTracker()
    tracker._save = Mock(side_effect=AssertionError("Listening must not write history"))
    player = Mock()
    player.open.return_value = True
    factory = Mock(return_value=player)
    monkeypatch.setattr("pickhero.ui.lesson_menu.MidiPlayer", factory)
    clock = [10.0]
    monkeypatch.setattr("pickhero.ui.lesson_menu.time.monotonic", lambda: clock[0])
    screen = LessonScreen(Config(instrument="bass_ukulele"), tracker, tmp_path)
    key(screen, pygame.K_o)
    factory.assert_not_called()
    key(screen, pygame.K_d)
    assert screen._demo_status == "playing"
    assert any(event.channel == 0 and event.event_type == NOTE_ON
               for event in factory.call_args.args[0].events)
    clock[0] += 1.25
    screen.update(.01)
    player.update.assert_called_with(1250.0)
    result = key(screen, pygame.K_p)
    assert isinstance(result, Path)
    player.close.assert_called_once()
    assert screen._player is None
    assert tracker._data == {}


def test_example_finishes_and_missing_midi_is_explained(tmp_path, monkeypatch):
    player = Mock()
    player.open.return_value = True
    monkeypatch.setattr("pickhero.ui.lesson_menu.MidiPlayer", lambda track: player)
    clock = [0.0]
    monkeypatch.setattr("pickhero.ui.lesson_menu.time.monotonic", lambda: clock[0])
    screen = LessonScreen(Config(), ProgressTracker(), tmp_path)
    key(screen, pygame.K_o)
    key(screen, pygame.K_d)
    clock[0] = screen._demo.duration_ms / 1000
    screen.update()
    assert screen._demo_status == "done" and screen._player is None
    player.open.return_value = False
    key(screen, pygame.K_d)
    assert screen._demo_status == "unavailable" and screen._player is None
    assert "MIDI" in screen._status_text()


@pytest.mark.parametrize("language", ["de", "en"])
@pytest.mark.parametrize("look", ["studio", "stage", "hifi"])
@pytest.mark.parametrize("size", [(320, 240), (640, 480), (1280, 720)])
def test_learning_path_opens_compact_freely_playable_catalog(tmp_path, language, look, size):
    i18n.set_language(language)
    set_look(look)
    tracker = ProgressTracker()
    store(tracker, tmp_path, LESSONS[0], record())
    store(tracker, tmp_path, LESSONS[6], record(accuracy=75, timing=54))
    screen = LessonScreen(Config(instrument="bass_ukulele"), tracker, tmp_path)
    before = deepcopy(tracker._data)
    tracker._save = Mock(side_effect=AssertionError("Opening a lesson is not progress"))
    surface = pygame.Surface(size)
    screen.render(surface)
    assert screen._mode == "catalog"
    assert not screen._panel.metrics and not screen._panel.sections
    assert screen.plan.completed == 1
    assert not screen.plan.lessons[6].complete
    assert all(not action.label.startswith("lesson.") for action in screen._panel.actions)
    if size == (1280, 720):
        assert screen._catalog_scroll == 0
        assert all(f"lesson:{index}" in screen._panel.hit for index in range(10))
        assert all(screen._panel.hit[f"lesson:{index}"].height == screen._row_height - 4
                   for index in range(10))
    preview_dir = os.environ.get("PICKHERO_CATALOG_PREVIEW_DIR")
    if preview_dir:
        destination = Path(preview_dir)
        destination.mkdir(parents=True, exist_ok=True)
        pygame.image.save(surface, str(destination / f"catalog-{look}-{language}-{size[0]}x{size[1]}.png"))
    # No completion unlocks or blocks any exercise; every row launches itself.
    key(screen, pygame.K_HOME)
    for index, state in enumerate(screen.plan.lessons):
        screen.render(surface)
        assert f"lesson:{index}" in screen._panel.hit
        assert key(screen, pygame.K_RETURN) == state.path
        key(screen, pygame.K_DOWN)
    assert tracker._data == before
    tracker._save.assert_not_called()


def test_initial_selection_uses_recent_visit_without_reopening_earliest_unmet_goal(tmp_path):
    tracker = ProgressTracker()
    store(tracker, tmp_path, LESSONS[6], record(accuracy=75, timing=54))
    current = tmp_path / EXERCISE_DIRNAME / LESSONS[7].filename
    tracker.save_session(song_key(current, BASS_UKULELE.key), {
        "song_path": str(current), "instrument": BASS_UKULELE.key,
        "track_index": 0, "position_ms": 0,
    })
    before = deepcopy(tracker._data)
    screen = LessonScreen(Config(instrument=BASS_UKULELE.key), tracker, tmp_path)
    assert screen._selected == 7
    assert screen.plan.next_lesson.lesson.order == 1
    assert key(screen, pygame.K_RETURN) == current
    assert tracker._data == before


def test_foreign_instrument_or_unrelated_session_does_not_select_another_path(tmp_path):
    tracker = ProgressTracker()
    recent = record(accuracy=75)
    recent.last_played = "2026-10-03T12:00:00"
    older = record()
    older.last_played = "2026-10-02T12:00:00"
    store(tracker, tmp_path, LESSONS[6], recent)
    store(tracker, tmp_path, LESSONS[0], older)
    for instrument in (GUITAR.key, BASS_UKULELE.key):
        current = tmp_path / "another-folder" / LESSONS[9].filename
        tracker.save_session(song_key(current, instrument), {
            "song_path": str(current), "instrument": instrument,
            "track_index": 0, "position_ms": 0,
        })
        screen = LessonScreen(Config(instrument=BASS_UKULELE.key), tracker, tmp_path)
        assert screen._selected == 6


def test_mouse_selection_and_double_click_launch_the_chosen_lesson(tmp_path, monkeypatch):
    clock = [10.0]
    monkeypatch.setattr("pickhero.ui.lesson_menu.time.monotonic", lambda: clock[0])
    tracker = ProgressTracker()
    screen = LessonScreen(Config(), tracker, tmp_path)
    screen.render(pygame.Surface((1280, 720)))
    assert click(screen, "lesson:7") is None
    assert screen._selected == 7 and screen._mode == "catalog"
    assert click(screen, "play") == screen.plan.lessons[7].path
    assert click(screen, "lesson:2") is None
    clock[0] += .1
    result = click(screen, "lesson:2")
    assert result == screen.plan.lessons[2].path
    assert click(screen, "lesson:2") is None
    clock[0] += .5
    assert click(screen, "lesson:2") is None
    assert tracker._data == {}


@pytest.mark.parametrize("look", ["studio", "stage", "hifi"])
def test_resize_keeps_keyboard_selected_lesson_visible_without_blocking_wheel(tmp_path, look):
    set_look(look)
    screen = LessonScreen(Config(), ProgressTracker(), tmp_path)
    key(screen, pygame.K_END)
    screen.render(pygame.Surface((1280, 720)))
    screen.render(pygame.Surface((320, 240)))
    assert "lesson:9" in screen._panel.hit
    screen.handle_event(pygame.event.Event(pygame.MOUSEWHEEL, y=100))
    screen.render(pygame.Surface((320, 240)))
    assert "lesson:0" in screen._panel.hit
