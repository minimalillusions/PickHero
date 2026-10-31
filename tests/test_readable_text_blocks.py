"""Prose remains readable and complete in large and small themed windows."""
from types import SimpleNamespace
from unittest.mock import Mock
import re

import pygame
import pytest

from pickhero import i18n
from pickhero.config import Config
from pickhero.ui import calibration_menu, check_menu, design, device_menu, import_menu, secondary
from pickhero.ui.colors import set_look
from pickhero.ui.practice_panels import Action, PracticePanel
from pickhero.ui.text_layout import wrap


WORDS = [f"W{index:03d}" for index in range(160)]
LONG_PROSE = " ".join(WORDS)


@pytest.fixture(autouse=True)
def display_and_audio(monkeypatch):
    pygame.font.init()
    old_language = i18n.current_language()
    i18n.set_language("de")
    set_look("studio")
    capture = SimpleNamespace(start=Mock(), stop=Mock(),
                              detector=SimpleNamespace(noise_gate_db=-45),
                              get_signal_db=lambda: -60, get_tuner_data=lambda: (0, 0))
    monkeypatch.setattr(check_menu, "AudioCapture", lambda *args, **kwargs: capture)
    monkeypatch.setattr(calibration_menu, "AudioCapture", lambda *args, **kwargs: capture)
    monkeypatch.setattr(device_menu, "list_audio_devices", lambda: [])
    monkeypatch.setattr(check_menu, "list_audio_devices", lambda: [])
    monkeypatch.setattr("pickhero.audio.input.best_input_device", lambda: None)
    yield
    i18n.set_language(old_language)
    set_look("studio")


@pytest.mark.parametrize("width", [28, 110, 420, 2400])
def test_wrapping_obeys_both_limits_without_losing_long_words_or_paragraphs(width):
    face = design.reading_font(17)
    prose = LONG_PROSE + "\n\n" + "x" * 181 + " Ende."
    lines = wrap(prose, face, width)
    assert all(len(line) <= 60 for line in lines)
    assert all(face.size(line)[0] <= width for line in lines)
    assert "" in lines
    assert "".join("".join(lines).split()) == "".join(prose.split())


def test_paragraph_does_not_truncate_after_three_lines_or_two_thousand_characters(monkeypatch):
    drawn = []
    monkeypatch.setattr(secondary, "text", lambda surface, value, rect, **kw: drawn.append(value))
    value = LONG_PROSE * 4
    surface = pygame.Surface((900, 400))
    height = secondary.paragraph(surface, value, (20, 10, 850, 40))
    assert len(drawn) > 3
    assert all(len(line) <= 60 for line in drawn)
    assert "".join("".join(drawn).split()) == "".join(value.split())
    assert height == secondary.paragraph_height(value, 850)


class RecordingFont:
    def __init__(self, original, lines):
        self.original = original
        self.lines = lines

    def __getattr__(self, name):
        return getattr(self.original, name)

    def render(self, value, *args, **kwargs):
        if re.search(r"W\d{3}", value):
            self.lines.append(value)
        return self.original.render(value, *args, **kwargs)


@pytest.mark.parametrize("look", ["studio", "stage", "hifi"])
@pytest.mark.parametrize("size", [(320, 240), (1280, 720)])
def test_modal_prose_is_complete_after_scrolling_in_every_design(monkeypatch, look, size):
    set_look(look)
    drawn = []
    original = design.reading_font
    monkeypatch.setattr(design, "reading_font", lambda *args, **kw: RecordingFont(original(*args, **kw), drawn))
    panel = PracticePanel("diagnostics", "Timing", "", [("", [LONG_PROSE])],
                          [Action("close", "Schließen")])
    surface = pygame.Surface(size)
    panel.render(surface)
    step = max(1, panel.body.height // 2)
    for scroll in range(0, panel.max_scroll + step, step):
        panel.scroll = scroll
        panel.render(surface)
    assert all(len(line) <= 60 for line in drawn)
    assert set(re.findall(r"W\d{3}", " ".join(drawn))) == set(WORDS)
    assert panel.body.bottom <= panel.hit["close"].top


def record_visible_prose(monkeypatch):
    drawn = []
    original = secondary.text

    def record(surface, value, rect, **kwargs):
        if re.search(r"W\d{3}", value) and surface.get_clip().contains(pygame.Rect(rect)):
            drawn.append(value)
        return original(surface, value, rect, **kwargs)

    monkeypatch.setattr(secondary, "text", record)
    return drawn


@pytest.mark.parametrize("look", ["studio", "stage", "hifi"])
@pytest.mark.parametrize("size", [(320, 240), (1280, 720)])
@pytest.mark.parametrize("kind", ["device", "check", "tuner", "import"])
def test_long_error_or_instruction_remains_accessible_without_overflow(monkeypatch, look, size, kind):
    set_look(look)
    drawn = record_visible_prose(monkeypatch)
    config = Config(instrument="bass_ukulele")
    if kind == "device":
        screen = device_menu.DeviceMenuScreen(config)
        screen._error = LONG_PROSE
        screen._reveal_selection = False
    elif kind == "check":
        screen = check_menu.CheckScreen(config)
        screen._error = LONG_PROSE
    elif kind == "tuner":
        screen = calibration_menu.CalibrationMenuScreen(config)
        screen._error = LONG_PROSE
    else:
        original = import_menu.t
        monkeypatch.setattr(import_menu, "t", lambda key, **kw: LONG_PROSE if key == "import.wait" else original(key, **kw))
        screen = import_menu.ImportScreen("score.gp5")
    surface = pygame.Surface(size)
    screen.render(surface)
    view = screen.view if kind == "import" else screen._ui
    step = max(1, view.viewport.height // 2)
    for scroll in range(0, max(0, view.content_height - view.viewport.height) + step, step):
        view.scroll = scroll
        screen.render(surface)
    assert all(len(line) <= 60 for line in drawn)
    assert set(re.findall(r"W\d{3}", " ".join(drawn))) == set(WORDS)
    assert all(not view.viewport.colliderect(rect) for rect in view.buttons.values())
    getattr(screen, "close", lambda: None)()


@pytest.mark.parametrize("look", ["studio", "stage", "hifi"])
@pytest.mark.parametrize("size", [(320, 240), (1280, 720)])
def test_long_subtitle_moves_to_readable_scroll_area(monkeypatch, look, size):
    set_look(look)
    drawn = record_visible_prose(monkeypatch)
    view = secondary.SetupView()
    surface = pygame.Surface(size)

    def render():
        view.begin(surface, "Mikrofon", LONG_PROSE, [("back", "Zurück")])
        view.content(40)
        view.finish(surface)

    render()
    assert view.intro_height > 0
    step = max(1, view.viewport.height // 2)
    for scroll in range(0, max(0, view.content_height - view.viewport.height) + step, step):
        view.scroll = scroll
        render()
    assert all(len(line) <= 60 for line in drawn)
    assert set(re.findall(r"W\d{3}", " ".join(drawn))) == set(WORDS)
