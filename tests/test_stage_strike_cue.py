"""Stage's musical target stays visible without drawing through string names."""

import numpy as np
import pygame
import pytest

from pickhero.ui import design, paint_stage
from pickhero.ui.colors import get_look, set_look
from pickhero.ui.page import Page


@pytest.fixture(autouse=True)
def stage_drawing():
    pygame.display.init()
    pygame.font.init()
    pygame.display.set_mode((1, 1))
    previous = get_look().key
    set_look("stage")
    yield
    set_look(previous)
    design.clear_caches()
    pygame.display.quit()


@pytest.mark.parametrize("size", [(1280, 720), (640, 480), (320, 240)])
@pytest.mark.parametrize("lanes", [4, 6])
@pytest.mark.parametrize("lit", [False, True])
@pytest.mark.parametrize("name", ["E1", "F#2"])
def test_strike_line_is_visible_at_its_real_height_on_both_sides_of_the_name(
        monkeypatch, size, lanes, lit, name):
    page = Page(*size)
    surface = pygame.Surface(size)
    surface.fill(get_look().stage_bg)
    labels = []
    original = design.text

    def record(*args, **kwargs):
        rect = original(*args, **kwargs)
        labels.append(rect)
        return rect

    monkeypatch.setattr(design, "text", record)
    rect = paint_stage.strike_ring(surface, page, 0, lanes, lit=lit, name=name)
    y = paint_stage.strike_y(page)
    white = get_look().hit_zone
    # Check actual pixels rather than just geometry. At 320 px, a sharp name
    # can occupy an entire six-string pad; keeping it readable takes priority
    # there. The white line continues in the spaces between those small pads.
    protected_left = min(label.left for label in labels) - 2
    protected_right = max(label.right for label in labels) + 2
    if rect.left < protected_left:
        assert surface.get_at((rect.left, y))[:3] == white
    if rect.right - 1 >= protected_right:
        assert surface.get_at((rect.right - 1, y))[:3] == white

    without_guides = pygame.Surface(size)
    without_guides.fill(get_look().stage_bg)
    monkeypatch.setattr(paint_stage, "_strike_guides", lambda *args: None)
    paint_stage.strike_ring(without_guides, page, 0, lanes, lit=lit, name=name)
    with_pixels = pygame.surfarray.array3d(surface)
    without_pixels = pygame.surfarray.array3d(without_guides)
    for label in labels:
        assert np.array_equal(
            with_pixels[label.left:label.right, label.top:label.bottom],
            without_pixels[label.left:label.right, label.top:label.bottom],
        ), "The timing mark overwrote the string name or its octave"

    # All changes remain on the true timing row. In particular the upper
    # oval edge, reached hundreds of milliseconds earlier, gets no white cue.
    changed_x, changed_y = np.where(np.any(with_pixels != without_pixels, axis=2))
    if size[0] > 320 or name == "E1":
        assert len(changed_x)
    half_band = max(2, round(page.height * 0.0049))
    assert np.all(np.abs(changed_y - y) <= half_band)
    if len(changed_x):
        assert rect.left <= changed_x.min() <= changed_x.max() < rect.right


@pytest.mark.parametrize("size", [(1280, 720), (640, 480), (320, 240)])
@pytest.mark.parametrize("lit", [False, True])
def test_unnamed_pad_keeps_an_unbroken_line_through_the_actual_onset(size, lit):
    page = Page(*size)
    surface = pygame.Surface(size)
    surface.fill(get_look().stage_bg)
    rect = paint_stage.strike_ring(surface, page, 1, 4, lit=lit)
    y = paint_stage.strike_y(page)
    assert all(surface.get_at((x, y))[:3] == get_look().hit_zone
               for x in range(rect.left, rect.right))
    assert paint_stage.note_box(page, 1, 4, 0).centery == y
    assert paint_stage.project(page, 1, 4, 0)[1] == y


@pytest.mark.parametrize("size", [(1280, 720), (640, 480), (320, 240)])
def test_relighting_a_pad_preserves_the_white_target(size):
    page = Page(*size)
    surface = pygame.Surface(size)
    surface.fill(get_look().stage_bg)
    rect = paint_stage.strike_ring(surface, page, 1, 4, name="A1")
    points = [(rect.left, paint_stage.strike_y(page)),
              (rect.right - 1, paint_stage.strike_y(page))]
    before = [surface.get_at(point) for point in points]
    paint_stage.strike_ring(surface, page, 1, 4, name="A1", lit=True)
    assert [surface.get_at(point) for point in points] == before
