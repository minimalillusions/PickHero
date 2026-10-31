"""The Stage look's playing field: a highway that runs towards the player.

Studio and Hi-fi draw strings as rows and move the notes right to left. This
draws four lanes receding to a horizon and moves the notes down the screen,
which is the shape every rhythm game has used since Guitar Hero, and the
reason it works is that the thing you are about to play is the biggest thing
on screen.

PyGame has no perspective transform, so the projection is done by hand. It is
one function, `project`, and everything else asks it: the lane trapezoids, the
beat rungs, the hit rings and the notes all come from the same depth, so they
cannot drift apart. There is no second copy of the time-to-pixel arithmetic
here -- the caller hands in a depth between 0 and 1 and gets back a rect.

Three things were measured before they were built, and two of them are the
opposite of what looks obvious:

  * The depth fog costs 2.515 ms to build per frame and 0.267 ms to blit from
    a prebuilt surface. It is built once per size.
  * Caching the lane trapezoids is 3x SLOWER than drawing them: four live
    polygons cost 0.085 ms, the cached 1280x520 alpha blit costs 0.270.
  * Caching note bodies by depth x string x verdict would need 320 keys
    against design._SPRITE_LIMIT of 240, so it would evict the gates, tails
    and halos every frame and make everything else slower.
"""

from __future__ import annotations

import pygame

from pickhero.audio.note_utils import midi_to_name
from pickhero.ui import design
from pickhero.ui.colors import get_look

# How much smaller the far end is than the near end. 0.26 puts the horizon
# about four bars away at the tempos the player practises at, which is far enough to
# read the shape of a phrase and near enough that the fourth bar is not three
# pixels tall.
FAR_SCALE = 0.26
# Where the strike line sits inside the band, as a fraction from the top. The
# ring needs room below it for its own radius and the glow.
STRIKE_AT = 0.86
LANE_W = 150
RING_R = 46


def band(layout) -> pygame.Rect:
    """The rectangle the highway lives in."""
    height = max(1, int(layout.lane_bottom - layout.lane_top))
    width = min(layout.screen_w, LANE_W * max(1, layout.lane_count) + 120)
    return pygame.Rect((layout.screen_w - width) // 2, int(layout.lane_top),
                       width, height)


def project(area: pygame.Rect, lane: int, lanes: int,
            depth: float) -> tuple[int, int, float]:
    """Where a point on lane `lane` at `depth` lands, and how big it is.

    depth 0.0 is the strike line, 1.0 is the horizon. Returns (x, y, scale).

    The 1/z is what makes it look like a road rather than a trapezoid with
    things sliding along it: equal steps in time have to produce shrinking
    steps on screen, or the notes appear to accelerate into your face.
    """
    depth = max(0.0, min(1.0, depth))
    near, far = 1.0, 1.0 / FAR_SCALE
    z = near + (far - near) * depth
    scale = 1.0 / z
    # Normalised so depth 0 lands on the strike line and depth 1 on the
    # horizon, whatever FAR_SCALE is.
    travel = (1.0 - scale) / (1.0 - FAR_SCALE)
    strike_y = area.top + area.height * STRIKE_AT
    y = strike_y - (strike_y - area.top) * travel
    centre = area.centerx
    span = area.width * 0.5
    offset = (lane + 0.5) / max(1, lanes) * 2.0 - 1.0
    x = centre + offset * span * scale
    return int(x), int(y), scale


def _lane_colour(order: list[int], lane: int) -> tuple[int, int, int]:
    return design.string_colour(order[lane])


def paint(surface: pygame.Surface, layout, order: list[int],
          gates: tuple[int, ...], tuning: dict | None = None) -> None:
    """The road: lane floors, beat rungs, fog, and the hit rings.

    `order` maps a lane index left-to-right onto a string number, so the
    lowest string is on the left the way a bass player sees it. `gates` is one
    brightness per lane, computed once by the caller from the notes it already
    has -- the gate ladder is not recomputed here, because a second copy of it
    would drift from the first.
    """
    look = get_look()
    area = band(layout)
    lanes = max(1, layout.lane_count)

    pygame.draw.rect(surface, look.stage_bg,
                     (0, area.top, layout.screen_w, area.height))

    # Lane floors, drawn live: four polygons cost 0.085 ms, and the cached
    # alpha blit that replaces them costs 0.270.
    for lane in range(lanes):
        nx0, ny0, ns = project(area, lane, lanes, 0.0)
        fx0, fy0, fs = project(area, lane, lanes, 1.0)
        half_near = area.width / lanes * 0.5 * ns
        half_far = area.width / lanes * 0.5 * fs
        colour = _lane_colour(order, lane)
        pygame.draw.polygon(surface, design.mix(look.stage_bg, colour, 0.16), [
            (nx0 - half_near, ny0), (nx0 + half_near, ny0),
            (fx0 + half_far, fy0), (fx0 - half_far, fy0)])
        pygame.draw.line(surface, design.mix(look.stage_bg, colour, 0.5),
                         (nx0 + half_near, ny0), (fx0 + half_far, fy0), 1)
        if lane == 0:
            pygame.draw.line(surface, design.mix(look.stage_bg, colour, 0.5),
                             (nx0 - half_near, ny0), (fx0 - half_far, fy0), 1)

    _rungs(surface, area, lanes)
    surface.blit(_fog(area, look.stage_bg), area.topleft)
    _rings(surface, area, lanes, order, gates, tuning or {})


