"""Track selection occupies its content size without losing navigation or state."""
from types import SimpleNamespace
from unittest.mock import Mock

import pygame
import pytest

from pickhero.config import Config
from pickhero.i18n import t
from pickhero.progress import song_key
from pickhero.ui import import_menu, secondary
from pickhero.ui.app import App
from pickhero.ui.colors import set_look
from pickhero.ui.import_menu import ImportScreen


@pytest.fixture(autouse=True)
def graphics():
    pygame.font.init()
    yield
    set_look("studio")


def tracks(count=1):
    return [{"index": index, "name": f"Exercise track {index + 1}",
             "strings": 4, "compatible": True} for index in range(count)]


def saved_state(*, completed=False):
    return {"track_index": 0, "position_ms": 30000, "measure": 0,
            "tempo_factor": .75, "wait_mode": True, "completed": completed,
            "loop_enabled": False, "active_strings": [True, True, True, True],
            "max_fret": 24}


def key(value):
    return pygame.event.Event(pygame.KEYDOWN, key=value)


def click(rect):
    return pygame.event.Event(pygame.MOUSEBUTTONDOWN, button=1, pos=rect.center)


def assert_contained_controls(screen, surface):
    bounds = screen.view.bounds
    viewport = screen.view.viewport
    assert surface.get_rect().contains(bounds)
    assert bounds.contains(viewport)
    assert viewport.width > 0 and viewport.height > 0
    for rect in screen.view.buttons.values():
        assert rect.width > 0 and rect.height > 0
        assert bounds.contains(rect)
        assert not viewport.colliderect(rect)
    visible = [rect for rect, _ in screen.rows if rect.width and rect.height]
    assert all(viewport.contains(rect) for rect in visible)
    buttons = list(screen.view.buttons.values())
    assert all(not left.colliderect(right)
               for index, left in enumerate(buttons) for right in buttons[index + 1:])


@pytest.mark.parametrize("look", ["studio", "stage", "hifi"])
def test_single_track_is_a_centered_content_sized_dialog(look):
    set_look(look)
    surface = pygame.Surface((1280, 720))
    screen = ImportScreen("02 all_open.gp5", tracks=tracks())
    screen.render(surface)

    bounds = screen.view.bounds
    assert bounds.width <= 720
    assert bounds.height < 480
    assert abs(bounds.centerx - surface.get_rect().centerx) <= 1
    assert abs(bounds.centery - surface.get_rect().centery) <= 1
    assert_contained_controls(screen, surface)
    assert set(screen.view.buttons) == {"cancel", "start"}
    assert screen.handle_event(key(pygame.K_RETURN)) == "start"
    assert screen.track_index == 0


@pytest.mark.parametrize("look", ["studio", "stage", "hifi"])
def test_dialog_overlay_preserves_background_context(look):
    set_look(look)
    screen = ImportScreen("song.gp5", tracks=tracks())
    surface = pygame.Surface((1280, 720))
    surface.fill((240, 240, 240))
    pygame.draw.rect(surface, (10, 30, 60), (0, 0, 80, surface.get_height()))
    screen.render(surface, background=True)

    assert not screen.view.bounds.collidepoint((10, 10))
    assert not screen.view.bounds.collidepoint((100, 10))
    assert surface.get_at((10, 10)) != surface.get_at((100, 10)), (
        "The dialog must retain visible library context beneath its dimming layer")
    assert_contained_controls(screen, surface)


@pytest.mark.parametrize("look", ["studio", "stage", "hifi"])
@pytest.mark.parametrize("size", [(320, 240), (640, 480), (1280, 720)])
def test_saved_session_controls_and_hitboxes_use_window_coordinates(look, size):
    set_look(look)
    surface = pygame.Surface(size)
    screen = ImportScreen("07 frets_back.gp5", tracks=tracks(), resume_available=True)
    screen.resume_state = saved_state(completed=True)
    screen.render(surface)

    assert_contained_controls(screen, surface)
    assert set(screen.view.buttons) == {"cancel", "start", "resume"}
    for name in ("start", "resume", "cancel"):
        assert screen.handle_event(click(screen.view.buttons[name])) == name
    assert screen.handle_event(key(pygame.K_ESCAPE)) == "cancel"


@pytest.mark.parametrize("look", ["studio", "stage", "hifi"])
@pytest.mark.parametrize("size", [(320, 240), (1280, 720)])
def test_long_track_list_remains_scrollable_and_selectable(look, size):
    set_look(look)
    rows = tracks(24)
    for index, row in enumerate(rows):
        row["compatible"] = index % 2 == 0
    screen = ImportScreen("song.gp5", tracks=rows)
    surface = pygame.Surface(size)
    screen.render(surface)

    assert screen.handle_event(key(pygame.K_END)) == "selection"
    assert screen.track_index == 22
    screen.render(surface)
    assert screen.view.scroll > 0
    assert_contained_controls(screen, surface)
    selected = next(rect for rect, index in screen.rows if index == 22)
    assert selected.width > 0 and selected.height > 0
    assert screen.handle_event(click(selected)) == "selection"
    assert screen.track_index == 22

    screen.handle_event(key(pygame.K_UP))
    assert screen.track_index == 20
    screen.handle_event(key(pygame.K_HOME))
    screen.render(surface)
    assert screen.track_index == 0
    before = screen.view.scroll
    screen.handle_event(pygame.event.Event(pygame.MOUSEWHEEL, y=-2))
    screen.render(surface)
    assert screen.view.scroll > before
    assert screen.track_index == 0, "Scrolling must not silently change the chosen part"


