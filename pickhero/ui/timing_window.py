"""Timing tolerance supplied by the visible Stage note head, in song time.

Pitch credit is separate from this secondary timing judgement. A capsule that
still touches the white target must not be described as too early or too late.
"""

from __future__ import annotations

import math
from functools import lru_cache

from pickhero.ui import paint_stage
from pickhero.ui.page import Page, frac


def _bounded(value: object, default: float, low: float, high: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return min(high, max(low, number)) if math.isfinite(number) else default


def stage_head_window_ms(page: Page, lanes: int, preview_ms: float) -> float:
    """Return a symmetric window covering the whole head/white-line overlap.

    The result is in song milliseconds, matching ``preview_ms`` and the
    matcher clock. It includes integer-pixel rounding, the actual white line's
    thickness and at least one extra millisecond. It does not include a held
    note's tail, a pad's oval or the glow around a head.

    Page contents are neither cached nor mutated. Cache keys retain the actual
    supported canvas dimensions, string count and preview; malformed inputs
    are normalised and extreme values bounded before geometry is evaluated.
    """
    width = int(_bounded(getattr(page, "width", None), 1280, 1, 16384))
    height = int(_bounded(getattr(page, "height", None), 720, 1, 16384))
    count = int(_bounded(lanes, 4, 1, 12))
    preview = _bounded(preview_ms, 8000.0, 1.0, 8000.0)
    return _stage_head_window_ms(width, height, count, preview)


@lru_cache(maxsize=128)
def _stage_head_window_ms(width: int, height: int, lanes: int,
                          preview_ms: float) -> float:
    page = Page(width, height, strings=lanes)
    target = paint_stage.strike_y(page)
    thickness = max(2, frac(height, 0.0049))
    # pygame.draw.line places the extra pixel of an even-width horizontal
    # stroke below its centre. Rect.bottom is exclusive, as for the head.
    line_top = target - (thickness - 1) // 2
    line_bottom = target + thickness // 2 + 1

    def overlaps(depth: float) -> bool:
        head = paint_stage.note_box(page, 0, lanes, depth)
        return head.top < line_bottom and head.bottom > line_top

    def boundary(direction: int, far_depth: float) -> float:
        # Project's valid domain ends at the horizon / the road's runoff.
        # On pathological canvas aspect ratios the head can fill this whole
        # domain; return a finite conservative limit instead of searching
        # forever against a clamped projection.
        if overlaps(direction * far_depth):
            return preview_ms
        inside, outside = 0.0, far_depth
        for _ in range(40):
            middle = (inside + outside) / 2.0
            if overlaps(direction * middle):
                inside = middle
            else:
                outside = middle
        return outside * preview_ms

    early = boundary(1, 1.0)
    late = boundary(-1, 0.45)
    return float(math.ceil(max(early, late)) + 1)
