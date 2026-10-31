"""Visual regressions: smooth coverage must never introduce a dark fringe."""
from __future__ import annotations

import os

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

import numpy as np
import pygame
import pytest

from pickhero.ui import design, shapes
from pickhero.ui.colors import get_look, set_look


@pytest.fixture(autouse=True)
def _renderer():
    pygame.font.init()
    previous = get_look().key
    shapes.clear_aa_cache()
    yield
    set_look(previous)
    shapes.clear_aa_cache()


def _draw(surface, kind, colour, offset=(0, 0)):
    rect = pygame.Rect(12 + offset[0], 12 + offset[1], 85, 43)
    if kind == "skew":
        shapes.skewed(surface, rect, colour)
    elif kind == "rounded":
        shapes.rounded_rect(surface, colour, rect, border_radius=14)
    elif kind == "ellipse":
        shapes.ellipse(surface, colour, rect)
    elif kind == "outline":
        shapes.ellipse(surface, colour, rect, width=3)
    elif kind == "star":
        shapes.stars(surface, rect.center, 1, 1, size=18, on=colour)


@pytest.mark.parametrize("kind", ["skew", "rounded", "ellipse", "outline", "star"])
def test_edges_have_partial_coverage_and_unmodified_colour(kind):
    colour = (26, 192, 236)
    surface = pygame.Surface((140, 90), pygame.SRCALPHA)
    _draw(surface, kind, colour)
    alpha = pygame.surfarray.array_alpha(surface)
    rgb = pygame.surfarray.array3d(surface)
    edge = (alpha > 0) & (alpha < 255)
    assert np.count_nonzero(edge) >= 15
    assert np.all(rgb[edge] == colour), "Black RGB at a translucent edge causes a dark fringe."
    assert not alpha[0].any()
    assert not alpha[:, 0].any()
    if kind == "outline":
        assert alpha[54, 33] == 0, "The ring must keep its transparent centre."


@pytest.mark.parametrize("kind", ["skew", "rounded", "ellipse", "outline", "star"])
@pytest.mark.parametrize("background", [(246, 242, 233), (12, 8, 27)])
def test_compositing_on_light_and_dark_backgrounds_has_no_black_fringe(kind, background):
    colour = (20, 185, 224)
    surface = pygame.Surface((140, 90))
    surface.fill(background)
    _draw(surface, kind, colour)
    rgb = pygame.surfarray.array3d(surface).astype(int)
    minimum = np.minimum(colour, background)
    maximum = np.maximum(colour, background)
    assert np.all(rgb >= minimum)
    assert np.all(rgb <= maximum)
    assert tuple(rgb[0, 0]) == background


def test_semtransparent_fill_never_gains_opacity_at_its_edge():
    surface = pygame.Surface((140, 90), pygame.SRCALPHA)
    shapes.polygon(surface, (145, 190, 240, 40), [(20, 12), (120, 12), (102, 65), (4, 65)])
    alpha = pygame.surfarray.array_alpha(surface)
    assert alpha.max() == 40
    assert np.count_nonzero((alpha > 0) & (alpha < 40)) > 20


@pytest.mark.parametrize("kind", ["skew", "rounded", "ellipse", "outline", "star"])
def test_moving_the_shape_reuses_coverage_without_changing_pixels(kind):
    a = pygame.Surface((140, 90), pygame.SRCALPHA)
    b = pygame.Surface((140, 90), pygame.SRCALPHA)
    _draw(a, kind, (150, 66, 222))
    keys = list(shapes._aa_cache)
    _draw(b, kind, (150, 66, 222), (7, 9))
    assert list(shapes._aa_cache) == keys
    assert np.array_equal(pygame.surfarray.array_alpha(a)[:120, :70],
                          pygame.surfarray.array_alpha(b)[7:127, 9:79])


def test_cache_obeys_both_entry_and_memory_budgets(monkeypatch):
    monkeypatch.setattr(shapes, "_AA_LIMIT", 3)
    monkeypatch.setattr(shapes, "_AA_BYTES", 2 * 64 * 64 * 4)
    surface = pygame.Surface((100, 100), pygame.SRCALPHA)
    for value in range(20):
        shapes.rounded_rect(surface, (value, 120, 240), (0, 0, 64, 64), border_radius=10)
        assert len(shapes._aa_cache) <= 3
        assert shapes._aa_bytes <= shapes._AA_BYTES
        assert shapes._aa_bytes == sum(s.get_pitch() * s.get_height()
                                      for s in shapes._aa_cache.values())
    assert len(shapes._aa_cache) == 2
    shapes.rounded_rect(surface, (255, 120, 240), (0, 0, 95, 95), border_radius=10)
    assert len(shapes._aa_cache) == 2, "An oversized one-off must not evict reusable controls."
    shapes.clear_aa_cache()
    assert not shapes._aa_cache
    assert shapes._aa_bytes == 0


def test_antialiased_shapes_obey_the_destination_clip():
    surface = pygame.Surface((140, 90), pygame.SRCALPHA)
    clip = pygame.Rect(30, 25, 25, 15)
    surface.set_clip(clip)
    _draw(surface, "skew", (44, 240, 180))
    alpha = pygame.surfarray.array_alpha(surface)
    assert alpha[40, 30] > 0
    alpha[clip.left:clip.right, clip.top:clip.bottom] = 0
    assert not alpha.any()
    assert surface.get_clip() == clip


@pytest.mark.parametrize("circle", [False, True])
def test_passed_note_outlines_keep_smooth_transparent_edges(circle):
    surface = pygame.Surface((100, 100), pygame.SRCALPHA)
    design.ghost(surface, pygame.Rect(15, 15, 70, 60), (150, 85, 224),
                 alpha=96, radius=15, circle=circle)
    alpha = pygame.surfarray.array_alpha(surface)
    edge = (alpha > 0) & (alpha < 96)
    assert np.count_nonzero(edge) > 30
    assert alpha.max() == 96
    assert alpha[50, 45] == 0
    assert np.all(pygame.surfarray.array3d(surface)[edge] == (150, 85, 224))


def test_stage_body_uses_readable_medium_weight_while_titles_keep_their_identity():
    set_look("stage")
    assert design.reading_font(17) is design.font(17, face="geist", weight="medium")
    assert design.font(30, bold=True) is design.font(30, face="chakra", weight="semibold")
    glyphs = design.reading_font(17).render("Weiter zum Lernpfad", True, (240, 240, 240))
    alpha = pygame.surfarray.array_alpha(glyphs)
    assert np.count_nonzero((alpha > 0) & (alpha < 255)) > 100
    for look in ("studio", "hifi"):
        set_look(look)
        assert design.reading_font(17) is design.font(17)
