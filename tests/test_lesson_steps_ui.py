"""A beginner can choose, hear and launch lesson passages without a device."""
import os
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pygame
import pytest

from pickhero import i18n
from pickhero.audio.midi_playback import NOTE_ON
from pickhero.config import Config
from pickhero.curriculum import (
    LESSONS,
    LessonPractice,
    exercise_for_lesson,
    practice_steps,
)
from pickhero.instruments import BASS_UKULELE
from pickhero.progress import ProgressTracker
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


def key(screen, code, mod=0):
    return screen.handle_event(pygame.event.Event(pygame.KEYDOWN, key=code, mod=mod))


def click(screen, action):
    return screen.handle_event(pygame.event.Event(
        pygame.MOUSEBUTTONDOWN, button=1, pos=screen._panel.hit[action].center))


def open_lesson(tmp_path, index, *, progress=None):
    screen = LessonScreen(Config(instrument="bass_ukulele"), progress or ProgressTracker(), tmp_path)
    key(screen, pygame.K_HOME)
    for _ in range(index):
        key(screen, pygame.K_DOWN)
    key(screen, pygame.K_o)
    assert screen._selected == index and screen._mode == "detail"
    return screen


def open_scale(tmp_path, *, progress=None):
    return open_lesson(tmp_path, 7, progress=progress)


@pytest.mark.parametrize("lesson", LESSONS, ids=lambda lesson: lesson.slug)
def test_steps_are_optional_and_normal_play_still_returns_whole_lesson(tmp_path, lesson):
    screen = open_lesson(tmp_path, lesson.order - 1)
    complete_path = key(screen, pygame.K_p)
    assert isinstance(complete_path, Path) and complete_path.name == lesson.filename
    key(screen, pygame.K_s)
    assert screen._mode == "steps"
    request = key(screen, pygame.K_RETURN)
    assert isinstance(request, LessonPractice) and request.path == complete_path
    assert request.step.loop
    key(screen, pygame.K_ESCAPE)
    assert screen._mode == "detail"
    assert key(screen, pygame.K_p) == complete_path
    key(screen, pygame.K_ESCAPE)
    assert screen._mode == "catalog"


def test_arrows_choose_each_exact_passage_and_do_not_wrap(tmp_path):
    screen = open_scale(tmp_path)
    key(screen, pygame.K_s)
    key(screen, pygame.K_LEFT)
    expected = [(0, 1, .5, True, True), (1, 2, .5, True, True),
                (0, 2, .75, False, True), (2, 4, .75, False, True),
                (0, 8, 1.0, False, False)]
    for first, stop, tempo, wait, loop in expected:
        request = key(screen, pygame.K_p)
        assert isinstance(request, LessonPractice)
        assert request.path.name == LESSONS[7].filename
        assert (request.step.first_bar, request.step.stop_bar, request.step.tempo_factor,
                request.step.wait_mode, request.step.loop) == (first, stop, tempo, wait, loop)
        key(screen, pygame.K_RIGHT)
    assert screen._step_index == 4
    key(screen, pygame.K_LEFT)
    assert screen._step_index == 3
    key(screen, pygame.K_ESCAPE)
    key(screen, pygame.K_s)
    assert screen._step_index == 3


@pytest.mark.parametrize("lesson", LESSONS, ids=lambda lesson: lesson.slug)
def test_listening_plays_selected_notes_and_tempo_and_never_writes_progress(
        tmp_path, monkeypatch, lesson):
    tracker = ProgressTracker()
    tracker._save = Mock(side_effect=AssertionError("An example cannot save a result"))
    players = []

    def player_factory(track):
        player = Mock()
        player.open.return_value = True
        player.track = track
        players.append(player)
        return player

    monkeypatch.setattr("pickhero.ui.lesson_menu.MidiPlayer", player_factory)
    screen = open_lesson(tmp_path, lesson.order - 1, progress=tracker)
    key(screen, pygame.K_d)
    detail_player = players[-1]
    key(screen, pygame.K_s)
    detail_player.close.assert_called_once()
    assert screen._player is None
    exercise = exercise_for_lesson(lesson, BASS_UKULELE)
    steps = practice_steps(lesson, BASS_UKULELE)
    for index in range(len(steps)):
        request = key(screen, pygame.K_p)
        step = request.step
        key(screen, pygame.K_d)
        player = players[-1]
        notes = [event for event in player.track.events
                 if event.channel == 0 and event.event_type == NOTE_ON]
        expected_steps = [note for bar in exercise.bars[step.first_bar:step.stop_bar]
                          for note in bar]
        assert [event.data1 for event in notes] == [
            BASS_UKULELE.tuning[note.string] + note.fret for note in expected_steps]
        quarter = 60000 / (exercise.bpm * step.tempo_factor)
        onset = 4 * quarter
        for note, written in zip(notes, expected_steps):
            assert note.timestamp_ms == pytest.approx(onset)
            onset += quarter * 4 / written.value
        if index < len(steps) - 1:
            key(screen, pygame.K_RIGHT)
            assert screen._player is None and screen._demo_status == "idle"
        else:
            key(screen, pygame.K_p)
        player.close.assert_called_once()
    assert tracker._data == {}
    tracker._save.assert_not_called()