@pytest.mark.parametrize("look", ["studio", "stage", "hifi"])
def test_mouse_selection_uses_global_rows_and_rejects_incompatible_parts(look):
    set_look(look)
    rows = tracks(3)
    rows[1]["compatible"] = False
    screen = ImportScreen("song.gp5", tracks=rows)
    surface = pygame.Surface((1280, 720))
    screen.render(surface)
    by_index = {index: rect for rect, index in screen.rows}

    assert screen.handle_event(click(by_index[2])) == "selection"
    assert screen.track_index == 2
    assert screen.handle_event(click(by_index[1])) != "selection"
    assert screen.track_index == 2


@pytest.mark.parametrize("requested", [0, 999])
def test_invalid_initial_selection_falls_back_to_first_compatible_track(requested):
    rows = tracks(3)
    rows[0]["compatible"] = False
    screen = ImportScreen("song.gp5", tracks=rows, selected=requested)
    assert screen.track_index == 1


@pytest.mark.parametrize("kind", ["loading", "empty", "incompatible"])
def test_without_playable_track_only_cancel_can_be_activated(kind):
    rows = None if kind == "loading" else [] if kind == "empty" else tracks()
    if kind == "incompatible":
        rows[0]["compatible"] = False
    screen = ImportScreen("song.gp5", tracks=rows)
    surface = pygame.Surface((320, 240))
    screen.render(surface)

    assert_contained_controls(screen, surface)
    assert set(screen.view.buttons) == {"cancel"}
    assert screen.handle_event(key(pygame.K_RETURN)) == "cancel"
    assert screen.track_index is None


@pytest.mark.parametrize("look", ["studio", "stage", "hifi"])
def test_completed_session_omits_misleading_end_position(monkeypatch, look):
    set_look(look)
    labels = []
    original = secondary.text

    def record(surface, value, rect, **kwargs):
        labels.append(str(value))
        return original(surface, value, rect, **kwargs)

    monkeypatch.setattr(secondary, "text", record)
    monkeypatch.setattr(import_menu, "text", record)
    screen = ImportScreen("07 frets_back.gp5", tracks=tracks(), resume_available=True)
    screen.resume_state = saved_state(completed=True)
    screen.render(pygame.Surface((1280, 720)))

    drawn = " ".join(" ".join(labels).split())
    false_position = t("library.resume_position", bar=1, position="0:30")
    assert false_position not in drawn
    assert " ".join(t("import.completed").split()) in drawn
    assert " ".join(t("import.repeat_settings").split()) in drawn
    assert screen.handle_event(click(screen.view.buttons["start"])) == "start"
    assert screen.handle_event(click(screen.view.buttons["resume"])) == "resume"


@pytest.mark.parametrize("action", ["start", "resume"])
def test_start_and_resume_pass_different_saved_state_to_selected_track(monkeypatch, tmp_path, action):
    """The visual redesign must not resume another track or erase saved options."""
    path = tmp_path / "song.gp5"
    content_id = "c" * 64
    config = Config(instrument="bass_ukulele")
    selected = 2
    saved = saved_state(completed=True)
    saved["track_index"] = selected
    expected_key = song_key(path, config.instrument, selected, content_id=content_id)
    saved["song_key"] = expected_key
    progress = Mock()
    progress.get_session.return_value = saved
    screen = ImportScreen(path, tracks=tracks(3), selected=selected, resume_available=True)
    screen.resume_state = saved
    screen.render(pygame.Surface((1280, 720)))

    pending = []

    def fake_job(operation, score_path, instrument, **kwargs):
        job = SimpleNamespace(operation=operation, path=score_path,
                              instrument=instrument, options=kwargs)
        pending.append(job)
        return job

    monkeypatch.setattr("pickhero.ui.app.ImportJob", fake_job)
    app = App.__new__(App)
    app._config = config
    app._progress = progress
    app._import_content_id = content_id
    app._import_screen = screen
    app._import_resume = {"old": "unrelated session"}
    app._handle_import_event(click(screen.view.buttons[action]))

    assert len(pending) == 1
    assert pending[0].operation == "load"
    assert pending[0].options == {"track_index": selected, "content_id": content_id}
    assert app._import_screen.tracks is None
    if action == "resume":
        progress.get_session.assert_called_once_with(expected_key)
        assert app._import_resume is saved
        assert app._import_resume["tempo_factor"] == .75
    else:
        progress.get_session.assert_not_called()
        assert app._import_resume is None
