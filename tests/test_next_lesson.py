"""Continue a lesson deliberately, without inventing a passing assessment."""
import os
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pygame
import pytest

from pickhero import curriculum, i18n
from pickhero.config import Config
from pickhero.curriculum import LESSONS, LessonPractice, PracticeStep, practice_steps
from pickhero.exercises import EXERCISE_DIRNAME, write_exercises
from pickhero.import_worker import encode_song
from pickhero.instruments import BASS_UKULELE, PROFILES
from pickhero.library import content_identity
from pickhero.ui.app import App
from pickhero.ui.colors import get_look, set_look
from pickhero.ui.scrolling import PlayingScreen


def key(code):
    return pygame.event.Event(pygame.KEYDOWN, key=code, mod=0, unicode="")


def screenshot_stats():
    return {"accuracy_percent": 75, "hits": 24, "total": 32, "in_time": 13,
            "close": 6, "misses": 2, "timing_assessed": True, "reliable": True,
            "timing_median_ms": -68}


@pytest.fixture(autouse=True)
def isolated_navigation(monkeypatch):
    pygame.font.init()
    previous_look, previous_language = get_look().key, i18n.current_language()
    monkeypatch.setattr(PlayingScreen, "_init_midi_player", lambda *args: None)
    monkeypatch.setattr(PlayingScreen, "_start_capture_only", lambda *args: None)
    monkeypatch.setattr(PlayingScreen, "_start_audio", lambda *args: None)
    yield
    set_look(previous_look)
    i18n.set_language(previous_language)


@pytest.fixture
def app_and_paths(tmp_path):
    paths = write_exercises(BASS_UKULELE, tmp_path / EXERCISE_DIRNAME)
    app = App(Config(instrument=BASS_UKULELE.key, songs_dir=str(tmp_path)))
    app._menu = SimpleNamespace(scan_files=Mock(), refresh_device_name=Mock())
    return app, paths


def finish_without_assessment(screen):
    """Place the real navigation in its end state without an audio device."""
    screen._song_completed = True
    screen._playback_ms = screen._timeline.duration_ms
    screen._playing = False
    screen._panel = None
    return screen._active_panel()


def assert_no_goal_or_attempt_added(progress, before):
    for identity, record in progress._data.items():
        old = before.get(identity)
        assert record.attempts == (old.attempts if old else 0)
        assert record.goal_pitch_met is (old.goal_pitch_met if old else False)
        assert record.goal_rhythm_met is (old.goal_rhythm_met if old else False)


@pytest.mark.parametrize("instrument", list(PROFILES.values()), ids=lambda item: item.key)
def test_next_request_follows_written_order_and_actual_preparations(tmp_path, instrument):
    folder = tmp_path / EXERCISE_DIRNAME
    # The resolver is pure: even missing files do not cause generation or writes.
    for index, lesson in enumerate(LESSONS):
        path = folder / lesson.filename
        steps = practice_steps(lesson, instrument)
        for step_index, step in enumerate(steps[:-1]):
            request = LessonPractice(path, step)
            assert curriculum.next_lesson_practice(path, instrument, request) == (
                LessonPractice(path, steps[step_index + 1]))
        following = (LessonPractice(folder / LESSONS[index + 1].filename,
                                    practice_steps(LESSONS[index + 1], instrument)[-1])
                     if index + 1 < len(LESSONS) else None)
        assert curriculum.next_lesson_practice(path, instrument) == following
        assert curriculum.next_lesson_practice(path, instrument,
                                               LessonPractice(path, steps[-1])) == following
    assert not folder.exists()


def test_unknown_song_and_invalid_preparation_do_not_guess_a_destination(tmp_path):
    instrument = BASS_UKULELE
    path = tmp_path / EXERCISE_DIRNAME / LESSONS[6].filename
    valid = practice_steps(LESSONS[6], instrument)[0]
    assert curriculum.next_lesson_practice(tmp_path / LESSONS[6].filename, instrument) is None
    assert curriculum.next_lesson_practice(path.with_name("my riff.gp5"), instrument) is None
    assert curriculum.next_lesson_practice(path, instrument, LessonPractice(
        path.with_name(LESSONS[5].filename), valid)) is None
    assert curriculum.next_lesson_practice(path, instrument, LessonPractice(
        path, PracticeStep("not_a_step", 0, 1, .5, True))) is None


