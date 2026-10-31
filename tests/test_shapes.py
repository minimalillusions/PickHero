"""The shapes the mockups use that a rounded rectangle cannot make.

These are drawn, so they are tested by drawing them and reading the pixels
back. Two earlier guards in this project were written against a helper rather
than against the screen and both survived putting the old code back; a shape
test that only checks the function returns is the same mistake in a new file.
"""

import os
import time

import pytest

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
import pygame  # noqa: E402

from pickhero.ui import design, shapes  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def _display():
    pygame.init()
    pygame.display.set_mode((640, 480))
    yield
    pygame.quit()


@pytest.fixture(autouse=True)
def _fresh():
    design.clear_caches()


WHITE = (255, 255, 255)
BLACK = (0, 0, 0)


def _canvas(size=(240, 160)):
    surface = pygame.Surface(size)
    surface.fill(BLACK)
    return surface


class TestSkew:
    """The Stage look leans. Everything that leans has to lean the same way."""

    def test_it_stays_inside_the_rect_it_was_given(self):
        """The caller measured a column against this rect. A shape that
        spills past it overlaps the score, which is what a bounding box is
        for."""
        rect = pygame.Rect(20, 30, 180, 60)
        for x, y in shapes.skew_points(rect):
            assert rect.left <= x <= rect.right, (x, rect)
            assert rect.top <= y <= rect.bottom, (y, rect)

    def test_it_leans_right_the_way_the_drawing_does(self):
        rect = pygame.Rect(0, 0, 100, 40)
        top_left, _top_right, _bottom_right, bottom_left = shapes.skew_points(rect)
        assert top_left[0] > bottom_left[0], "the top must sit right of the bottom"
        assert top_left[0] - bottom_left[0] == int(40 * shapes.SKEW)

    def test_a_flat_lean_is_an_ordinary_rectangle(self):
        rect = pygame.Rect(5, 5, 50, 20)
        assert shapes.skew_points(rect, 0.0) == [
            (5, 5), (55, 5), (55, 25), (5, 25)]

    def test_the_corner_is_empty_and_the_middle_is_not(self):
        surface = _canvas()
        rect = pygame.Rect(20, 20, 160, 80)
        shapes.skewed(surface, rect, WHITE)
        assert surface.get_at((rect.centerx, rect.centery))[:3] == WHITE
        # Bottom-right corner: outside a shape that leans right.
        assert surface.get_at((rect.right - 2, rect.bottom - 2))[:3] == BLACK

    def test_the_gradient_runs_left_to_right(self):
        surface = _canvas((240, 60))
        rect = pygame.Rect(0, 0, 240, 60)
        shapes.skewed(surface, rect, (240, 240, 240), fill_to=(20, 20, 20),
                      lean=0.0)
        left = surface.get_at((6, 30))[0]
        right = surface.get_at((233, 30))[0]
        assert left > right + 120, f"{left} then {right} is not a ramp"

    def test_the_gradient_is_cut_to_the_lean_not_to_the_box(self):
        """The ramp is painted as columns into a scratch surface and then
        masked. Without the mask it fills the bounding box instead, and the
        row stops leaning while still looking like a gradient -- which the
        flat-lean gradient test cannot see, because there the mask is a
        no-op."""
        surface = _canvas((240, 80))
        rect = pygame.Rect(0, 0, 240, 80)
        shapes.skewed(surface, rect, (240, 240, 240), fill_to=(200, 200, 200))
        # Bottom-right: inside the box, outside a shape that leans right.
        assert surface.get_at((rect.right - 3, rect.bottom - 3))[:3] == BLACK
        # Top-left: same, on the other corner.
        assert surface.get_at((rect.left + 2, rect.top + 2))[:3] == BLACK
        assert surface.get_at((rect.centerx, rect.centery))[:3] != BLACK

    def test_a_zero_sized_rect_does_not_raise(self):
        """Windows get dragged very small, and a column can reach zero before
        the layout notices."""
        surface = _canvas()
        shapes.skewed(surface, pygame.Rect(10, 10, 0, 30), WHITE, fill_to=BLACK)
        shapes.skewed(surface, pygame.Rect(10, 10, 40, 0), WHITE, fill_to=BLACK)


