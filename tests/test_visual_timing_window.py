"""The complete visible Stage head receives in-time credit at the target."""

import math
from types import SimpleNamespace

import pygame
import pytest

from pickhero.ui import paint_stage, timing_window
from pickhero.ui.page import Page
from pickhero.ui.timing_window import stage_head_window_ms


def _drawn_target(page):
    """Get actual SDL raster bounds, independently of the helper's formula."""
    surface = pygame.Surface((3, page.height))
    y = paint_stage.strike_y(page)
    return pygame.draw.line(surface, (255, 255, 255), (0, y), (2, y),
                            max(2, round(page.height * 0.0049)))


def _touches(page, lanes, preview, elapsed, target):
    rect = paint_stage.note_box(page, 0, lanes, -elapsed / preview)
    return rect.top < target.bottom and rect.bottom > target.top


@pytest.mark.parametrize("size", [(320, 240), (640, 360), (1280, 720),
                                  (2560, 1080), (2560, 240)])
@pytest.mark.parametrize("lanes", [4, 6])
@pytest.mark.parametrize("preview", [750.0, 1358.696, 2717.391, 5434.783, 8000.0])
def test_every_head_touch_is_inside_the_window_and_both_outer_edges_are_clear(
        size, lanes, preview):
    page = Page(*size, strings=lanes)
    target = _drawn_target(page)
    window = stage_head_window_ms(page, lanes, preview)
    assert math.isfinite(window) and window >= 1
    assert not _touches(page, lanes, preview, -window, target)
    assert not _touches(page, lanes, preview, window, target)
    # The maximum of the two actual boundaries lies 1--2 ms inside the
    # returned integer tolerance. Check the raster immediately either side.
    assert any(_touches(page, lanes, preview, sign * (window - 2), target)
               for sign in [-1, 1])
    for direction in [-1, 1]:
        # A dense local sweep independently verifies all boundary pixels;
        # it does not repeat the production bisection algorithm.
        for quarter_ms in range(-16, 17):
            elapsed = direction * (window + quarter_ms / 4)
            if _touches(page, lanes, preview, elapsed, target):
                assert abs(elapsed) < window
        assert not _touches(page, lanes, preview,
                            direction * (window + 20), target)


def test_line_thickness_is_part_of_the_visual_timing_window():
    page = Page(1280, 720)
    window = stage_head_window_ms(page, 4, 8000)
    target = _drawn_target(page)
    # At this real frame the head has left the single centre row, but still
    # touches one of the white line's other painted rows.
    elapsed = 165.0
    head = paint_stage.note_box(page, 0, 4, -elapsed / 8000)
    assert head.top > paint_stage.strike_y(page)
    assert _touches(page, 4, 8000, elapsed, target)
    assert elapsed < window


def test_sparse_wide_head_is_not_limited_to_the_old_150_ms_window():
    assert stage_head_window_ms(Page(2560, 1080), 4, 8000) > 200
    assert stage_head_window_ms(Page(1280, 720), 4, 750) < 40


def test_helper_caches_geometry_without_retaining_or_changing_the_page(monkeypatch):
    timing_window._stage_head_window_ms.cache_clear()
    page = Page(1280, 720, title="unchanged", strings=4)
    before = dict(vars(page))
    first = stage_head_window_ms(page, 4, 8000)
    original = paint_stage.note_box
    calls = []

    def record(*args, **kwargs):
        calls.append(args)
        return original(*args, **kwargs)

    monkeypatch.setattr(paint_stage, "note_box", record)
    assert stage_head_window_ms(Page(1280, 720, title="another frame"), 4, 8000) == first
    assert not calls
    assert vars(page) == before
    for other, lanes, preview in [(Page(1281, 720), 4, 8000),
                                  (Page(1280, 721), 4, 8000),
                                  (page, 6, 8000), (page, 4, 7999)]:
        calls.clear()
        stage_head_window_ms(other, lanes, preview)
        assert calls


@pytest.mark.parametrize("invalid", [None, "bad", math.nan, math.inf, -math.inf,
                                    -1e100, 0, 1e100, 10 ** 400])
def test_untrusted_geometry_values_cannot_produce_unbounded_or_nonfinite_timing(invalid):
    page = SimpleNamespace(width=invalid, height=invalid)
    result = stage_head_window_ms(page, invalid, invalid)
    assert math.isfinite(result)
    assert 1 <= result <= 8001


def test_invalid_missing_page_geometry_uses_supported_defaults():
    assert stage_head_window_ms(None, None, None) == stage_head_window_ms(
        Page(1280, 720), 4, 8000)


def test_geometry_cache_has_a_fixed_memory_bound():
    timing_window._stage_head_window_ms.cache_clear()
    for width in range(320, 520):
        stage_head_window_ms(Page(width, 360), 4, 8000)
    cache = timing_window._stage_head_window_ms.cache_info()
    assert cache.maxsize == 128
    assert cache.currsize == 128