def test_demo_finish_and_manual_stop_keep_the_current_step(tmp_path, monkeypatch):
    player = Mock()
    player.open.return_value = True
    monkeypatch.setattr("pickhero.ui.lesson_menu.MidiPlayer", lambda track: player)
    clock = [10.0]
    monkeypatch.setattr("pickhero.ui.lesson_menu.time.monotonic", lambda: clock[0])
    screen = open_scale(tmp_path)
    key(screen, pygame.K_s)
    key(screen, pygame.K_RIGHT)
    key(screen, pygame.K_d)
    clock[0] += screen._demo.duration_ms / 1000 + .001
    screen.update()
    assert screen._demo_status == "done" and screen._step_index == 1
    key(screen, pygame.K_d)
    key(screen, pygame.K_d)
    assert screen._demo_status == "idle" and screen._step_index == 1
    assert screen._player is None


@pytest.mark.parametrize("lesson", LESSONS, ids=lambda lesson: lesson.slug)
def test_return_from_practice_reopens_exact_step_without_sound_or_progress(tmp_path, monkeypatch, lesson):
    origin = open_lesson(tmp_path, lesson.order - 1)
    key(origin, pygame.K_s)
    player = Mock(side_effect=AssertionError("Returning must not start a demo"))
    monkeypatch.setattr("pickhero.ui.lesson_menu.MidiPlayer", player)
    tracker = ProgressTracker()
    tracker._save = Mock(side_effect=AssertionError("Navigation must not save progress"))
    for index in range(len(practice_steps(lesson, BASS_UKULELE))):
        request = key(origin, pygame.K_p)
        returned = LessonScreen(Config(instrument="bass_ukulele"), tracker, tmp_path)
        assert returned.show_practice(request)
        assert returned._mode == "steps" and returned._step_index == index
        assert key(returned, pygame.K_RETURN) == request
        key(returned, pygame.K_ESCAPE)
        assert returned._mode == "detail"
        key(returned, pygame.K_ESCAPE)
        assert returned._mode == "catalog"
        key(origin, pygame.K_RIGHT)
    player.assert_not_called()
    tracker._save.assert_not_called()


def test_return_rejects_unknown_step_or_path_without_changing_navigation(tmp_path):
    screen = open_scale(tmp_path)
    key(screen, pygame.K_s)
    request = key(screen, pygame.K_p)
    returned = LessonScreen(Config(instrument="bass_ukulele"), ProgressTracker(), tmp_path)
    invalid = [replace(request, path=tmp_path / "unknown.gp5"),
               replace(request, step=replace(request.step, tempo_factor=.3)),
               replace(request, step=replace(request.step, first_bar=7)), None]
    for item in invalid:
        assert not returned.show_practice(item)
        assert returned._mode == "catalog" and returned._player is None


@pytest.mark.parametrize("control", ["wheel", "down", "page", "button"])
def test_small_step_scroll_never_skips_an_unread_line(tmp_path, control):
    set_look("stage")
    screen = open_scale(tmp_path)
    key(screen, pygame.K_s)
    screen.render(pygame.Surface((320, 240)))
    panel = screen._panel
    if control == "wheel":
        screen.handle_event(pygame.event.Event(pygame.MOUSEWHEEL, y=-1))
    elif control == "button":
        click(screen, "scroll_down")
    else:
        key(screen, pygame.K_DOWN if control == "down" else pygame.K_PAGEDOWN)
    assert 0 < panel.scroll <= panel.body.height // 2