@pytest.mark.parametrize("input_state", ["normal", "unreliable", "unscored"])
@pytest.mark.parametrize("activation", ["enter", "mouse"])
def test_library_seven_continues_to_eight_without_grading_or_revisiting_one(
        app_and_paths, input_state, activation):
    app, paths = app_and_paths
    app._load_song(paths[6], track_index=0)
    old = app._playing_screen
    # Match the screenshot's below-goal result. Earlier lessons remain open.
    app._progress.record_detailed_result(old._song_key, screenshot_stats(), [(3, 4, 50)], 1.0)
    old._matcher = SimpleNamespace(get_statistics=screenshot_stats)
    before = deepcopy(app._progress._data)
    if input_state == "unreliable":
        old._attempt_issues = ["input_disconnected"]
    elif input_state == "unscored":
        old._audio_enabled = False
    panel = finish_without_assessment(old)
    assert panel.actions[0].name == "next_lesson"
    assert panel.focus == 0
    assert app._lesson_return is None
    panel.render(pygame.Surface((1280, 720)))
    event = (key(pygame.K_RETURN) if activation == "enter" else
             pygame.event.Event(pygame.MOUSEBUTTONDOWN, button=1,
                                pos=panel.hit["next_lesson"].center))
    app._handle_playing_event(event)
    current = app._playing_screen
    assert app._state == "playing" and current is not old
    assert Path(current._song_path) == paths[7].resolve()
    assert current._track_index == 0
    assert not current._playing and current._resume_count_pending
    assert current._playback_ms == current._timeline.measures[0].start_ms
    assert current._tempo_factor == 1 and not current._wait_mode
    assert not current._loop_enabled and current._full_song_attempt
    assert app._lesson_return == LessonPractice(
        paths[7], practice_steps(LESSONS[7], BASS_UKULELE)[-1])
    assert_no_goal_or_attempt_added(app._progress, before)
    assert curriculum.build_learning_plan(BASS_UKULELE, app._songs_dir,
                                          app._progress).next_lesson.lesson == LESSONS[0]


@pytest.mark.parametrize("step_index", [0, 3])
def test_setup_continues_looping_preparation_without_waiting_for_song_end(
        app_and_paths, step_index):
    app, paths = app_and_paths
    steps = practice_steps(LESSONS[7], BASS_UKULELE)
    app._load_song(paths[7], track_index=0, practice=LessonPractice(paths[7], steps[step_index]))
    old = app._playing_screen
    assert old._loop_enabled and not old._song_completed
    before = deepcopy(app._progress._data)
    old._open_setup()
    panel = old._panel
    assert "next_lesson" in [action.name for action in panel.actions]
    panel.render(pygame.Surface((640, 480)))
    app._handle_playing_event(pygame.event.Event(
        pygame.MOUSEBUTTONDOWN, button=1, pos=panel.hit["next_lesson"].center))
    current = app._playing_screen
    target = steps[step_index + 1]
    assert current is not old
    assert app._lesson_return == LessonPractice(paths[7], target)
    assert current._loop_enabled is target.loop
    assert current._wait_mode is target.wait_mode
    assert current._tempo_factor == target.tempo_factor
    assert current._playback_ms == current._timeline.measures[target.first_bar].start_ms
    assert not current._playing and current._resume_count_pending
    assert_no_goal_or_attempt_added(app._progress, before)


def test_last_lesson_primary_action_opens_learning_path_without_wrapping(app_and_paths):
    app, paths = app_and_paths
    app._load_song(paths[-1], track_index=0)
    screen = app._playing_screen
    before = deepcopy(app._progress._data)
    panel = finish_without_assessment(screen)
    assert screen._next_lesson_practice is None
    assert panel.actions[0].name == "lessons"
    assert "next_lesson" not in [action.name for action in panel.actions]
    app._handle_playing_event(key(pygame.K_RETURN))
    assert app._state == "lessons" and app._playing_screen is None
    assert_no_goal_or_attempt_added(app._progress, before)


