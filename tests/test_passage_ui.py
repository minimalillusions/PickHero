"""Mouse/keyboard passage editing and explicit practice choices, without devices."""
import os
from pathlib import Path
from unittest.mock import Mock

import pygame
import pytest

from pickhero import i18n
from pickhero.progress import ProgressTracker
from pickhero.tabs.timeline import MeasureInfo
from pickhero.ui.colors import set_look
from pickhero.ui.passage_menu import PassagePanel
from tests import test_timing_clock
from tests.test_named_passages import identity, state

clock_screen = test_timing_clock.clock_screen


@pytest.fixture(autouse=True)
def display():
    pygame.font.init()
    language = i18n.current_language()
    i18n.set_language("de")
    set_look("studio")
    yield
    i18n.set_language(language)
    set_look("studio")


def key(panel, code, mod=0):
    return panel.handle_event(pygame.event.Event(pygame.KEYDOWN, key=code, mod=mod))


def type_text(panel, value):
    return panel.handle_event(pygame.event.Event(pygame.TEXTINPUT, text=value))


def click(panel, action):
    return panel.handle_event(pygame.event.Event(
        pygame.MOUSEBUTTONDOWN, button=1, pos=panel.hit[action].center))


def attach(screen):
    screen._song_key = identity()
    screen._song_path = state()["song_path"]
    screen._progress_tracker = ProgressTracker()
    screen._timeline._measures = [MeasureInfo(i, i * 2000, (i + 1) * 2000) for i in range(6)]


def test_typing_saving_renaming_opening_and_confirming_deletion():
    tracker = ProgressTracker()
    panel = PassagePanel(tracker, identity(), state(), "A song")
    key(panel, pygame.K_s)
    type_text(panel, "Refrain")
    key(panel, pygame.K_RETURN)
    item = tracker.saved_passages(identity())[0]
    assert item["name"] == "Refrain" and panel.mode == "detail"
    assert key(panel, pygame.K_o) == "open_passage:" + item["id"]
    key(panel, pygame.K_r)
    type_text(panel, "Refrain langsam")
    key(panel, pygame.K_HOME)
    type_text(panel, "A: ")
    key(panel, pygame.K_RETURN)
    assert tracker.saved_passages(identity())[0]["name"] == "A: Refrain langsam"
    key(panel, pygame.K_DELETE)
    assert panel.mode == "delete" and tracker.saved_passages(identity())
    key(panel, pygame.K_ESCAPE)
    assert panel.mode == "detail" and tracker.saved_passages(identity())
    key(panel, pygame.K_DELETE)
    key(panel, pygame.K_RETURN)  # Confirmation defaults to cancel.
    assert panel.mode == "detail" and tracker.saved_passages(identity())
    key(panel, pygame.K_DELETE)
    key(panel, pygame.K_DELETE)
    assert not tracker.saved_passages(identity())
    assert panel.mode == "list"
    assert key(panel, pygame.K_ESCAPE) == "close"


def test_names_validate_duplicates_length_and_cancellation():
    tracker = ProgressTracker()
    tracker.save_passage(identity(), "Refrain", state())
    panel = PassagePanel(tracker, identity(), state(), "A song")
    key(panel, pygame.K_s)
    type_text(panel, "Refrain")
    key(panel, pygame.K_RETURN)
    assert panel.error == "duplicate" and panel.mode == "name"
    key(panel, pygame.K_a, pygame.KMOD_CTRL)
    type_text(panel, "x" * 100)
    assert len(panel.name) == 60
    key(panel, pygame.K_a, pygame.KMOD_CTRL)
    key(panel, pygame.K_BACKSPACE)
    key(panel, pygame.K_RETURN)
    assert panel.error == "name"
    key(panel, pygame.K_ESCAPE)
    assert len(tracker.saved_passages(identity())) == 1


def test_arrow_selection_names_the_selected_passage_before_scrolling():
    tracker = ProgressTracker()
    tracker.save_passage(identity(), "Strophe", state())
    tracker.save_passage(identity(), "Refrain", state())
    panel = PassagePanel(tracker, identity(), state(), "A song")
    assert panel.subtitle == "Strophe"
    key(panel, pygame.K_RIGHT)
    assert panel.subtitle == "Refrain"
    assert panel.scroll == 0