def panel_text(panel):
    return "\n".join([panel.title, panel.subtitle] + [
        text for heading, paragraphs in panel.sections for text in (heading, *paragraphs)])


@pytest.mark.parametrize("language", ["de", "en"])
def test_every_step_names_its_lesson_and_has_complete_localized_guidance(tmp_path, language):
    i18n.set_language(language)
    for lesson in LESSONS:
        screen = open_lesson(tmp_path, lesson.order - 1)
        key(screen, pygame.K_s)
        for index, step in enumerate(practice_steps(lesson, BASS_UKULELE)):
            panel = screen._panel
            assert f"{lesson.order:02d}" in panel.title
            assert str(index + 1) in panel.title
            assert panel.subtitle == lesson.title
            assert panel.sections[0][0] == step.title
            if step.loop:
                assert panel.sections[0][1] == [step.instruction]
            else:
                # Completion targets must be prominent, before setup advice.
                assert panel.sections[0][1][0] == lesson.criterion
            text = panel_text(panel)
            assert not any(line.startswith(("lesson.", "exercise.")) for line in text.splitlines())
            assert "{" not in text
            assert str(round(step.tempo_factor * 100)) in text
            assert "BPM" in text
            if step.loop:
                assert lesson.criterion not in text
            else:
                assert lesson.criterion in text
            key(screen, pygame.K_RIGHT)


@pytest.mark.parametrize("language", ["de", "en"])
@pytest.mark.parametrize("lesson", LESSONS[:2], ids=lambda lesson: lesson.slug)
def test_first_two_full_attempts_show_pitch_goal_without_rhythm_requirement(tmp_path, language, lesson):
    i18n.set_language(language)
    screen = open_lesson(tmp_path, lesson.order - 1)
    key(screen, pygame.K_s)
    steps = practice_steps(lesson, BASS_UKULELE)
    for _ in range(len(steps) - 1):
        key(screen, pygame.K_RIGHT)
    assert not key(screen, pygame.K_p).step.loop
    text = panel_text(screen._panel)
    assert i18n.t("lesson.criterion_pitch") in text
    assert i18n.t("lesson.step_pitch_only") in text
    assert i18n.t("lesson.criterion_rhythm") not in text
    assert i18n.t("lesson.step_timed") not in text


@pytest.mark.parametrize("language", ["de", "en"])
def test_rhythm_full_attempt_keeps_its_timing_goal(tmp_path, language):
    i18n.set_language(language)
    lesson = LESSONS[2]
    screen = open_lesson(tmp_path, lesson.order - 1)
    key(screen, pygame.K_s)
    for _ in range(len(practice_steps(lesson, BASS_UKULELE)) - 1):
        key(screen, pygame.K_RIGHT)
    text = panel_text(screen._panel)
    assert i18n.t("lesson.criterion_rhythm") in text
    assert i18n.t("lesson.step_timed") in text
    assert i18n.t("lesson.step_pitch_only") not in text


@pytest.mark.parametrize("language", ["de", "en"])
def test_demo_status_and_errors_are_brought_into_view_after_scrolling(tmp_path, monkeypatch, language):
    i18n.set_language(language)
    screen = open_scale(tmp_path)
    key(screen, pygame.K_s)
    surface = pygame.Surface((320, 240))
    screen.render(surface)
    key(screen, pygame.K_END)
    assert screen._panel.scroll > 0
    player = Mock()
    player.open.return_value = False
    monkeypatch.setattr("pickhero.ui.lesson_menu.MidiPlayer", lambda track: player)
    clock = [10.0]
    monkeypatch.setattr("pickhero.ui.lesson_menu.time.monotonic", lambda: clock[0])
    key(screen, pygame.K_d)
    assert screen._demo_status == "unavailable"
    assert screen._panel.scroll == 0
    assert "MIDI" in screen._panel.subtitle
    assert "lesson." not in screen._panel.subtitle
    assert screen._panel.subtitle != LESSONS[7].title
    player.open.return_value = True
    screen.render(surface)
    key(screen, pygame.K_END)
    key(screen, pygame.K_d)
    assert screen._panel.scroll == 0
    assert screen._demo_status == "playing"
    playing_status = screen._panel.subtitle
    assert playing_status != LESSONS[7].title and "lesson." not in playing_status
    screen.render(surface)
    key(screen, pygame.K_END)
    clock[0] += screen._demo.duration_ms / 1000 + .001
    screen.update()
    assert screen._demo_status == "done" and screen._panel.scroll == 0
    assert screen._panel.subtitle not in (LESSONS[7].title, playing_status)
    assert "lesson." not in screen._panel.subtitle
    key(screen, pygame.K_d)
    key(screen, pygame.K_d)
    assert screen._panel.subtitle == LESSONS[7].title


