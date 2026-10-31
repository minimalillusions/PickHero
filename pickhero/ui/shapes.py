"""The shapes the three drawings use that a rounded rectangle cannot make.

Everything here exists because it appears in the mockups and could not be
built from `design.card` and `design.text`. They live in their own module
rather than swelling design.py, which is already the largest file in ui/.

Each takes an explicit colour. None of them reads the look itself: the caller
knows what page it is drawing on, and a primitive that guesses is how the
light theme ended up drawing every control in its own background colour.

Reusable shapes use bounded caches. The antialiased contours have their own
memory budget so a resize cannot evict all of the play screen's soft glows.
"""

from __future__ import annotations

import math
from collections import OrderedDict

import numpy as np
import pygame
import pygame.gfxdraw

from pickhero.ui import design

# How far the Stage look leans. Measured off the mockup: the row's bottom
# edge sits 14 px right of its top edge at a 56 px row, which is 0.25 of the
# height, and the big button leans the same amount so they read as one idea.
SKEW = 0.25

# Cache coverage, not a screenshot of the background: the same edge must
# blend correctly over a light page, a dark page, and a transparent layer.
# Only these small local shapes are supersampled; text keeps SDL_ttf's AA.
_AA_SCALE = 4
# Perspective notes have several hundred small size/colour combinations.
# Retain those without letting a resize exceed the independent byte budget.
_AA_LIMIT = 512
_AA_BYTES = 16 * 1024 * 1024
_aa_cache: OrderedDict[tuple, pygame.Surface] = OrderedDict()
_aa_bytes = 0


def clear_aa_cache() -> None:
    global _aa_bytes
    _aa_cache.clear()
    _aa_bytes = 0


