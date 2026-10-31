"""The saved practice is visible, actionable and readable in every library."""
import os
from pathlib import Path

import pygame
import pytest

from pickhero import library
from pickhero.config import Config
from pickhero.i18n import current_language, set_language, t
from pickhero.progress import ProgressTracker, song_key
from pickhero.ui import secondary
from pickhero.ui.colors import get_look, set_look
from pickhero.ui.menu import MenuScreen


@pytest.fixture
def browser(tmp_path, monkeypatch):
    pygame.font.init()
    previous_look, previous_language = get_look().key, current_language()
    songs = [library.SongInfo(str(tmp_path / f"practice-{i:02}.gp5"),
                             title=f"{i:02} Bünde rückwärts", strings=4,
                             difficulty=1, bars=8, bpm=66) for i in range(1, 12)]
    for song in songs:
        song.file.touch()
    monkeypatch.setattr(library, "scan", lambda *_a, **_k: songs)
    tracker = ProgressTracker()
    menu = MenuScreen(tmp_path, Config(instrument="bass_guitar"), tracker)

    def save():
        tracker.save_session(song_key(songs[6].file, "bass_guitar", 0), {
            "song_path": str(songs[6].file), "instrument": "bass_guitar", "track_index": 0,
            "position_ms": 12500, "measure": 3, "tempo_factor": .75, "wait_mode": False,
            "loop_enabled": False, "active_strings": [True] * 4,
        })

    yield menu, save
    set_look(previous_look)
    set_language(previous_language)


@pytest.mark.parametrize("look", ["studio", "stage", "hifi"])
@pytest.mark.parametrize("language", ["de", "en"])
@pytest.mark.parametrize("size", [(320, 240), (640, 480), (900, 520), (1280, 720)])
@pytest.mark.parametrize("saved", [False, True])
@pytest.mark.parametrize("scanning", [False, True])
def test_primary_action_and_footer_stay_readable(browser, monkeypatch, look, language, size, saved, scanning):
    menu, save = browser
    set_look(look)
    set_language(language)
    if saved:
        save()
    if scanning:
        menu._scan_status = t("import.progress", current=21, total=100)
    drawn = []
    original_button = secondary.SetupView.button

    def button(self, surface, rect, label, **kwargs):
        drawn.append((label, pygame.Rect(rect), kwargs.get("primary", False)))
        return original_button(self, surface, rect, label, **kwargs)

    monkeypatch.setattr(secondary.SetupView, "button", button)
    surface = pygame.Surface(size)
    menu.render(surface)
    primary = [(label, rect) for label, rect, chosen in drawn if chosen]
    if saved:
        assert any(rect == menu._button_rects["resume"][0] for _, rect in primary)
        assert any(menu._songs[6].display_title in label for label, _ in primary)
    elif size[1] >= 380:
        assert any(rect == menu._button_rects["play" if size[0] >= 940 else "details"][0]
                   for _, rect in primary)
    names = [name for name in ("lessons", "wizard", "rescan", "resume", "resume_details")
             if name in menu._button_rects]
    rects = [menu._button_rects[name][0] for name in names]
    for rect in rects:
        assert surface.get_rect().contains(rect)
        assert rect.height >= 26
        assert not any(rect.colliderect(other) for other in rects if other is not rect)
    assert menu._row_rects, "Song navigation must remain visible even with status and resume context"
    assert max(rect.bottom for rect, _ in menu._row_rects) <= min(rect.top for rect in rects)
    if saved:
        assert menu._button_rects["resume"][0].left < menu._button_rects["rescan"][0].left
        assert menu.handle_event(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_r)) == "resume"
        assert menu.handle_event(pygame.event.Event(pygame.MOUSEBUTTONDOWN, button=1,
               pos=menu._button_rects["resume"][0].center)) == "resume"
        if "resume_details" in menu._button_rects:
            menu.handle_event(pygame.event.Event(pygame.MOUSEBUTTONDOWN, button=1,
                              pos=menu._button_rects["resume_details"][0].center))
        else:
            menu.handle_event(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_r, mod=pygame.KMOD_SHIFT))
        assert menu._resume_panel is not None
        assert menu._resume_panel.subtitle == menu._songs[6].display_title
    else:
        assert "resume" not in menu._button_rects
        assert menu.handle_event(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_r)) is None
    preview = os.environ.get("PICKHERO_RESUME_PREVIEW_DIR")
    if preview and saved and not scanning and language == "de":
        folder = Path(preview)
        folder.mkdir(parents=True, exist_ok=True)
        pygame.image.save(surface, folder / f"{look}-{size[0]}x{size[1]}.png")


def test_saved_point_appears_after_progress_change_and_is_specific_to_instrument(browser):
    menu, save = browser
    surface = pygame.Surface((1280, 720))
    menu.render(surface)
    assert "resume" not in menu._button_rects
    save()
    menu.render(surface)
    assert "resume" in menu._button_rects
    menu._config.instrument = "guitar"
    menu.render(surface)
    assert "resume" not in menu._button_rects
    assert menu.handle_event(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_r)) is None


def test_resume_context_uses_readable_text_and_real_details_button(browser, monkeypatch):
    from pickhero.ui import library_browser
    menu, save = browser
    save()
    original_text = library_browser.text
    drawn = []

    def text(surface, value, rect, **kwargs):
        drawn.append((str(value), pygame.Rect(rect), kwargs.get("size", 17)))
        return original_text(surface, value, rect, **kwargs)

    monkeypatch.setattr(library_browser, "text", text)
    surface = pygame.Surface((1280, 720))
    menu.render(surface)
    context = [(value, rect, size) for value, rect, size in drawn
               if menu._songs[6].display_title in value and "75" in value]
    assert len(context) == 1 and context[0][2] >= 14
    details = menu._button_rects["resume_details"][0]
    assert not context[0][1].colliderect(details)
    assert details.width >= 170
    menu.handle_event(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_r, mod=pygame.KMOD_SHIFT))
    assert menu._resume_panel is not None
    assert menu.handle_event(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_RETURN)) == "resume"