@pytest.mark.parametrize("passage", ["first", "full"])
@pytest.mark.parametrize("language", ["de", "en"])
@pytest.mark.parametrize("size", [(320, 240), (640, 480), (1280, 720)])
@pytest.mark.parametrize("look", ["studio", "stage", "hifi"])
def test_step_actions_are_visible_readable_and_usable_in_every_look(
        tmp_path, monkeypatch, language, size, look, passage):
    i18n.set_language(language)
    set_look(look)
    lesson = max(LESSONS, key=lambda item: len(item.title))
    screen = open_lesson(tmp_path, lesson.order - 1)
    surface = pygame.Surface(size)
    screen.render(surface)
    click(screen, "steps")
    steps = practice_steps(lesson, BASS_UKULELE)
    if passage == "full":
        for _ in range(len(steps) - 1):
            key(screen, pygame.K_RIGHT)
    player = Mock()
    player.open.return_value = True
    monkeypatch.setattr("pickhero.ui.lesson_menu.MidiPlayer", lambda track: player)
    clock = [10.0]
    monkeypatch.setattr("pickhero.ui.lesson_menu.time.monotonic", lambda: clock[0])
    for status in ("idle", "playing", "done", "unavailable"):
        if status == "playing":
            key(screen, pygame.K_d)
        elif status == "done":
            clock[0] += screen._demo.duration_ms / 1000 + .001
            screen.update()
        elif status == "unavailable":
            player.open.return_value = False
            key(screen, pygame.K_d)
        screen.render(surface)
        panel = screen._panel
        assert len(panel.actions) == 5
        assert not panel.title.startswith("lesson.") and "{" not in panel.title
        assert screen._demo_status == status
        assert panel.subtitle and not panel.subtitle.startswith("lesson.")
        for heading, paragraphs in panel.sections:
            assert not heading.startswith("lesson.")
            assert all(not text.startswith("lesson.") and "{" not in text for text in paragraphs)
        actions = [panel.hit[action.name] for action in panel.actions]
        assert all(surface.get_rect().contains(rect) for rect in actions)
        assert all(not a.colliderect(b) for i, a in enumerate(actions) for b in actions[i + 1:])
        assert panel.body.height >= 20
        assert all(not rect.colliderect(panel.body) for rect in actions)
        if size == (320, 240):
            assert panel.max_scroll > 0
            screen.handle_event(pygame.event.Event(pygame.MOUSEWHEEL, y=-1))
            assert panel.scroll > 0
        preview_dir = os.environ.get("PICKHERO_LESSON_PREVIEW_DIR")
        if preview_dir:
            destination = Path(preview_dir)
            destination.mkdir(parents=True, exist_ok=True)
            pygame.image.save(surface, str(destination / (
                f"steps-{look}-{language}-{size[0]}x{size[1]}-{lesson.order:02d}-{passage}-{status}.png")))
    for _ in range(len(steps) - 1):
        key(screen, pygame.K_LEFT)
    screen.render(surface)
    click(screen, "step_next")
    assert screen._step_index == 1 and screen._player is None
    screen.render(surface)
    click(screen, "step_previous")
    assert screen._step_index == 0
    screen.render(surface)
    assert isinstance(click(screen, "play"), LessonPractice)
    click(screen, "back")
    assert screen._mode == "detail"
    screen.render(surface)
    click(screen, "steps")
    screen.render(surface)
    click(screen, "header_close")
    assert screen._mode == "detail"