@pytest.mark.parametrize("language", ["de", "en"])
@pytest.mark.parametrize("look", ["studio", "stage", "hifi"])
@pytest.mark.parametrize("size", [(320, 240), (640, 480), (1280, 720)])
def test_all_passage_modes_have_visible_mouse_controls_and_readable_scrolling(language, look, size):
    i18n.set_language(language)
    set_look(look)
    tracker = ProgressTracker()
    tracker.save_passage(identity(), "Refrain", state())
    panel = PassagePanel(tracker, identity(), state(), "Open strings up and down")
    surface = pygame.Surface(size)
    for mode in ("list", "name", "detail", "delete"):
        if mode == "name":
            panel.render(surface)
            click(panel, "new")
        elif mode == "detail":
            type_text(panel, "Solo")
            panel.render(surface)
            click(panel, "save")
        elif mode == "delete":
            panel.render(surface)
            click(panel, "delete")
        panel.render(surface)
        assert panel.mode == mode
        assert not panel.title.startswith("passage.")
        rects = [panel.hit[action.name] for action in panel.actions]
        assert all(surface.get_rect().contains(rect) for rect in rects)
        assert all(not a.colliderect(b) for index, a in enumerate(rects) for b in rects[index + 1:])
        assert panel.body.height >= 20
        assert all(not rect.colliderect(panel.body) for rect in rects)
        if panel.max_scroll and panel.body.height < 84:
            panel.handle_event(pygame.event.Event(pygame.MOUSEWHEEL, y=-1))
            assert 0 < panel.scroll <= panel.body.height // 2
        folder = os.environ.get("PICKHERO_PASSAGE_PREVIEW_DIR")
        if folder:
            Path(folder).mkdir(parents=True, exist_ok=True)
            pygame.image.save(surface, str(Path(folder) / f"passages-{language}-{look}-{size[0]}-{mode}.png"))
    click(panel, "cancel")
    assert panel.mode == "detail" and len(tracker.saved_passages(identity())) == 2


def test_open_saved_passage_pauses_and_restores_selected_settings(clock_screen):
    _, screen, _ = clock_screen()
    attach(screen)
    saved = state(tempo_factor=.4, wait_mode=True, active_strings=[True, False, True, True])
    identifier = screen._progress_tracker.save_passage(identity(), "Solo", saved)
    key(screen, pygame.K_n)
    assert isinstance(screen._panel, PassagePanel)
    key(screen, pygame.K_RETURN)
    assert screen._panel.mode == "detail"
    key(screen, pygame.K_o)
    assert screen._panel is None and not screen.is_playing()
    assert screen._tempo_factor == .4 and screen._wait_mode
    assert screen._loop_start_ms == 1000 and screen._loop_end_ms == 5000
    assert screen._active_strings == [True, False, True, True]
    assert screen._resume_count_pending
    assert screen._progress_tracker.saved_passages(identity())[0]["id"] == identifier


@pytest.mark.parametrize("transition", ["stop_audio", "suspend_for_setup", "_open_setup"])
def test_leaving_name_input_always_stops_text_input(clock_screen, monkeypatch, transition):
    _, screen, _ = clock_screen()
    attach(screen)
    screen._midi_player = None
    monkeypatch.setattr(pygame, "get_init", lambda: True)
    monkeypatch.setattr(pygame.key, "start_text_input", Mock())
    stop = Mock()
    monkeypatch.setattr(pygame.key, "stop_text_input", stop)
    key(screen, pygame.K_n)
    key(screen, pygame.K_s)
    assert screen._panel.mode == "name"
    getattr(screen, transition)()
    stop.assert_called()


@pytest.mark.parametrize("choice,tempo,loop", [("accept_tempo", .45, (0, 8000)),
    ("keep_tempo", .5, (0, 8000)), ("close", .5, (0, 8000)),
    ("smaller_passage", .5, (0, 4000))])