def _rungs(surface: pygame.Surface, area: pygame.Rect, lanes: int) -> None:
    """Beat lines, which is how you see the road move at all."""
    look = get_look()
    for step in range(1, 9):
        depth = step / 8.0
        x_left, y, scale = project(area, 0, lanes, depth)
        x_right, _, _ = project(area, lanes - 1, lanes, depth)
        half = area.width / lanes * 0.5 * scale
        colour = look.stage_bar_line if step % 4 == 0 else look.stage_beat_line
        pygame.draw.line(surface, colour, (x_left - half, y),
                         (x_right + half, y), 1 if step % 4 else 2)


def _fog(area: pygame.Rect, base: tuple[int, int, int]) -> pygame.Surface:
    """Distance haze, built once per size.

    Measured: 2.515 ms to build, 0.267 ms to blit. At 60 fps the first number
    is a third of the whole frame budget, for a gradient that never changes.
    """
    def paint_fog(layer: pygame.Surface) -> None:
        height = layer.get_height()
        for row in range(height):
            # Opaque at the horizon, clear by a third of the way down.
            t = max(0.0, 1.0 - row / max(1.0, height * 0.42))
            layer.fill(design.with_alpha(base, int(235 * t * t)),
                       (0, row, layer.get_width(), 1))

    return design.sprite(("highway-fog", area.size, base), area.size, paint_fog)


def _rings(surface: pygame.Surface, area: pygame.Rect, lanes: int,
           order: list[int], gates: tuple[int, ...],
           tuning: dict) -> None:
    """The four landing rings and the bar they sit on."""
    look = get_look()
    x0, y0, _ = project(area, 0, lanes, 0.0)
    x1, _, _ = project(area, lanes - 1, lanes, 0.0)
    half = area.width / lanes * 0.5
    pygame.draw.line(surface, look.hit_zone, (x0 - half, y0), (x1 + half, y0), 4)

    for lane in range(lanes):
        x, y, _ = project(area, lane, lanes, 0.0)
        colour = _lane_colour(order, lane)
        lit = gates[lane] if lane < len(gates) else 0
        if lit:
            design.circle_ring(surface, (x, y), RING_R + 8, colour, 3,
                               alpha=min(255, 40 + lit * 30))
            pygame.draw.circle(surface, design.mix(look.stage_bg, colour,
                                                   min(1.0, lit / 6.0)),
                               (x, y), RING_R)
        pygame.draw.circle(surface, colour, (x, y), RING_R, width=6)
        # The ring names its own string. On a road there is no side gutter to
        # put it in, and a landing spot you cannot name is a landing spot you
        # have to work out from the colour alone.
        midi = tuning.get(order[lane]) if tuning else None
        name = midi_to_name(midi) if midi is not None else str(order[lane])
        letter = name[:-1] if name and name[-1].isdigit() else name
        design.text(surface, letter, (x, y - 20), design.ink(look.stage_bg),
                    design.TEXT_XL, bold=True, align="center")
        design.text(surface, name, (x, y + 10),
                    design.ink(look.stage_bg, 0.6), design.TEXT_XS,
                    align="center")


def place(layout, lane: int, lanes: int, depth: float,
          card_h: int) -> tuple[pygame.Rect, float]:
    """Where a note sits on the road, and how big it is there."""
    area = band(layout)
    x, y, scale = project(area, lane, lanes, depth)
    height = max(10, int(card_h * 0.56 * scale))
    width = max(18, int(LANE_W * 0.74 * scale))
    return pygame.Rect(x - width // 2, y - height // 2, width, height), scale