def _aa_sprite(key: tuple, size: tuple[int, int], colour, paint) -> pygame.Surface:
    """Downsample coverage alone, preserving RGB even at transparent edges."""
    global _aa_bytes
    rgba = tuple(pygame.Color(colour))
    key = (key, size, rgba)
    found = _aa_cache.get(key)
    if found is not None:
        _aa_cache.move_to_end(key)
        return found
    scale = _AA_SCALE
    mask = pygame.Surface((size[0] * scale, size[1] * scale), pygame.SRCALPHA)
    paint(mask, scale)
    mask = pygame.transform.smoothscale(mask, size)
    layer = pygame.Surface(size, pygame.SRCALPHA)
    layer.fill((*rgba[:3], 0))
    coverage = pygame.surfarray.array_alpha(mask).astype(np.uint16)
    opacity = pygame.surfarray.pixels_alpha(layer)
    opacity[:] = ((coverage * rgba[3] + 127) // 255).astype(np.uint8)
    del opacity
    cost = layer.get_pitch() * layer.get_height()
    if cost <= _AA_BYTES:
        while _aa_cache and (len(_aa_cache) >= _AA_LIMIT or _aa_bytes + cost > _AA_BYTES):
            _old_key, old = _aa_cache.popitem(last=False)
            _aa_bytes -= old.get_pitch() * old.get_height()
        _aa_cache[key] = layer
        _aa_bytes += cost
    return layer


def polygon(surface: pygame.Surface, colour, points, width: int = 0) -> None:
    """A cached antialiased polygon; translating it reuses its local coverage."""
    if len(points) < 3 or width < 0:
        return
    pad = max(1, (width + 1) // 2)
    left = math.floor(min(p[0] for p in points)) - pad
    top = math.floor(min(p[1] for p in points)) - pad
    right = math.ceil(max(p[0] for p in points)) + pad + 1
    bottom = math.ceil(max(p[1] for p in points)) + pad + 1
    size = (right - left, bottom - top)
    local = tuple((round(x - left, 4), round(y - top, 4)) for x, y in points)

    def paint(mask, scale):
        pygame.draw.polygon(mask, (255, 255, 255, 255),
                            [(round(x * scale), round(y * scale)) for x, y in local],
                            width=width * scale)

    surface.blit(_aa_sprite(("polygon", local, width), size, colour, paint), (left, top))


def rounded_rect(surface: pygame.Surface, colour, rect, width: int = 0,
                 border_radius: int = 0) -> None:
    """An antialiased fill or inward border, within the original rectangle."""
    rect = pygame.Rect(rect)
    if rect.width <= 0 or rect.height <= 0 or width < 0:
        return
    radius = max(0, min(int(border_radius), min(rect.size) // 2))
    if not radius:
        pygame.draw.rect(surface, colour, rect, width=width)
        return

    def paint(mask, scale):
        pygame.draw.rect(mask, (255, 255, 255, 255), mask.get_rect(),
                         width=width * scale, border_radius=radius * scale)

    surface.blit(_aa_sprite(("rect", radius, width), rect.size, colour, paint), rect.topleft)


def ellipse(surface: pygame.Surface, colour, rect, width: int = 0) -> None:
    """An antialiased ellipse with a transparent centre when used as a ring."""
    rect = pygame.Rect(rect)
    if rect.width <= 0 or rect.height <= 0 or width < 0:
        return

    def paint(mask, scale):
        pygame.draw.ellipse(mask, (255, 255, 255, 255), mask.get_rect(), width=width * scale)

    surface.blit(_aa_sprite(("ellipse", width), rect.size, colour, paint), rect.topleft)


def skew_points(rect: pygame.Rect, lean: float = SKEW) -> list[tuple[int, int]]:
    """The four corners of a parallelogram leaning right by `lean` x height.

    The rect is the bounding box, so the shape sits inside it rather than
    spilling past the edge the caller measured against.
    """
    run = int(rect.height * lean)
    return [(rect.left + run, rect.top), (rect.right, rect.top),
            (rect.right - run, rect.bottom), (rect.left, rect.bottom)]


def skewed(surface: pygame.Surface, rect: pygame.Rect,
           fill: tuple[int, int, int] | None, *,
           ring: tuple[int, int, int] | None = None,
           ring_width: int = 1,
           lean: float = SKEW,
           fill_to: tuple[int, int, int] | None = None) -> None:
    """A leaning slab: the Stage look's row, chip and play button.

    `fill_to` gives it a left-to-right gradient. Cached coverage softens the
    diagonals without resampling the text or the whole frame.
    """
    if rect.width <= 0 or rect.height <= 0:
        return
    points = skew_points(rect, lean)
    if fill is not None and fill_to is not None:
        _gradient_polygon(surface, rect, points, fill, fill_to)
    elif fill is not None:
        polygon(surface, fill, points)
    if ring is not None:
        polygon(surface, ring, points, width=ring_width)


def _gradient_polygon(surface: pygame.Surface, rect: pygame.Rect,
                      points: list[tuple[int, int]],
                      left: tuple[int, int, int],
                      right: tuple[int, int, int]) -> None:
    """A horizontal ramp clipped to a polygon.

    pygame has no gradient fill, and per-pixel is far too slow at this size.
    This paints one column per 4 px into a scratch surface and masks it with
    the polygon, which measures 0.21 ms for a full-width row against 11.4 ms
    for the per-pixel version.
    """
    if rect.width <= 0 or rect.height <= 0:
        return
    layer = pygame.Surface(rect.size, pygame.SRCALPHA)
    step = 4
    for x in range(0, rect.width, step):
        t = x / max(1, rect.width - 1)
        layer.fill(design.mix(left, right, t), (x, 0, step, rect.height))
    mask = pygame.Surface(rect.size, pygame.SRCALPHA)
    polygon(mask, (255, 255, 255, 255),
            [(x - rect.left, y - rect.top) for x, y in points])
    opacity = pygame.surfarray.pixels_alpha(layer)
    opacity[:] = pygame.surfarray.array_alpha(mask)
    del opacity
    surface.blit(layer, rect.topleft)


def glow(surface: pygame.Surface, rect: pygame.Rect,
         colour: tuple[int, int, int], *, spread: int = 18,
         alpha: int = 110, radius: int = design.RADIUS_MD,
         lean: float = 0.0, ellipse: bool = False) -> None:
    """A cached, continuous falloff following the actual outline.

    Distance from a rectangular bounding box leaves square holes around a
    rounded note. Sampling transparent black and upscaling it also darkens
    the colour at the edge. Here only alpha varies; even transparent pixels
    carry the light's RGB, and the outline is evaluated at pixel centres.
    """
    if rect.width <= 0 or rect.height <= 0 or spread <= 0 or alpha <= 0:
        return
    pad = int(math.ceil(spread)) + 2
    size = (rect.width + pad * 2, rect.height + pad * 2)
    radius = max(0.0, min(float(radius), rect.width / 2, rect.height / 2))

    def paint(layer: pygame.Surface) -> None:
        x = np.arange(size[0], dtype=np.float32)[:, None] + 0.5 - size[0] / 2
        y = np.arange(size[1], dtype=np.float32)[None, :] + 0.5 - size[1] / 2
        half_w, half_h = rect.width / 2, rect.height / 2
        if ellipse:
            k0 = np.hypot(x / half_w, y / half_h)
            k1 = np.hypot(x / (half_w * half_w), y / (half_h * half_h))
            distance = k0 * (k0 - 1) / np.maximum(k1, 1e-6)
        else:
            half_w = max(0.5, half_w - abs(lean) * half_h)
            corner = min(radius, half_w, half_h)
            qx = np.abs(x + lean * y) - (half_w - corner)
            qy = np.abs(y) - (half_h - corner)
            distance = (np.hypot(np.maximum(qx, 0), np.maximum(qy, 0))
                        + np.minimum(np.maximum(qx, qy), 0) - corner)
        falloff = np.clip(1.0 - distance / spread, 0.0, 1.0) ** 2
        falloff[distance <= 0] = 0.0
        pixels = pygame.surfarray.pixels3d(layer)
        pixels[:] = colour
        del pixels
        opacity = pygame.surfarray.pixels_alpha(layer)
        opacity[:] = (255 * falloff).astype(np.uint8)
        del opacity

    # Opacity changes as a note approaches. Reuse one shape instead of filling
    # the shared cache with every animation frame's alpha value.
    layer = design.sprite(("shape-glow", size, colour, spread, radius, lean, ellipse),
                          size, paint)
    layer.set_alpha(max(0, min(255, int(alpha))))
    surface.blit(layer, (rect.left - pad, rect.top - pad))


def stars(surface: pygame.Surface, pos: tuple[int, int], lit: int,
          total: int = 3, *, size: int = 9, gap: int = 7,
          on: tuple[int, int, int] = (250, 200, 60),
          off: tuple[int, int, int] = (70, 70, 80),
          align: str = "left") -> pygame.Rect:
    """The Stage look's rating. Returns the rect it filled.

    Five-pointed, drawn from the maths rather than from a glyph, because the
    bundled faces do not all carry one and a missing glyph renders as a box.
    """
    width = total * size * 2 + (total - 1) * gap
    x, y = pos
    if align == "center":
        x -= width // 2
    elif align == "right":
        x -= width
    for i in range(total):
        cx = x + size + i * (size * 2 + gap)
        _star(surface, (cx, y), size, on if i < lit else off)
    return pygame.Rect(x, y - size, width, size * 2)


def _star(surface: pygame.Surface, centre: tuple[int, int], size: int,
          colour: tuple[int, int, int]) -> None:
    cx, cy = centre
    points = []
    for i in range(10):
        angle = -math.pi / 2 + i * math.pi / 5
        r = size if i % 2 == 0 else size * 0.42
        points.append((cx + r * math.cos(angle), cy + r * math.sin(angle)))
    polygon(surface, colour, points)


def ring_gauge(surface: pygame.Surface, centre: tuple[int, int], radius: int,
               fraction: float, *, colour: tuple[int, int, int],
               track: tuple[int, int, int], width: int = 10,
               start: float = -math.pi / 2) -> None:
    """An arc filled clockwise from the top. The Stage look's multiplier.

    pygame.draw.arc takes a bounding box and angles measured anticlockwise
    from east, which is neither of the two things a caller wants, so the
    conversion lives here once instead of at each call site.
    """
    fraction = max(0.0, min(1.0, fraction))
    box = pygame.Rect(centre[0] - radius, centre[1] - radius,
                      radius * 2, radius * 2)
    pygame.draw.circle(surface, track, centre, radius, width=width)
    if fraction <= 0.0:
        return
    # pygame sweeps anticlockwise, so the arc runs from the end angle back.
    end = -start
    begin = end - fraction * math.tau
    pygame.draw.arc(surface, colour, box, begin, end, width)


def meter(surface: pygame.Surface, rect: pygame.Rect, level: float, *,
          on: tuple[int, int, int], off: tuple[int, int, int],
          bars: int = 7, rising: bool = True) -> None:
    """The input level, as discrete bars. Both play footers carry one.

    Discrete rather than continuous on purpose: a bar that is either lit or
    not can be counted at a glance and photographed, and the thing the player needs
    from it is "is anything arriving", not a reading in decibels.
    """
    level = max(0.0, min(1.0, level))
    if bars <= 0 or rect.width <= 0:
        return
    step = rect.width / bars
    bar_w = max(2, int(step * 0.62))
    for i in range(bars):
        tall = rect.height if not rising else int(
            rect.height * (0.35 + 0.65 * (i + 1) / bars))
        x = int(rect.left + i * step)
        y = rect.bottom - tall
        pygame.draw.rect(surface, on if (i + 1) / bars <= level else off,
                         (x, y, bar_w, tall), border_radius=1)


def disc(surface: pygame.Surface, centre: tuple[int, int], radius: int,
         fill: tuple[int, int, int], *,
         ring: tuple[int, int, int] | None = None, ring_width: int = 2,
         shadow: tuple[int, int, int] | None = None,
         shadow_drop: int = 3, shadow_spread: int = 5,
         shadow_drift: int = 0, shadow_alpha: int = 42) -> None:
    """A note. Antialiased, and sitting on a soft shadow if one is asked for.

    pygame.draw.circle has no antialiasing at all, so at 24 px a note is a
    polygon with visible steps down its sides -- which on a page whose whole
    claim is precision is the first thing the eye finds. pygame.gfxdraw has
    both halves of what is wanted: a filled circle and an antialiased rim to
    put round it. Measured at 0.0058 ms against 0.0015 for the hard-edged
    version, which at thirty notes on screen is 0.17 ms of a 16.7 ms frame.

    The shadow is what makes the note sit ON the page rather than be a hole
    cut in it, and it is the other half of why the drawing looks like an
    object. It is built once per (radius, colour) and blitted.
    """
    if radius < 1:
        return
    x, y = int(centre[0]), int(centre[1])
    if shadow is not None and shadow_spread > 0:
        # drift moves it sideways. A light source overhead and to the right
        # throws the shadow down and to the LEFT, and a shadow that falls
        # straight down reads as the note floating rather than lying on the
        # page -- there is nowhere for the light to be coming from.
        surface.blit(_cached_shadow(radius, shadow, shadow_spread,
                                    shadow_alpha),
                     (x - radius - shadow_spread + shadow_drift,
                      y - radius - shadow_spread + shadow_drop))
    pygame.gfxdraw.filled_circle(surface, x, y, radius, fill)
    pygame.gfxdraw.aacircle(surface, x, y, radius, fill)
    if ring is not None:
        for step in range(max(1, ring_width)):
            pygame.gfxdraw.aacircle(surface, x, y, radius - step, ring)


# Built once per (radius, colour, spread) and kept. A per-pixel ramp is far
# too slow to build per frame and costs nothing to blit; this is the same
# trade the highway makes with its fog.
_shadows: dict[tuple, pygame.Surface] = {}
_SHADOW_LIMIT = 48


def _cached_shadow(radius: int, colour: tuple[int, int, int],
                   spread: int, alpha: int = 42) -> pygame.Surface:
    key = (radius, colour, spread, alpha)
    found = _shadows.get(key)
    if found is None:
        if len(_shadows) >= _SHADOW_LIMIT:
            _shadows.clear()
        found = _shadows[key] = _shadow(radius, colour, spread, alpha)
    return found


def _shadow(radius: int, colour: tuple[int, int, int],
            spread: int, alpha: int = 42) -> pygame.Surface:
    """A soft disc, darkest at the rim and gone by `spread`. Cached.

    Measured off the drawing: two pixels under a note the page has darkened
    by 33 of 236, six pixels under by 22, ten by 10, and by fourteen it is
    gone -- very nearly a straight ramp over a wide spread, which is the
    shape that reads as a shadow rather than as a dark rim.

    Written pixel by pixel rather than as nested circles. Nested circles were
    the obvious way and they are wrong: each one composites over the last, so
    the middle accumulates every layer and lands at 59 points against the
    drawing's 33 while four pixels out it is already at 5. Per-pixel costs
    more, but this is built once per radius and colour and blitted after
    that, so what it costs is nothing per frame.
    """
    size = (radius + spread) * 2
    layer = pygame.Surface((size, size), pygame.SRCALPHA)
    middle = size // 2
    r, g, b = colour
    for y in range(size):
        dy = y - middle
        for x in range(size):
            dx = x - middle
            distance = math.hypot(dx, dy) - radius
            if distance <= 0.0:
                value = alpha
            elif distance >= spread:
                continue
            else:
                value = int(alpha * (1.0 - distance / spread))
            if value > 0:
                layer.set_at((x, y), (r, g, b, value))
    return layer


def ring_shadow(surface: pygame.Surface, centre: tuple[int, int], radius: int,
                colour: tuple[int, int, int], *, spread: int = 7,
                drop: int = 4, drift: int = 0, alpha: int = 42) -> None:
    """The same shadow on its own, for a control that draws its own face."""
    x, y = int(centre[0]), int(centre[1])
    surface.blit(_cached_shadow(radius, colour, spread, alpha),
                 (x - radius - spread + drift, y - radius - spread + drop))


def knob(surface: pygame.Surface, centre: tuple[int, int], radius: int,
         fraction: float, *, face: tuple[int, int, int],
         edge: tuple[int, int, int], marker: tuple[int, int, int],
         shadow: tuple[int, int, int] | None = None) -> None:
    """The Hi-fi look's tempo dial: a pale disc with a tick.

    The mockup gives it a soft top-left highlight, which is one lighter disc
    offset by a fifth of the radius -- enough to read as a physical control
    on paper without pretending to be a photograph.
    """
    disc(surface, centre, radius, edge, shadow=shadow, shadow_drop=4,
         shadow_spread=7 if shadow else 0)
    disc(surface, centre, radius - 1, face)
    lift = design.mix(face, (255, 255, 255), 0.55)
    disc(surface, (centre[0] - radius // 5, centre[1] - radius // 4),
         int(radius * 0.62), lift)
    disc(surface, centre, int(radius * 0.74), face)
    # The tick sweeps 300 degrees, leaving a gap at the bottom the way a
    # physical dial does, so the ends of the range are distinguishable.
    angle = math.radians(-240 + 300 * max(0.0, min(1.0, fraction)))
    inner, outer = radius * 0.30, radius * 0.78
    pygame.draw.line(
        surface, marker,
        (centre[0] + inner * math.cos(angle), centre[1] + inner * math.sin(angle)),
        (centre[0] + outer * math.cos(angle), centre[1] + outer * math.sin(angle)),
        max(2, radius // 10))


def hints(surface: pygame.Surface, pos: tuple[int, int],
          pairs: list[tuple[str, str]], *, ink: tuple[int, int, int],
          key_ink: tuple[int, int, int], size: int = design.TEXT_XS,
          gap: int = design.SPACE_6, align: str = "left",
          stacked: bool = False) -> pygame.Rect:
    """Keyboard hints as plain text, which is what all three drawings show.

    They were four filled buttons, and buttons promise a click, carry a
    border each and take four times the room to say the same thing. The key
    is set in the mono face so it reads as something to press.

    `pairs` is (key, what it does). `stacked` puts the key under the word,
    which is the Hi-fi play footer; otherwise they sit side by side.
    """
    x, y = pos
    width = 0
    for key, label in pairs:
        kw = design.font(size, mono=True).size(key)[0]
        lw = design.font(size).size(label)[0]
        width += (max(kw, lw) if stacked else kw + 6 + lw) + gap
    width = max(0, width - gap)
    if align == "center":
        x -= width // 2
    elif align == "right":
        x -= width

    cursor = x
    for key, label in pairs:
        if stacked:
            lw = design.text(surface, label, (cursor, y), ink, size).width
            kw = design.text(surface, key, (cursor, y + size + 6), key_ink,
                             size, mono=True).width
            cursor += max(kw, lw) + gap
        else:
            kw = design.text(surface, key, (cursor, y), key_ink, size,
                             mono=True).width
            lw = design.text(surface, label, (cursor + kw + 6, y), ink,
                             size).width
            cursor += kw + 6 + lw + gap
    return pygame.Rect(x, y, width, size * (2 if stacked else 1) + 6)


def popover(surface: pygame.Surface, rect: pygame.Rect, *,
            fill: tuple[int, int, int], edge: tuple[int, int, int],
            radius: int = design.RADIUS_LG, shadow: int = 26,
            lean: float = 0.0) -> None:
    """A card that floats above the page, anchored by the caller.

    All three drawings put the setup panel beside the button that opens it,
    not in the middle of a blacked-out screen. What makes it read as floating
    is the shadow, so that is what this is: a stack of fading offset shapes
    below the card, then the card.
    """
    def paint(layer: pygame.Surface) -> None:
        for step in range(shadow, 0, -2):
            a = int(60 * (1.0 - step / shadow) ** 2)
            if a <= 0:
                continue
            box = pygame.Rect(shadow - step, shadow - step + step // 2,
                              rect.width + step * 2, rect.height + step * 2)
            if lean:
                pygame.draw.polygon(layer, (0, 0, 0, a),
                                    skew_points(box, lean))
            else:
                pygame.draw.rect(layer, (0, 0, 0, a), box,
                                 border_radius=radius + step)

    size = (rect.width + shadow * 2, rect.height + shadow * 2 + shadow // 2)
    layer = design.sprite(("popover-shadow", size, radius, shadow, lean),
                          size, paint)
    surface.blit(layer, (rect.left - shadow, rect.top - shadow))
    if lean:
        skewed(surface, rect, fill, ring=edge, ring_width=1, lean=lean)
    else:
        pygame.draw.rect(surface, fill, rect, border_radius=radius)
        pygame.draw.rect(surface, edge, rect, width=1, border_radius=radius)