def test_difficult_repeats_offer_slow_or_smaller_without_automatic_changes(clock_screen, choice, tempo, loop):
    clock, screen, _ = clock_screen(tempo=.5)
    attach(screen)
    screen._loop_start_ms, screen._loop_end_ms, screen._loop_enabled = 0, 8000, True
    screen._count_in_ms = 0
    screen.toggle_play()
    for index in range(3):
        screen._begin_attempt(0)
        screen._playback_ms = screen._loop_finish_ms()
        screen._last_tick = clock.now
        screen.update()
        if index < 2:
            assert screen._panel is None
    assert screen._panel.kind == "loop_progress" and not screen.is_playing()
    assert screen._tempo_factor == .5 and screen._loop_end_ms == 8000
    assert {a.name for a in screen._panel.actions} >= {"accept_tempo", "smaller_passage", "keep_tempo"}
    screen._panel_action(choice)
    assert screen._tempo_factor == tempo
    assert (screen._loop_start_ms, screen._loop_end_ms) == loop
    assert screen.is_playing() and screen._playback_ms < screen._count_in_target_ms


@pytest.mark.parametrize("look", ["studio", "stage", "hifi"])
def test_setup_help_wizard_and_passages_are_visible_at_smallest_size(clock_screen, look):
    _, screen, _ = clock_screen()
    attach(screen)
    set_look(look)
    surface = pygame.Surface((320, 240))
    for opener in (screen._open_setup, screen._open_help):
        opener()
        screen._panel.render(surface)
        panel = screen._panel
        assert "passages" in panel.hit and "wizard" in panel.hit
        assert all(surface.get_rect().contains(rect) and not rect.colliderect(panel.body)
                   for name, rect in panel.hit.items() if name in {a.name for a in panel.actions})
        assert panel.body.height >= 20
        assert key(screen, pygame.K_F2) == "wizard"


def test_gate_change_requires_three_new_comparable_attempts(clock_screen):
    clock, screen, _ = clock_screen(tempo=.5)
    attach(screen)
    screen._loop_start_ms, screen._loop_end_ms, screen._loop_enabled = 0, 8000, True
    screen._count_in_ms = 0
    screen.toggle_play()
    screen._audio_capture.set_noise_gate_db = Mock()
    for index in range(5):
        if index == 2:
            screen.set_noise_gate_db(-35)
        screen._begin_attempt(0)
        screen._playback_ms = screen._loop_finish_ms()
        screen._last_tick = clock.now
        screen.update()
        assert (screen._panel is not None) is (index == 4)


def test_mid_attempt_gate_changes_are_latched_even_when_reverted(clock_screen):
    _, screen, _ = clock_screen(tempo=.5)
    attach(screen)
    screen._loop_start_ms, screen._loop_end_ms, screen._loop_enabled = 0, 8000, True
    screen._count_in_ms = 0
    screen.toggle_play()
    screen._audio_capture.set_noise_gate_db = Mock()
    screen._begin_attempt(0)
    initial = screen._noise_gate_db
    screen.set_noise_gate_db(-35)
    screen.set_noise_gate_db(initial)
    screen._record_section_attempt(8000)
    entry = screen._progress_tracker.get_best(identity()).section_attempts[-1]
    assert not entry["uniform_input"]


def test_save_and_delete_errors_remain_in_the_editing_panel(monkeypatch):
    from pickhero import progress as storage
    tracker = ProgressTracker()
    tracker.save_passage(identity(), "Refrain", state())
    panel = PassagePanel(tracker, identity(), state(), "A song")
    monkeypatch.setattr(storage, "MAX_PROGRESS_BYTES", 1)
    key(panel, pygame.K_s)
    type_text(panel, "Solo")
    key(panel, pygame.K_RETURN)
    assert panel.mode == "name" and panel.error == "capacity"
    assert len(tracker.saved_passages(identity())) == 1
    key(panel, pygame.K_ESCAPE)
    key(panel, pygame.K_RETURN)
    key(panel, pygame.K_DELETE)
    key(panel, pygame.K_DELETE)
    assert panel.mode == "delete" and panel.error == "capacity"
    assert len(tracker.saved_passages(identity())) == 1


def test_save_error_banner_is_separate_from_audio_reliability(clock_screen, monkeypatch):
    from pickhero import progress as storage
    _, screen, _ = clock_screen()
    attach(screen)
    monkeypatch.setattr(storage, "MAX_PROGRESS_BYTES", 1)
    screen.save_practice_state()
    assert screen._progress_tracker.last_save_error == "capacity"
    assert screen._current_warning() == i18n.t("passage.progress_capacity")
    assert screen._attempt_issues == []
    assert i18n.t("passage.progress_capacity") in str(screen._result_panel().sections)