class TestGlow:
    def test_it_fades_outward(self):
        surface = _canvas((200, 140))
        rect = pygame.Rect(60, 40, 80, 60)
        shapes.glow(surface, rect, WHITE, spread=16, alpha=200)
        near = surface.get_at((rect.left - 3, rect.centery))[0]
        far = surface.get_at((rect.left - 14, rect.centery))[0]
        assert near > far, f"near {near} should be brighter than far {far}"

    def test_it_does_not_paint_the_middle(self):
        """It is a halo round a shape the caller draws next, not a fill. If
        it painted the middle the row's own colour would be lost under it."""
        surface = _canvas((200, 140))
        rect = pygame.Rect(60, 40, 80, 60)
        shapes.glow(surface, rect, WHITE, spread=16)
        assert surface.get_at((rect.centerx, rect.centery))[:3] == BLACK

    def test_it_is_cached_rather_than_rebuilt(self):
        """A per-frame falloff is the one thing in this module expensive
        enough to matter at 60 fps."""
        surface = _canvas((300, 200))
        rect = pygame.Rect(40, 40, 200, 60)
        shapes.glow(surface, rect, WHITE)        # build
        start = time.perf_counter()
        for _ in range(40):
            shapes.glow(surface, rect, WHITE)
        per_frame = (time.perf_counter() - start) / 40 * 1000
        assert per_frame < 1.0, f"{per_frame:.3f} ms a frame is a blur, not a blit"


class TestStars:
    def test_the_lit_ones_come_first(self):
        surface = _canvas((160, 60))
        shapes.stars(surface, (20, 30), 2, on=WHITE, off=(40, 40, 40))
        # Sample the centre of each star.
        centres = [20 + 9 + i * (9 * 2 + 7) for i in range(3)]
        shades = [surface.get_at((cx, 30))[0] for cx in centres]
        assert shades[0] > 200 and shades[1] > 200, shades
        assert shades[2] < 100, shades

    def test_alignment_moves_it_rather_than_resizing_it(self):
        surface = _canvas((300, 60))
        left = shapes.stars(surface, (0, 30), 1, align="left")
        right = shapes.stars(surface, (300, 30), 1, align="right")
        centre = shapes.stars(surface, (150, 30), 1, align="center")
        assert left.width == right.width == centre.width
        assert left.left == 0
        assert right.right == 300
        assert abs(centre.centerx - 150) <= 1

    def test_none_lit_still_draws_the_row(self):
        """An unplayed piece shows three empty stars, not nothing -- the row
        would otherwise lose its column and the list would stop lining up."""
        surface = _canvas((160, 60))
        shapes.stars(surface, (20, 30), 0, on=WHITE, off=(80, 80, 80))
        assert surface.get_at((29, 30))[0] > 40


class TestRingGauge:
    def test_it_fills_clockwise_from_the_top(self):
        surface = _canvas((200, 200))
        shapes.ring_gauge(surface, (100, 100), 60, 0.25, colour=WHITE,
                          track=(30, 30, 30), width=10)
        # A quarter turn clockwise from the top ends at the right-hand side.
        right = surface.get_at((155, 100))[0]
        left = surface.get_at((45, 100))[0]
        assert right > 200, f"right side should be filled, got {right}"
        assert left < 100, f"left side should still be track, got {left}"

    def test_empty_draws_only_the_track(self):
        surface = _canvas((200, 200))
        shapes.ring_gauge(surface, (100, 100), 60, 0.0, colour=WHITE,
                          track=(40, 40, 40), width=10)
        assert surface.get_at((100, 42))[0] < 100

    @pytest.mark.parametrize("fraction", [-2.0, 0.0, 0.5, 1.0, 3.0])
    def test_it_clamps(self, fraction):
        surface = _canvas((200, 200))
        shapes.ring_gauge(surface, (100, 100), 60, fraction, colour=WHITE,
                          track=(40, 40, 40))


class TestMeter:
    def test_more_signal_lights_more_bars(self):
        def lit(level):
            surface = _canvas((120, 60))
            rect = pygame.Rect(10, 10, 100, 40)
            shapes.meter(surface, rect, level, on=WHITE, off=(20, 20, 20),
                         bars=7)
            return sum(1 for i in range(7)
                       if surface.get_at((int(10 + i * 100 / 7) + 2,
                                          rect.bottom - 3))[0] > 200)
        assert lit(0.0) == 0
        assert 0 < lit(0.5) < 7
        assert lit(1.0) == 7

    def test_it_survives_a_column_with_no_room(self):
        surface = _canvas()
        shapes.meter(surface, pygame.Rect(5, 5, 0, 20), 0.5, on=WHITE,
                     off=BLACK)
        shapes.meter(surface, pygame.Rect(5, 5, 40, 20), 0.5, on=WHITE,
                     off=BLACK, bars=0)