def test_own_song_has_no_curriculum_destination_even_with_matching_basename(app_and_paths):
    app, paths = app_and_paths
    own = app._songs_dir / paths[6].name
    own.write_bytes(paths[6].read_bytes())
    app._load_song(own, track_index=0)
    panel = finish_without_assessment(app._playing_screen)
    assert app._playing_screen._next_lesson_practice is None
    assert not {"next_lesson", "lessons"}.intersection(action.name for action in panel.actions)
    assert panel.actions[0].name == "replay"


def test_missing_destination_does_not_advertise_a_broken_next_button(app_and_paths):
    app, paths = app_and_paths
    paths[7].unlink()
    app._load_song(paths[6], track_index=0)
    panel = finish_without_assessment(app._playing_screen)
    assert app._playing_screen._next_lesson_practice is None
    assert "next_lesson" not in [action.name for action in panel.actions]


def test_home_keeps_explicit_replay_on_current_lesson(app_and_paths):
    app, paths = app_and_paths
    app._load_song(paths[6], track_index=0)
    screen = app._playing_screen
    finish_without_assessment(screen)
    before = deepcopy(app._progress._data)
    app._handle_playing_event(key(pygame.K_HOME))
    assert app._playing_screen is screen
    assert Path(screen._song_path) == paths[6].resolve()
    assert screen._playing and not screen._song_completed
    assert screen._count_in_target_ms == 0
    assert_no_goal_or_attempt_added(app._progress, before)


def test_completed_full_step_can_return_to_library_instead_of_same_step(app_and_paths):
    app, paths = app_and_paths
    request = LessonPractice(paths[6], practice_steps(LESSONS[6], BASS_UKULELE)[-1])
    app._load_song(paths[6], track_index=0, practice=request)
    panel = finish_without_assessment(app._playing_screen)
    panel.render(pygame.Surface((640, 480)))
    app._handle_playing_event(pygame.event.Event(
        pygame.MOUSEBUTTONDOWN, button=1, pos=panel.hit["menu"].center))
    assert app._state == "menu" and app._playing_screen is None
    app._menu.scan_files.assert_called_once()


def test_last_full_step_returns_to_overview_instead_of_its_detail(app_and_paths):
    app, paths = app_and_paths
    request = LessonPractice(paths[-1], practice_steps(LESSONS[-1], BASS_UKULELE)[-1])
    app._load_song(paths[-1], track_index=0, practice=request)
    finish_without_assessment(app._playing_screen)
    app._handle_playing_event(key(pygame.K_RETURN))
    assert app._state == "lessons"
    assert app._lesson_screen._mode == "catalog"


def test_stable_loop_offer_has_explicit_next_without_automatically_advancing(app_and_paths):
    app, paths = app_and_paths
    steps = practice_steps(LESSONS[7], BASS_UKULELE)
    app._load_song(paths[7], track_index=0, practice=LessonPractice(paths[7], steps[0]))
    screen = app._playing_screen
    before = deepcopy(app._progress._data)
    screen._section_advice = {"kind": "raise"}
    screen._panel = screen._advice_panel(.55)
    panel = screen._panel
    assert "next_lesson" in [action.name for action in panel.actions]
    assert panel.actions[panel.focus].name == "keep_tempo"
    assert app._playing_screen is screen
    panel.render(pygame.Surface((640, 480)))
    app._handle_playing_event(pygame.event.Event(
        pygame.MOUSEBUTTONDOWN, button=1, pos=panel.hit["next_lesson"].center))
    assert app._lesson_return == LessonPractice(paths[7], steps[1])
    assert app._playing_screen is not screen
    assert not app._playing_screen._playing
    assert_no_goal_or_attempt_added(app._progress, before)


