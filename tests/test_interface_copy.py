"""Visible instructions and real painter controls must agree."""

from itertools import combinations

import pygame
import pytest

from pickhero import i18n
from pickhero.lang import de, en
from pickhero.ui import design
from pickhero.ui.colors import get_look, set_look
from pickhero.ui.page import Page, painter


@pytest.fixture(autouse=True)
def rendering():
    old_look = get_look().key
    old_language = i18n.current_language()
    pygame.font.init()
    yield
    set_look(old_look)
    i18n.set_language(old_language)


def example_page(size=(1280, 720)):
    return Page(
        *size, title="05 Leere Saiten rauf und runter", subtitle="Bassgitarre · E A D G",
        bpm=90, bars=16, bar=3, elapsed_ms=4000, total_ms=40000,
        found=7, expected=10, in_time=5, accuracy=70, signal=0.4,
        strings=4, string_labels=[(1, "G", "G2"), (2, "D", "D2"),
                                   (3, "A", "A1"), (4, "E", "E1")],
        toggles=[("play.listen", "key.a", True, "listen"),
                 ("play.wait", "key.w", False, "wait"),
                 ("play.loop", "key.p", True, "loop"),
                 ("play.metronome", "key.m", False, "metronome")],
    )


def test_no_editorial_notes_are_exposed_in_german():
    markers = ("Rendered clause", "Written fresh", "English title", "the shipped",
               "nominalises", "The parenthesis", "Same English", "came over from")
    for key, value in de.STRINGS.items():
        values = value.values() if isinstance(value, dict) else [value]
        for text in values:
            assert not any(marker in text for marker in markers), key


def test_help_distinguishes_stage_motion_and_separate_pitch_timing():
    assert "von oben" in de.STRINGS["practice.help.reading_stage"]
    assert "rechts nach links" in de.STRINGS["practice.help.reading_studio"]
    assert "rechts nach links" in de.STRINGS["practice.help.reading_hifi"]
    assert "getrennt" in de.STRINGS["play.help_hit"]
    assert "separately" in en.STRINGS["play.help_hit"]
    assert "P:" in de.STRINGS["practice.help.loop_keys"]
    assert "Rücktaste" in de.STRINGS["practice.help.loop_keys"]
    assert "M:" in de.STRINGS["play.help_keys_3"]
    assert "Ansicht" in de.STRINGS["play.help_keys_3"]


@pytest.mark.parametrize("look", ["studio", "stage", "hifi"])
@pytest.mark.parametrize("language", ["de", "en"])
@pytest.mark.parametrize("size", [(640, 360), (900, 600), (1280, 720)])
def test_playing_setup_and_help_are_visible_clickable_and_do_not_overlap(
        look, language, size, monkeypatch):
    set_look(look)
    i18n.set_language(language)
    page = example_page(size)
    surface = pygame.Surface(size)
    drawn = []
    original = design.text

    def record(target, value, pos, *args, **kwargs):
        rect = original(target, value, pos, *args, **kwargs)
        if target is surface:
            drawn.append((value, rect.copy()))
        return rect

    monkeypatch.setattr(design, "text", record)
    painter(look).play(surface, page)
    for action in ("setup", "help"):
        assert page.hits(action), (look, action)
        for rect in page.hits(action):
            assert surface.get_rect().contains(rect)
            assert page.at(rect.center) == action
    controls = [(action, rect) for action, rects in page.hit.items()
                if action not in ("board", "banner") for rect in rects]
    for (action, a), (other, b) in combinations(controls, 2):
        if action != other:
            assert not a.colliderect(b), (look, size, action, other, a, b)
    toolbar = page.hits("setup") + page.hits("help")[:1]
    labels = [f"{i18n.t('key.s')}  {i18n.t('practice.setup')}",
              f"{i18n.t('key.h')}  {i18n.t('play.help')}"]
    for value, rect in drawn:
        if value in labels:
            continue
        for button in toolbar:
            assert not button.colliderect(rect), (look, size, value, button, rect)


def test_stage_tempo_has_separate_click_targets():
    set_look("stage")
    page = example_page()
    painter("stage").play(pygame.Surface((page.width, page.height)), page)
    for action in ("tempo_down", "tempo_up"):
        assert page.hits(action)
        assert page.at(page.hits(action)[0].center) == action


@pytest.mark.parametrize("look", ["studio", "stage", "hifi"])
@pytest.mark.parametrize("size", [(640, 360), (1280, 720)])
def test_loop_marks_expose_an_explicit_clear_button(look, size):
    set_look(look)
    page = example_page(size)
    page.loop_status = i18n.t("play.loop_on", start="0:08", end="0:16")
    surface = pygame.Surface(size)
    painter(look).play(surface, page)
    assert page.hits("loop_clear")
    for rect in page.hits("loop_clear"):
        assert surface.get_rect().contains(rect)
        assert page.at(rect.center) == "loop_clear"
        for action, others in page.hit.items():
            if action not in ("loop_clear", "board", "banner"):
                assert not any(rect.colliderect(other) for other in others), action
        before = pygame.image.tostring(surface.subsurface(rect), "RGB")
        page.count_in_remaining = 4
        page.count_in_progress = 0.5
        painter(look).count_in(surface, page)
        assert pygame.image.tostring(surface.subsurface(rect), "RGB") == before


def test_hifi_dial_moves_across_the_entire_supported_tempo_range():
    from pickhero.ui.scrolling import TEMPO_MIN
    from pickhero.ui.paint_hifi import _dial
    page = example_page()
    page.tempo_min_percent = int(round(TEMPO_MIN * 100))
    positions = []
    for percent in (25, 30, 40, 50, 75, 100):
        page.tempo_percent = percent
        positions.append(_dial(page))
    assert positions[0] == 0
    assert positions[-1] == 1
    assert all(a < b for a, b in zip(positions, positions[1:]))


@pytest.mark.parametrize("look", ["studio", "stage", "hifi"])
def test_count_in_adds_a_visible_overlay_and_disappears_when_finished(look):
    set_look(look)
    page = example_page()
    surface = pygame.Surface((page.width, page.height))
    module = painter(look)
    module.play(surface, page)
    before = pygame.image.tostring(surface, "RGB")
    page.count_in_remaining = 3
    page.count_in_progress = 0.5
    module.count_in(surface, page)
    assert pygame.image.tostring(surface, "RGB") != before
    module.play(surface, page)
    page.count_in_remaining = 0
    module.count_in(surface, page)
    assert pygame.image.tostring(surface, "RGB") == before