class TestKnob:
    def test_the_tick_moves_with_the_value(self):
        def tick_side(fraction):
            surface = _canvas((140, 140))
            shapes.knob(surface, (70, 70), 40, fraction, face=(240, 240, 240),
                        edge=(180, 180, 180), marker=(255, 0, 0))
            reds = [(x, y) for x in range(140) for y in range(140)
                    if surface.get_at((x, y))[:3] == (255, 0, 0)]
            assert reds, "the tick was not drawn"
            return sum(x for x, _ in reds) / len(reds)
        assert tick_side(0.0) < 70 < tick_side(1.0)

    def test_the_sweep_leaves_a_gap_at_the_bottom(self):
        """A dial that goes all the way round has no readable ends."""
        surface = _canvas((140, 140))
        for fraction in (0.0, 0.5, 1.0):
            shapes.knob(surface, (70, 70), 40, fraction, face=(240, 240, 240),
                        edge=(180, 180, 180), marker=(255, 0, 0))
        column = [surface.get_at((70, y))[:3] for y in range(100, 118)]
        assert (255, 0, 0) not in column, "the tick reached straight down"


class TestHints:
    def test_both_the_key_and_the_word_are_drawn(self):
        surface = _canvas((300, 60))
        rect = shapes.hints(surface, (10, 20), [("Enter", "spielen")],
                            ink=WHITE, key_ink=(255, 0, 0))
        painted = {surface.get_at((x, y))[:3]
                   for x in range(rect.left, rect.right)
                   for y in range(rect.top, rect.bottom + 4)}
        assert any(c[0] > 180 and c[1] > 180 for c in painted), "no word"
        assert any(c[0] > 150 and c[1] < 90 for c in painted), "no key"

    def test_the_reported_width_covers_what_was_drawn(self):
        """The footer packs these against other things; a rect that lies
        about its width is how two of them end up on top of each other."""
        surface = _canvas((640, 80))
        pairs = [("↑↓", "wählen"), ("Enter", "spielen"), ("S", "Setup")]
        rect = shapes.hints(surface, (10, 20), pairs, ink=WHITE,
                            key_ink=WHITE)
        for x in range(rect.right + 2, 640):
            for y in range(rect.top, rect.bottom):
                assert surface.get_at((x, y))[:3] == BLACK, (x, y)

    def test_stacked_puts_the_key_under_the_word(self):
        surface = _canvas((200, 80))
        shapes.hints(surface, (10, 10), [("A", "Hören")], ink=WHITE,
                     key_ink=(255, 0, 0), stacked=True)
        # Antialiasing means no pixel is the exact colour asked for, so the
        # two inks are told apart by which channel dominates.
        reds, whites = [], []
        for y in range(80):
            for x in range(200):
                r, g, b = surface.get_at((x, y))[:3]
                if r > 90 and g < r // 2:
                    reds.append(y)
                elif r > 140 and g > 140 and b > 140:
                    whites.append(y)
        assert reds, "the key was not drawn"
        assert whites, "the word was not drawn"
        assert min(reds) > max(whites), "the key is not below the word"

    @pytest.mark.parametrize("align", ["left", "center", "right"])
    def test_alignment_never_runs_off_the_left_edge(self, align):
        surface = _canvas((400, 60))
        rect = shapes.hints(surface, (200, 20), [("Esc", "Menü")], ink=WHITE,
                            key_ink=WHITE, align=align)
        assert rect.left >= 0