@pytest.mark.parametrize("language", ["de", "en"])
@pytest.mark.parametrize("look", ["studio", "stage", "hifi"])
@pytest.mark.parametrize("size", [(320, 240), (640, 480), (1280, 720)])
def test_result_actions_remain_visible_and_keyboard_reachable(app_and_paths, language, look, size):
    app, paths = app_and_paths
    i18n.set_language(language)
    set_look(look)
    app._load_song(paths[6], track_index=0)
    screen = app._playing_screen
    screen._matcher = SimpleNamespace(get_statistics=screenshot_stats)
    screen._weakest_sections = [(3, 4, 50)]
    screen._recommendations = ["A long practice recommendation. " * 12]
    panel = finish_without_assessment(screen)
    canvas = pygame.Surface(size)
    panel.render(canvas)
    assert panel.actions[0].name == "next_lesson"
    names = [action.name for action in panel.actions]
    assert {"next_lesson", "replay", "weak", "menu"}.issubset(names)
    assert "lessons" not in names
    rects = [panel.hit[name] for name in names]
    assert all(canvas.get_rect().contains(rect) for rect in rects)
    assert all(not rect.colliderect(panel.body) for rect in rects)
    assert all(not left.colliderect(right) for index, left in enumerate(rects)
               for right in rects[index + 1:])
    assert panel.body.height > 0
    if destination := os.environ.get("PICKHERO_NEXT_PREVIEW_DIR"):
        folder = Path(destination)
        folder.mkdir(parents=True, exist_ok=True)
        pygame.image.save(canvas, folder / f"{language}-{look}-{size[0]}x{size[1]}.png")
    assert panel.handle_event(key(pygame.K_RETURN)) == "next_lesson"
    reached = set()
    for _ in names:
        reached.add(panel.handle_event(key(pygame.K_RETURN)))
        panel.handle_event(key(pygame.K_TAB))
    assert reached == set(names)


class FakeJob:
    def __init__(self, operation, path, instrument, **options):
        self.operation, self.path, self.options = operation, path, options
        self.status, self.error, self.result = {}, None, None
        self.cancelled = False

    def poll(self):
        return self.error is not None or self.result is not None

    def cancel(self):
        self.cancelled = True


def test_background_next_keeps_target_through_retry_and_discards_cancelled_step(
        app_and_paths, monkeypatch):
    from pickhero.tabs.loader import load_gp_file

    app, paths = app_and_paths
    app._load_song(paths[6], track_index=0)
    finish_without_assessment(app._playing_screen)
    monkeypatch.setattr("pickhero.ui.app.ImportJob", FakeJob)
    app._background = True
    before = deepcopy(app._progress._data)
    app._handle_playing_event(key(pygame.K_RETURN))
    request = LessonPractice(paths[7], practice_steps(LESSONS[7], BASS_UKULELE)[-1])
    first = app._import_job
    assert app._state == "import"
    assert first.operation == "load" and first.options["track_index"] == 0
    assert app._lesson_practice == request
    first.error = "Temporary import failure"
    app._poll_import()
    assert app._load_error is not None
    app._handle_load_error(key(pygame.K_r))
    retry = app._import_job
    assert retry is not first and app._lesson_practice == request
    retry.result = {"song": encode_song(load_gp_file(paths[7], instrument=BASS_UKULELE)),
                    "content_id": content_identity(paths[7])}
    app._poll_import()
    current = app._playing_screen
    assert Path(current._song_path) == paths[7].resolve()
    assert app._lesson_return == request and app._lesson_practice is None
    assert not current._playing and current._resume_count_pending
    assert_no_goal_or_attempt_added(app._progress, before)
    finish_without_assessment(current)
    app._handle_playing_event(key(pygame.K_RETURN))
    cancelled = app._import_job
    assert cancelled is not None
    app._handle_import_event(key(pygame.K_ESCAPE))
    assert cancelled.cancelled and app._state == "menu"
    assert app._lesson_practice is None
    # An ordinary file opened after cancellation must not inherit its step.
    own = app._songs_dir / "own.gp5"
    own.write_bytes(paths[0].read_bytes())
    app._background = False
    app._load_song(own, track_index=0)
    assert app._lesson_return is None
    assert app._playing_screen._next_lesson_practice is None