class TestPopover:
    def test_the_card_is_opaque_and_the_shadow_is_not(self):
        # Mid grey, not black: the shadow IS black, so on a black page it is
        # invisible and the assertion below would pass without it.
        surface = pygame.Surface((300, 240))
        surface.fill((128, 128, 128))
        rect = pygame.Rect(80, 60, 140, 100)
        shapes.popover(surface, rect, fill=WHITE, edge=(200, 200, 200))
        assert surface.get_at((rect.centerx, rect.centery))[:3] == WHITE
        page = (128, 128, 128)
        below = surface.get_at((rect.centerx, rect.bottom + 6))[:3]
        assert below[0] < page[0] - 8, (
            f"nothing lifts the card off the page: {below} on {page}")
        assert below[0] > 20, f"the shadow is a hole, not a shadow: {below}"

    def test_it_does_not_black_out_the_screen(self):
        """The first version of the setup panel used an opaque scrim and hid
        the entire library behind a wall."""
        surface = pygame.Surface((400, 300))
        surface.fill((128, 128, 128))
        rect = pygame.Rect(240, 40, 140, 100)
        shapes.popover(surface, rect, fill=WHITE, edge=(180, 180, 180))
        assert surface.get_at((20, 280))[:3] == (128, 128, 128)


class TestTheDisc:
    """A note on paper. Antialiased, and standing on a shadow.

    Both halves were reported by comparing the running app with the design
    drawing: the circles had no antialiasing and the light shadow beneath them
    was missing. Both are measured here, because both were got wrong more
    than once by reasoning about them instead.
    """

    def test_the_rim_is_antialiased(self):
        """pygame.draw.circle has no antialiasing at all, so at this size a
        note is a polygon with visible steps down its sides.

        Compared against that circle rather than judged on its own: "how many
        shades is enough" is a number I would have had to invent, and the
        thing that actually matters is that this is smoother than what it
        replaced.
        """
        def shades(draw):
            surface = _canvas((120, 120))
            surface.fill((236, 235, 231))
            draw(surface)
            # Every pixel in the box, so the whole rim is covered whatever
            # angle it crosses a pixel at.
            return {surface.get_at((x, y))[0]
                    for x in range(26, 95) for y in range(26, 95)}

        hard = shades(lambda s: pygame.draw.circle(s, (28, 27, 26), (60, 60), 30))
        soft = shades(lambda s: shapes.disc(s, (60, 60), 30, (28, 27, 26)))
        assert len(hard) <= 3, f"the hard circle already has {len(hard)} shades"
        assert len(soft) > len(hard) + 6, (
            f"antialiased gives {len(soft)} shades against {len(hard)} hard")

    def test_the_ramp_falls_away_in_a_straight_line(self):
        """The SHAPE is the drawing's, and it is the shape that makes this
        read as a shadow rather than as a dark rim.

        Measured off the Hi-fi sheet at its own spread: 33 points of 236 two
        pixels out, 22 at six, 10 at ten, nothing by fourteen -- very nearly
        straight. Two wrong versions before that, both from reasoning instead
        of measuring: a peak of 28 over a spread of 3 was invisible, and
        nested circles at 48 accumulated to 59 hard against the edge and 7
        four pixels out.

        The weight and the reach are no longer the drawing's -- both were
        asked smaller -- so this checks the ramp and the two tests below
        check what was chosen.
        """
        radius, spread, alpha = 19, 14, 42
        sprite = shapes._cached_shadow(radius, (28, 27, 26), spread, alpha)
        middle = sprite.get_width() // 2
        read = [sprite.get_at((middle, middle + radius + d))[3]
                for d in range(0, spread)]
        steps = [a - b for a, b in zip(read, read[1:])]
        # Two, not one: the alpha is an integer, so a ramp of 42 over 14 px
        # steps by 3 and rounds to 2 or 4 along the way. Anything wider than
        # that is a curve, which is the dark-rim failure.
        assert max(steps) - min(steps) <= 2, f"not a straight ramp: {read}"
        assert read[0] == alpha, f"the rim is {read[0]}, not {alpha}"
        assert read[-1] <= 4, f"it has not faded out by {spread} px: {read[-1]}"

    def test_the_shadow_falls_away_from_the_light(self):
        surface = _canvas((140, 140))
        surface.fill((240, 240, 240))
        shapes.disc(surface, (70, 60), 24, (20, 20, 20), shadow=(20, 20, 20),
                    shadow_drop=4, shadow_spread=12)
        below = surface.get_at((70, 60 + 24 + 6))[0]
        above = surface.get_at((70, 60 - 24 - 6))[0]
        assert below < above, f"below {below} is not darker than above {above}"

    def test_it_is_built_once_and_kept(self):
        """A per-pixel ramp is far too slow to build per frame, and this is
        the hot screen."""
        shapes._shadows.clear()
        surface = _canvas((200, 200))
        shapes.disc(surface, (100, 100), 24, (20, 20, 20), shadow=(20, 20, 20))
        built = len(shapes._shadows)
        start = time.perf_counter()
        for _ in range(200):
            shapes.disc(surface, (100, 100), 24, (20, 20, 20),
                        shadow=(20, 20, 20))
        per_note = (time.perf_counter() - start) / 200 * 1000
        assert len(shapes._shadows) == built, "it rebuilt the shadow"
        assert per_note < 0.2, f"{per_note:.3f} ms a note"

    def test_without_a_shadow_nothing_is_drawn_under_it(self):
        surface = _canvas((140, 140))
        surface.fill((240, 240, 240))
        shapes.disc(surface, (70, 70), 24, (20, 20, 20))
        assert surface.get_at((70, 70 + 24 + 6))[:3] == (240, 240, 240)

    def test_a_disc_with_no_room_does_not_raise(self):
        surface = _canvas()
        shapes.disc(surface, (20, 20), 0, (20, 20, 20), shadow=(0, 0, 0))
        shapes.disc(surface, (20, 20), -3, (20, 20, 20))


class TestHowBigTheShadowIs:
    """The ramp's shape is the drawing's; its size is theirs.

    tests above fix the profile -- a near-straight fall from the rim, which is
    what makes it read as a shadow. This fixes how far it reaches, which is a
    separate decision and one the player made by looking at it: at the drawing's own
    spread it ran to 14 px and the player asked for smaller.
    """

    def test_the_spread_is_the_one_she_asked_for(self):
        from pickhero.ui import scrolling

        assert scrolling.SHADOW_SPREAD < 0.42, (
            f"{scrolling.SHADOW_SPREAD} is back up at the drawing's reach")
        assert scrolling.SHADOW_SPREAD > 0.22, (
            f"{scrolling.SHADOW_SPREAD} leaves the note with no ground")

    def test_it_has_faded_out_within_eight_pixels_of_a_note(self):
        from pickhero.ui import scrolling

        radius = 19
        sprite = self._as_drawn(radius)
        middle = sprite.get_width() // 2
        edge = middle + radius + 8
        gone = (0 if edge >= sprite.get_height()
                else sprite.get_at((middle, edge))[3])
        assert gone <= 3, f"eight pixels out it is still at alpha {gone}"

    def test_it_still_touches_the_page_at_all(self):
        """Lighter twice over, so this is the floor: below it the note stops
        standing on anything and the page goes back to being a hole."""
        from pickhero.ui import scrolling

        radius = 19
        sprite = self._as_drawn(radius)
        middle = sprite.get_width() // 2
        near = sprite.get_at((middle, middle + radius + 2))[3]
        assert near >= 10, f"two pixels out it is only alpha {near}"
        assert scrolling.SHADOW_ALPHA <= 26, (
            f"{scrolling.SHADOW_ALPHA} is back up at the drawing's weight")

    def test_it_falls_down_and_to_the_left(self):
        """A shadow straight under a note has no light source, so the note
        reads as floating rather than as lying on the page. Down and left
        puts the light overhead and to the right, where a desk lamp is."""
        from pickhero.ui import scrolling

        assert scrolling.SHADOW_DRIFT < 0, "it does not drift left"
        assert scrolling.SHADOW_DROP > 0, "it does not fall"

        surface = _canvas((160, 160))
        surface.fill((236, 235, 231))
        radius = 24
        shapes.disc(surface, (80, 70), radius, (28, 27, 26),
                    shadow=(28, 27, 26), shadow_drop=4, shadow_spread=10,
                    shadow_drift=-5, shadow_alpha=30)
        left = surface.get_at((80 - radius - 4, 70 + radius - 2))[0]
        right = surface.get_at((80 + radius + 4, 70 + radius - 2))[0]
        assert left < right, f"left {left} is not darker than right {right}"

    @staticmethod
    def _as_drawn(radius: int):
        """The shadow exactly as the play screen asks for it."""
        from pickhero.ui import scrolling

        return shapes._cached_shadow(
            radius, (28, 27, 26),
            max(4, round(radius * scrolling.SHADOW_SPREAD)),
            scrolling.SHADOW_ALPHA)
