"""The Stage look's two pages: a library in two columns, and a highway.

Every number here was measured off scratchpad/skins/seite3.png rather than
guessed. The drawing is a 1280x766 sheet, the window is 1280x720 and
resizable, so what is stored is the fraction and not the pixel -- reading a y
straight off the sheet puts everything 46 px low.

Three things in the drawing are not what a first reading of it suggests, and
all three were measured before they were drawn:

  The lean is 0.145, not shapes.SKEW. The rows and the play button both move
  their left edge 10 px over a 68 px height; 0.25 is the play screen's note
  slab, which is a different shape in a different drawing.

  The rows are NOT filled. They are a 3 % lift of ink_1 over whatever is
  underneath, which is why the top rows are violet and the bottom ones are
  not: the wash behind them is what makes the gradient. Painting the ramp
  into each row instead gets row seven wrong, and row seven is the one that
  shows whether it was measured or invented.

  Only four things are italic -- the wordmark, the SETUP heading, the play
  button and everything inside a list row. The big title, the chips, the
  stat tiles and the key hints stand upright in the drawing. "Italic
  throughout" would be a different and much noisier page.

Colours come from the Look and from nothing else. Where the drawing sits
between two of them the two are mixed, and the mix says what it is for.

── The play page, and what it hands back ────────────────────────────────

`play` draws the SHELL of the highway: the field, the road, the rungs, the
rails, the four strike ellipses with their letters, the header, the chips,
the readouts, the gauge and the key hints. It draws no notes. The note layer
owns note layout, and it asks this module where a note goes, so there is one
copy of the perspective and not two that drift:

    board(page)                     -> Rect   everything the road covers
    strike_y(page)                  -> int    the white line the notes land on
    project(page, lane, lanes, d)   -> (x, y, scale)
    note_box(page, lane, lanes, d)  -> Rect   one note's capsule at depth d
    capsule(surface, page, lane, lanes, d, fret, ...)  draws one, Stage's way
    strike_ring(surface, page, lane, lanes, lit=..., name=...)  relights one

`d` is depth: 0.0 is the strike line, 1.0 is the far edge of the board, and
a small negative number is the run-off below the strike line. Lanes run
left to right, lane 0 leftmost, which is the lowest string -- the order
page.tuning_label is already written in.

Two things the Page cannot tell a painter, so they are parameters instead of
guesses. Which lane is sounding is known to the note layer and not to the
Page, so `play` draws all four rings unlit and the note layer calls
`strike_ring(..., lit=True)` for the one that is. And the ring letters come
from page.tuning_label ("E A D G"); if a caller has the octaves as well
("E1 A1 D2 G2") the ring carries them small underneath, which is what the
drawing shows.

── What Stage does not show, and why ────────────────────────────────────

The drawing's two left readouts are SCORE and ACCURACY, its ring is a x4
multiplier and the line under it is a 12-note streak. All four are gone.
The low E string is still not detected reliably, and a score, a multiplier
and a streak are three ways of multiplying an unreliable measurement by
itself until it looks like an achievement. What is left in those four slots
is measured: notes found out of notes expected, notes that landed in the
timing window out of the notes found, how far through the piece the player is, and
how long is left.

And the timing readout is not green. Finding the note and placing it in the
beat are separate skills, so the timing number is drawn in feedback_early,
which is off the green-amber-red scale on purpose: a note found early is
still found, and nothing on this page is allowed to call it a miss.
"""

from __future__ import annotations

import math

import pygame
import pygame.gfxdraw

from pickhero.i18n import t
from pickhero.ui import design, library_setup, shapes
from pickhero.ui.colors import get_look
from pickhero.ui.page import Page, frac, split

# ── Geometry, all measured off the sheet ─────────────────────────────────
# The hairline between the columns sits at x 500.9 of 1280.
DIVIDE = 0.3913
# Below this the left column stops being the drawing and becomes a squeezed
# apology for it, so page.split drops it and the list takes the whole window.
# 340 rather than page.split's own 280: measured, the settings card needs 340
# to hold a label, a value, a note and a keycap on one row without any of
# them being trimmed, and a card of four trimmed rows helps nobody.
PANEL_FLOOR = 340
# The rows' left edge moves 10 px left over their 68 px height, and the play
# button moves 7.3 over 52. Both are 0.14; 0.145 is the mean of the two.
LEAN = 0.145

# The list: rows 68 tall on an 83 pitch, starting 88 down a 766 sheet. Kept
# as fractions of the live window rather than as those pixels, which is what
# puts seven whole rows in a 720 window instead of six and a sliced one.
ROW_H_F, ROW_PITCH_F = 0.0888, 0.1083
ROW_H_MIN, ROW_PITCH_MIN = 44, 54
LIST_TOP = 0.1149
LIST_BOTTOM = 0.858
CHIP_H = 32
# Three stars at size 6 on a gap of 5, which is what the drawing measures.
STAR_SIZE, STAR_GAP = 6, 5
STARS_W = 3 * STAR_SIZE * 2 + 2 * STAR_GAP

# What a tile says when there is nothing true to put in it. An em dash, not a
# zero: the player has not played it nought per cent well, there is no number yet.
EMPTY = "—"

# A piece counts as learned above this. The same number menu.py uses; it is
# repeated rather than imported because importing it would make the painter
# depend on the screen, which is the one direction the seam forbids.
PASSED_PERCENT = 85.0


# ── The wash ─────────────────────────────────────────────────────────────

def _wash(surface: pygame.Surface, width: int, height: int, look) -> None:
    """The violet bloom over the top of the page.

    Sampled off the drawing in a grid: it peaks at +(25, 10, 49) over the
    field just left of the divide, is down to a fifth of that by x 1000, and
    is gone by y 590. That is an ellipse centred above the divide, and the
    single reason the top rows look violet and the bottom ones do not.

    Built at 48x48 and scaled up rather than per-pixel at window size, and
    then cached: measured, this costs 6 ms once and 0.36 ms a frame to blit,
    against most of a second per build at window size. At this softness
    nothing survives the scaling that anyone could see.
    """
    size = (width, height)
    tint = look.menu_selected_bg

    def paint(layer: pygame.Surface) -> None:
        cell = 48
        small = pygame.Surface((cell, cell), pygame.SRCALPHA)
        cx, cy = 0.33, -0.05
        for gy in range(cell):
            fy = (gy + 0.5) / cell
            for gx in range(cell):
                fx = (gx + 0.5) / cell
                dx = (fx - cx) / 0.72
                dy = (fy - cy) / 0.95
                radius = (dx * dx + dy * dy) ** 0.5
                if radius >= 1.0:
                    continue
                alpha = int(175 * (1.0 - radius) ** 1.25)
                if alpha > 0:
                    small.set_at((gx, gy), design.with_alpha(tint, alpha))
        layer.blit(pygame.transform.smoothscale(small, size), (0, 0))

    surface.blit(design.sprite(("stage-wash", size, tint), size, paint), (0, 0))


# The rows' corners. shapes.skew_points gives the four corners square, which
# is right for the play button -- the drawing's button really is a hard
# parallelogram -- and wrong for the rows, whose corners are visibly turned.
ROW_RADIUS = 10


def _toward(p, q, distance: float):
    dx, dy = q[0] - p[0], q[1] - p[1]
    length = math.hypot(dx, dy) or 1.0
    step = min(distance, length / 2) / length
    return (p[0] + dx * step, p[1] + dy * step)


def _rounded(points: list, radius: int, steps: int = 4) -> list:
    """The same polygon with its corners turned.

    Each corner becomes a quadratic curve from one edge to the next through
    the corner itself, which is the standard construction and the only one
    that stays correct when the edges are not at right angles -- and on a
    leaning row none of them are.
    """
    out: list = []
    count = len(points)
    for i, corner in enumerate(points):
        before = _toward(corner, points[(i - 1) % count], radius)
        after = _toward(corner, points[(i + 1) % count], radius)
        out.append(before)
        for step in range(1, steps):
            # Named rather than called t: t is the catalogue lookup in every
            # other line of this file.
            at = step / steps
            out.append((
                (1 - at) ** 2 * before[0] + 2 * (1 - at) * at * corner[0]
                + at * at * after[0],
                (1 - at) ** 2 * before[1] + 2 * (1 - at) * at * corner[1]
                + at * at * after[1]))
        out.append(after)
    return out


def _row_points(rect: pygame.Rect) -> list:
    return _rounded(shapes.skew_points(rect, LEAN), ROW_RADIUS)


def _slab(size: tuple[int, int], colour, alpha: int) -> pygame.Surface:
    """A leaning translucent lift, cached. The list row and nothing else.

    Translucent on purpose: see the module docstring. ink_1 at alpha 8 over
    the field reproduces the drawing's row fill to the digit, and lets the
    wash through the way the drawing does.
    """
    def paint(layer: pygame.Surface) -> None:
        shapes.polygon(layer, design.with_alpha(colour, alpha),
                            _row_points(pygame.Rect((0, 0), size)))

    return design.sprite(("stage-slab", size, colour, alpha), size, paint)


# ── The page ─────────────────────────────────────────────────────────────

# The drawing's stars read straight off its own percentages: 100 and 97 get
# three, 88 gets two, 75 and 78 get one, 16 gets none. So they are accuracy
# bands, not a rating -- which matters, because Stage ships without a score
# and three gold stars for eleven TRIES, which is what this drew first, is
# praise for turning up rather than for playing it.
STAR_BANDS = (90.0, 80.0, 50.0)


def _star_band(accuracy: float | None) -> int:
    """How many of the three are lit. None and never-played light none."""
    if accuracy is None:
        return 0
    return sum(1 for edge in STAR_BANDS if accuracy >= edge)


def library(surface: pygame.Surface, page: Page) -> None:
    """Draw the whole library page for the Stage look."""
    look = get_look()
    width, height = page.width, page.height
    surface.fill(look.panel_right)
    left_w, _right_w = split(width, DIVIDE, minimum=PANEL_FLOOR)
    _wash(surface, width, height, look)
    if left_w:
        # The one piece of chrome in the drawing. No header band, no footer
        # band, no sidebar: one hairline and two columns of air.
        pygame.draw.line(surface, look.hairline, (left_w, 0), (left_w, height))
        _left_column(surface, page, look, width, height, left_w)
    _list_column(surface, page, look, width, height, left_w)
    library_setup.button(surface, page)
    if page.setup_open:
        library_setup.panel(surface, page)


# ── Left column ──────────────────────────────────────────────────────────

TILES_H = 76


def _left_column(surface: pygame.Surface, page: Page, look, width: int,
                 height: int, left_w: int) -> None:
    """The selected piece; settings live at the common top-right anchor."""
    pad = max(design.SPACE_4, frac(width, 0.0312))
    body = pygame.Rect(pad, 0, max(120, left_w - 2 * pad), height)
    brand_bottom = _brand(surface, page, look, body, 32)
    song = _selected(page)
    if song is None:
        return
    title = _named(song, page.selected)
    foot = height - max(design.SPACE_5, frac(height, 0.042))
    button = pygame.Rect(body.x, foot - 54, body.width, 54)
    tiles_h = TILES_H if height >= 480 else 0
    for size in (design.TEXT_2XL, design.TEXT_XL, design.TEXT_LG, design.TEXT_MD):
        lines = _wrap(title, size, body.width)
        if len(lines) <= 3:
            break
    block_h = len(lines) * (size + 1) + 48 + tiles_h
    y = max(brand_bottom + 40, button.top - block_h - 32)
    y = _title(surface, look, body, y, lines[:3], size)
    y = _meta(surface, look, body, y, song)
    if tiles_h:
        _tiles(surface, page, look, body, button.top - tiles_h - 20, tiles_h, song)
    _play_button(surface, page, look, button, True)


def _brand(surface: pygame.Surface, page: Page, look, body: pygame.Rect,
           y: int) -> int:
    size = design.TEXT_XL if body.width >= 300 else design.TEXT_LG
    mark = design.text(surface, "PICK", (body.x, y), look.ink_1, size,
                       weight="bold", italic=True)
    hero = design.text(surface, "HERO", (mark.right, y), look.hud_accent,
                       size, weight="bold", italic=True)
    label = page.instrument_short or page.instrument_name
    _fitted(surface, label, (body.x, hero.bottom + 10), look.ink_2,
            design.TEXT_SM, body.width)
    return hero.bottom + 34


def _selected(page: Page):
    """The piece the left column is about: the one under the cursor."""
    if not page.rows:
        return None
    index = max(0, min(len(page.rows) - 1, page.selected))
    return page.rows[index]


def _named(song, index: int) -> str:
    """A piece's title with the number the ordinal is already saying.

    Their exercises are files called "05 Leere Saiten rauf und runter", and the
    drawing puts that 05 in the big dim ordinal and the words beside it. So
    the prefix is dropped -- but only when it is the same number the ordinal
    shows. Under any other sort the two disagree, the file's own number is
    the only thing that still identifies the piece, and it stays.
    """
    title = song.display_title
    head = title.split(maxsplit=1)
    if len(head) == 2 and head[0].rstrip(".-").isdigit():
        try:
            if int(head[0].rstrip(".-")) == index + 1:
                return head[1]
        except ValueError:
            pass
    return title


def _wrap(title: str, size: int, max_w: int) -> list[str]:
    """The selected piece's title, uppercased and broken to the column.

    The drawing sets it at 36 over two lines. A narrower window is a
    narrower column, not a smaller drawing, so the caller steps the size
    down rather than letting one word run into the hairline -- and a word
    that still does not fit returns a line too many, which is how the caller
    is told to step down again.
    """
    words = title.upper().split()
    if not words:
        return []
    lines: list[str] = []
    current = ""
    for word in words:
        trial = f"{current} {word}".strip()
        if current and design.text_width(trial, size, weight="bold") > max_w:
            lines.append(current)
            current = word
        else:
            current = trial
    if current:
        lines.append(current)
    if max(design.text_width(line, size, weight="bold") for line in lines) \
            > max_w:
        return lines + [""] * 4
    return lines


def _title(surface: pygame.Surface, look, body: pygame.Rect, y: int,
           lines: list[str], size: int) -> int:
    for line in lines:
        design.text(surface, line, (body.x, y), look.ink_2, size,
                    weight="bold")
        y += size + 1
    return y


def _meta(surface: pygame.Surface, look, body: pygame.Rect, y: int,
          song) -> int:
    if song is None:
        return y
    summary = song.summary()
    if not summary:
        return y
    _fitted(surface, summary, (body.x, y + design.SPACE_3), look.ink_3,
            design.TEXT_SM, body.width)
    return y + design.SPACE_3 + design.TEXT_SM + 8


def _tiles(surface: pygame.Surface, page: Page, look, body: pygame.Rect,
           top: int, height: int, song) -> None:
    """Three facts about the piece, and not one of them a score.

    The drawing says BEST / STARS / NEXT, which is a score, a rating and a
    target to miss. The low E string is still not detected reliably, so all
    three would be numbers standing on a measurement that is not there.
    What is left after taking them out is still three things, and all three
    are true: how many notes I actually matched on their best run, how many
    times the player has had a go, and how long the piece is.
    """
    index = page.selected if page.rows else -1
    accuracy = page.scores.get(index)
    attempts, _date = page.history.get(index, (0, ""))
    facts = (
        (t("stage.tile_found"),
         t("menu.percent", percent=f"{accuracy:.0f}") if accuracy is not None
         else EMPTY),
        (t("stage.tile_tries"), str(attempts) if attempts else EMPTY),
        (t("stage.tile_bars"),
         str(song.bars) if song is not None and song.bars else EMPTY),
    )
    gap = design.SPACE_3
    tile_w = (body.width - gap * (len(facts) - 1)) // len(facts)
    for i, (label, value) in enumerate(facts):
        tile = pygame.Rect(body.x + i * (tile_w + gap), top, tile_w, height)
        shapes.rounded_rect(surface, look.panel_left, tile,
                         border_radius=design.RADIUS_MD)
        _fitted(surface, label, (tile.x + design.SPACE_3, tile.y + 18),
                look.ink_3, design.TEXT_XS, tile_w - design.SPACE_5,
                tracking=0.10, upper=True)
        _fitted(surface, value, (tile.x + design.SPACE_3, tile.y + 36),
                look.ink_1, design.TEXT_XL, tile_w - design.SPACE_5,
                weight="bold")


def _play_button(surface: pygame.Surface, page: Page, look,
                 rect: pygame.Rect, live: bool) -> None:
    """The one thing on the page that is a solid block of accent.

    With nothing in the library it is drawn dead and not registered: a
    glowing button that cannot do anything is worse than no button, because
    the first thing the player would do is press it.
    """
    if live:
        shapes.glow(surface, rect, look.hud_accent, spread=16, alpha=90,
                    lean=LEAN)
    fill = look.hud_accent if live else look.track_empty
    shapes.skewed(surface, rect, fill, lean=LEAN)
    label = t("stage.start")
    size = design.TEXT_XL
    while size > design.TEXT_SM and design.text_width(
            label, size, weight="bold", italic=True, tracking=0.06,
            upper=True) > rect.width - 60:
        size -= 2
    design.text(surface, label, (rect.centerx, rect.centery - size // 2 - 3),
                design.on_accent(fill) if live else look.ink_3, size,
                weight="bold", italic=True, tracking=0.06, align="center",
                upper=True)
    if live:
        page.mark("play", rect)


# ── Right column ─────────────────────────────────────────────────────────

def _list_column(surface: pygame.Surface, page: Page, look, width: int,
                 height: int, left_w: int) -> None:
    pad = max(design.SPACE_5, frac(width, 0.0226))
    list_rect = pygame.Rect(left_w + pad, 0,
                            width - left_w - pad - max(design.SPACE_6,
                                                       frac(width, 0.0273)),
                            height)
    chips_y = 72
    _chips(surface, page, look, list_rect, chips_y)

    # Chips retain their readable minimum height when the window shrinks.
    # Reserve that height before rows, including the selected row's glow.
    top = max(frac(height, LIST_TOP), chips_y + CHIP_H + 12)
    bottom = frac(height, LIST_BOTTOM)
    row_h = max(ROW_H_MIN, frac(height, ROW_H_F))
    pitch = max(ROW_PITCH_MIN, frac(height, ROW_PITCH_F))
    _rows(surface, page, look, list_rect, top, bottom, row_h, pitch)
    _hints(surface, look, list_rect, frac(height, 0.940))


def _chips(surface: pygame.Surface, page: Page, look, list_rect: pygame.Rect,
           y: int) -> None:
    """The filters, as chips, and the search affordance at the far right.

    The sidebar in the drawing is gone -- there is no sidebar on this page --
    so the filters have to fit one line. They are drawn in order until the
    line is full, and the active one is forced into the last place rather
    than dropped: a filter that is on and invisible is how a library looks
    empty for no reason anyone can see.
    """
    search = t("stage.search_active", text=page.search_text + "_") \
        if page.search_active else t("stage.search")
    search_w = design.text_width(search, design.TEXT_XS, tracking=0.14,
                                 upper=True)
    design.text(surface, search, (list_rect.right - 19, y + 9),
                look.hud_accent if page.search_active else look.ink_3,
                design.TEXT_XS, tracking=0.14, align="right", upper=True)
    page.mark("search", pygame.Rect(list_rect.right - 19 - search_w,
                                    y, search_w + 8, CHIP_H))

    room = list_rect.width - search_w - design.SPACE_12 - 5
    gap = design.SPACE_2 + 1

    def caption(label: str, count: int) -> str:
        return f"{label} {count}".upper() if count >= 0 else label.upper()

    def width_of(text: str) -> int:
        return design.text_width(text, design.TEXT_XS, weight="medium",
                                 tracking=0.06) + design.SPACE_8

    # The drawing shows all pieces and the three progress buckets, and that
    # is the right four: they answer "what have I not started" and "what is
    # unfinished", which is why someone opens a practice library at all.
    # The difficulty levels and the folders follow if the line has room, and
    # empty difficulty/folder entries are left out. Progress stays visible
    # at zero so the three states are stable controls in every look.
    def rank(entry):
        kind = entry[1][0] if isinstance(entry[1], tuple) else ""
        return {"all": 0, "progress": 1, "level": 2}.get(kind, 3)

    order = sorted(enumerate(page.filters), key=lambda e: rank(e[1]))
    laid: list[tuple[str, int, int]] = []
    used = 0
    active_at = -1
    for index, (label, value, count, active) in order:
        if count == 0 and not active and value[0] not in ("all", "progress"):
            continue
        text = caption(label, count)
        chip_w = width_of(text)
        if used + chip_w > room:
            continue
        laid.append((text, chip_w, index))
        used += chip_w + gap
        if active:
            active_at = index
    if active_at < 0:
        # The active filter did not fit. Swap it into the last slot rather
        # than dropping it: a page that does not say what it is filtered to
        # just looks like a library that lost half its pieces.
        for index, (label, _value, count, active) in order:
            if not active:
                continue
            text = caption(label, count)
            if laid:
                laid[-1] = (text, width_of(text), index)
            active_at = index
            break

    x = list_rect.x + 5
    for text, chip_w, index in laid:
        chip = pygame.Rect(x, y, chip_w, CHIP_H)
        if index == active_at:
            shapes.rounded_rect(surface, look.ink_1, chip,
                             border_radius=design.RADIUS_SM)
            ink = look.panel_right
        else:
            shapes.rounded_rect(surface, look.hairline_loud, chip, width=1,
                             border_radius=design.RADIUS_SM)
            ink = look.ink_2
        design.text(surface, text, (chip.centerx, chip.centery - 9), ink,
                    design.TEXT_XS, weight="medium", tracking=0.06,
                    align="center")
        kind, value = page.filters[index][1]
        page.mark(f"filter.{kind}.{value}", chip)
        x += chip_w + gap


def _rows(surface: pygame.Surface, page: Page, look, list_rect: pygame.Rect,
          top: int, bottom: int, row_h: int, pitch: int) -> None:
    page.clamp_scroll(row_h, bottom - top, row_pitch=pitch)
    if not page.rows:
        _fitted(surface, page.empty_title or t("menu.empty_search"),
                (list_rect.x + 5, top + 6), look.ink_2,
                design.TEXT_MD, list_rect.width - 10)
        if page.empty_hint:
            _fitted(surface, page.empty_hint, (list_rect.x + 5, top + 34),
                    look.ink_3, design.TEXT_XS, list_rect.width - 10)
        return

    pct_inset = max(18, frac(list_rect.width, 0.036))
    star_inset = max(96, frac(list_rect.width, 0.214))
    title_x = max(60, frac(list_rect.width, 0.127))
    ordinal_x = max(16, frac(list_rect.width, 0.035))

    clip = surface.get_clip()
    surface.set_clip(pygame.Rect(list_rect.x - 24, top - 4,
                                 list_rect.width + 48, bottom - top + 8))
    slab = None
    for index, song in enumerate(page.rows):
        y = top + index * pitch - page.scroll_px
        if y + row_h < top - 4 or y > bottom:
            continue
        rect = pygame.Rect(list_rect.x, y, list_rect.width, row_h)
        chosen = index == page.selected
        if slab is None:
            slab = _slab(rect.size, look.ink_1, 8)
        if chosen:
            shapes.glow(surface, rect, look.hud_accent, spread=14, alpha=85,
                        lean=LEAN)
        surface.blit(slab, rect.topleft)
        if chosen:
            # A second translucent pass rather than an opaque fill, so the
            # selected row keeps the same wash gradient as its neighbours.
            # Filled solid it is the one row on the page that is flat, and
            # it is the row the player is looking at.
            surface.blit(_slab(rect.size,
                               design.mix(look.select_fill, look.hud_accent,
                                          0.25), 90),
                         rect.topleft)
            shapes.polygon(surface, look.hud_accent, _row_points(rect),
                                width=1)

        design.text(surface, f"{index + 1:02d}",
                    (rect.x + ordinal_x, rect.centery - 18),
                    look.ink_4 if not chosen else look.ink_3,
                    design.TEXT_XL, weight="bold", italic=True)

        right_edge = rect.right - star_inset - STARS_W - design.SPACE_4
        _fitted(surface, _named(song, index).upper(),
                (rect.x + title_x, rect.centery - 21), look.ink_1,
                design.TEXT_MD, right_edge - rect.x - title_x,
                weight="bold", italic=True)
        meta = song.level_name
        if song.bpm:
            meta = f"{meta}  ·  {t('song.bpm', bpm=song.bpm)}"
        _fitted(surface, meta.upper(), (rect.x + title_x, rect.centery + 4),
                look.ink_3, design.TEXT_XS,
                right_edge - rect.x - title_x, italic=True)

        shapes.stars(surface, (rect.right - star_inset, rect.centery),
                     _star_band(page.scores.get(index)),
                     size=STAR_SIZE, gap=STAR_GAP, align="right",
                     on=look.feedback_close, off=look.signal_cold)

        accuracy = page.scores.get(index)
        if accuracy is None:
            design.text(surface, t("menu.progress_new"),
                        (rect.right - pct_inset, rect.centery - 10),
                        look.ink_3, design.TEXT_MD, weight="medium",
                        italic=True, align="right")
        else:
            design.text(surface, t("menu.percent", percent=f"{accuracy:.0f}"),
                        (rect.right - pct_inset, rect.centery - 10),
                        look.feedback_hit if accuracy >= PASSED_PERCENT
                        else look.feedback_close,
                        design.TEXT_MD, weight="bold", italic=True,
                        align="right")
        visible = rect.clip(surface.get_clip())
        if visible.height > 0:
            page.mark_row(visible, index)
    surface.set_clip(clip)


def _hints(surface: pygame.Surface, look, list_rect: pygame.Rect,
           y: int) -> None:
    """Plain tracked caps, which is what the drawing shows. Not buttons.

    Drawn here rather than through shapes.hints because that one sets the
    key in the mono face and in a louder ink, which is right for the two
    play screens and wrong here: this drawing's footer is one line of one
    face at one weight, and the mono key in the middle of it is the only
    thing on the page that would not be Chakra Petch.
    """
    pairs = ((t("key.arrows"), t("menu.hint_select")),
             (t("key.enter"), t("menu.play")),
             (t("key.w"), t("menu.suggest")),
             (t("key.escape"), t("stage.hint_quit")))
    phrases = [(f"{key} {label}",
                design.text_width(f"{key} {label}", design.TEXT_XS,
                                  tracking=0.14, upper=True))
               for key, label in pairs]
    gap = design.SPACE_6
    room = list_rect.width - 12
    # Wrapped rather than trimmed. Two of these keys were invisible for weeks
    # on a narrow window while they kept working, and the fix was never to
    # draw fewer of them.
    rows: list[list[tuple[str, int]]] = [[]]
    used = 0
    for phrase in phrases:
        if rows[-1] and used + phrase[1] > room:
            rows.append([])
            used = 0
        rows[-1].append(phrase)
        used += phrase[1] + gap
    y -= (len(rows) - 1) * (design.TEXT_XS + 7)
    for row in rows:
        x = list_rect.x + 12
        for phrase, width in row:
            design.text(surface, phrase, (x, y), look.ink_3, design.TEXT_XS,
                        tracking=0.14, upper=True)
            x += width + gap
        y += design.TEXT_XS + 7


# ── One small helper ─────────────────────────────────────────────────────

def _fitted(surface: pygame.Surface, value: str, pos: tuple[int, int],
            colour, size: int, max_w: int, **kwargs) -> pygame.Rect:
    """Draw `value`, trimmed to `max_w` with an ellipsis if it will not fit.

    A German label is routinely half again as long as the English one it was
    measured against, so every value on this page that comes from data is
    drawn through here. Letting one run past its column is how the first
    attempt ended up with the tuning written across the keycap.
    """
    if max_w <= 0:
        return pygame.Rect(pos, (0, 0))
    # align tells text where to put the string, not how wide it is, and
    # text_width does not take it. Measuring with it raises, which is a
    # crash in the draw loop rather than a layout bug anyone would see.
    measure = {k: v for k, v in kwargs.items() if k != "align"}
    shown = design.elide(value, lambda s: design.text_width(s, size, **measure) <= max_w,
                         strip=False)
    return design.text(surface, shown, pos, colour, size, **kwargs)


# ══ The play page ════════════════════════════════════════════════════════
# The road, measured off scratchpad/skins/seite4.png and kept as fractions of
# the live window. The drawing is a 1280x766 sheet; every y below was read off
# it and divided by 766, every x by 1280.
#
# The projection is ordinary linear perspective about one vanishing point, and
# the point is a long way off the top of the page: sheet (642.9, -436.6). That
# is the single number that makes this road look like the drawing and not like
# a funnel. The two rails measured independently converge on it to within
# 0.2 px, so it is the drawing's own point rather than one chosen to look
# right.
VP_X, VP_Y = 0.5023, -0.5700
# The strike line, the far edge of the floor, and where the floor stops short
# of the bottom of the window so the footlights have somewhere to be.
# Leave a readable result area below the targets; depth zero still denotes
# the exact musical onset everywhere in the projection.
STRIKE_F = 0.8000
ROAD_TOP = 0.2285
ROAD_FOOT = 0.9752
# Half the road's width where it meets the strike line.
ROAD_HALF = 0.2040
# How much the road narrows between the strike line and its far edge. Derived
# from the three above rather than written down, because a hand-typed 0.55
# that disagrees with them bends the rails.
FAR = (ROAD_TOP - VP_Y) / (STRIKE_F - VP_Y)
# Rungs, evenly spaced in depth. Measured off the sheet in two lanes that
# carry no note, they sit at y 196 232 272 317 369 428 497 577 of a 766 high
# render -- eight of them, so the road is divided into nine steps of depth,
# which is what 1/scale falling by a constant 0.09 a rung means. This counts
# the rungs and not the gaps: with eight gaps the loop drew seven, every one
# of them nearer than the drawing's, by 15 px at the horizon and 72 at the
# strike line, and the eighth was simply absent.
RUNGS = 8
# Points of light a rung adds to the floor it crosses, nearest and farthest.
# Measured, not chosen: sampled in the two lanes that carry no note, the
# sheet's rungs lift the floor by 19 at the strike line falling to 6 at the
# horizon. They are light on a road, not lines over it, which is why they
# fade with distance -- and why a drawn line was 3x too strong at the horizon
# and 5x at the strike line, where the floor is brightest and a line is not.
# A beginner reading notes off this road was reading them against a ladder as
# loud as they were.
RUNG_NEAR, RUNG_FAR = 19.0, 6.0
# How far a lane floor, and the seam on its right, are carried toward the
# string's own colour. The seam goes on under the haze and not over it: the
# sheet's seams lift the floor by 5 at the horizon and 11 at the strike line,
# and that fade is one the haze is already doing to everything else on the
# road, so the seam gets it for nothing by being underneath.
LANE_TINT, LANE_SEAM = 0.15, 0.45
# And a bar line is this much of a beat line. The sheet has no accent at all,
# but the player counts bars off these and the Look ships a colour for them, so it
# gets the smallest step that is still a step. stage_bar_line at its own
# brightness is 1.8x a beat line, which on this page is a stripe.
BAR_LIFT = 1.35
# A note is this much of its lane wide, and this much of its own width tall.
NOTE_W, NOTE_H = 0.78, 0.26
# The ellipse: wide, not round. 45 by 31 against a 65 px half-lane on the
# sheet, so it is two thirds of the lane across and two thirds of that tall.
RING_W, RING_FLAT = 0.69, 0.689

# The type ladder, so a smaller window steps down it rather than drawing the
# 1280 layout at 1280 sizes inside 640 px.
LADDER = (design.TEXT_XS, design.TEXT_SM, design.TEXT_MD, design.TEXT_LG,
          design.TEXT_XL, design.TEXT_2XL, design.TEXT_3XL, design.TEXT_4XL)

# Where every block sits, as a fraction of the window height. Read off the
# sheet as the top of the capital letters, which is what the eye lines up;
# _cap turns that into the top of the font box.
META_Y, TITLE_Y = 0.0516, 0.0840
BAR_Y, BAR_H = 0.1344, 0.0078
POSITION_Y = 0.1535
CHIP_Y, CHIP_HF = 0.0505, 0.0363
READ_1, READ_2 = 0.3548, 0.4760
GAUGE_X, GAUGE_Y, GAUGE_R = 0.8898, 0.4359, 0.1087
CAPTION_Y, VERDICT_Y = 0.5779, 0.6352
HINT_Y, HINT_PITCH = 0.8773, 0.0305
# The gutter the header and the hints keep from the left edge, and the chips
# from the right. The sheet puts both at 39.5 of 1280.
GUTTER = 0.0309

# Of the notes the player found, this many inside the window and the beat is tight.
# Below it the beat is loose, which is a different thing from wrong -- see the
# module docstring.
TIGHT = 0.80


def _cap(y: int, size: int) -> int:
    """The font box top that puts the top of the capitals at `y`.

    design.text draws from the top of the box, and the box carries the
    ascender and the leading above the capitals. Measured across the whole
    ladder in Chakra Petch that gap is 0.30 of the size, within a pixel.
    """
    return int(y - round(size * 0.30))


def _middle(cy: int, size: int) -> int:
    """The font box top that centres the capitals on `cy`."""
    return int(cy - round(size * 0.65))


def _step(page: Page) -> int:
    """How many places down the type ladder this window goes.

    Fractions alone keep the composition, but they do not keep the type
    readable: the sheet's 56 px readout at 640 px wide is a readout and a
    half of the column it stands in.
    """
    if page.width >= 1180 and page.height >= 660:
        return 0
    if page.width >= 880 and page.height >= 560:
        return 1
    return 2


def _size(base: int, down: int) -> int:
    """`base`, moved `down` places down the ladder."""
    try:
        at = LADDER.index(base)
    except ValueError:
        return base
    return LADDER[max(0, at - down)]


def _tracking(page: Page) -> float:
    """How far the small caps are letterspaced at this window size.

    The drawing tracks every label at +0.14 em, which is a third of a word of
    extra width on a German string. At 1280 that is what makes a label read
    as a label; at 640 it is what pushes the last key hint off the page. So
    it goes with the type: a luxury of a window that has the room for it.
    """
    return (0.14, 0.10, 0.06)[_step(page)]


def _clock(ms: float) -> str:
    """M:SS. Not in the catalogue, for the same reason note names are not."""
    total = max(0, int(ms // 1000))
    return f"{total // 60}:{total % 60:02d}"


# ── The geometry the note layer asks for ─────────────────────────────────

def _half(page: Page) -> float:
    """Half the road's width at the strike line, in pixels."""
    return page.width * ROAD_HALF


def strike_y(page: Page) -> int:
    """The white line. A note at depth 0 lands here."""
    return int(round(page.height * STRIKE_F))


def project(page: Page, lane: int, lanes: int,
            depth: float) -> tuple[int, int, float]:
    """Where lane `lane` is at `depth`, and how big the world is there.

    depth 0.0 is the strike line and 1.0 the far edge of the floor. Returns
    (x, y, scale), with scale 1.0 at the strike line.

    One function, so the floors, the rungs, the rails, the rings and the
    notes cannot disagree about where the road is. The 1/z is what turns
    equal steps of time into shrinking steps on screen; without it the notes
    appear to accelerate into the player.
    """
    lanes = max(1, lanes)
    depth = max(-0.45, min(1.0, depth))
    scale = 1.0 / (1.0 + (1.0 / FAR - 1.0) * depth)
    vx, vy = page.width * VP_X, page.height * VP_Y
    y = vy + (strike_y(page) - vy) * scale
    offset = (lane + 0.5) / lanes * 2.0 - 1.0
    return int(round(vx + offset * _half(page) * scale)), int(round(y)), scale


def _depth_at(page: Page, y: float) -> float:
    """The depth whose row is `y`. project's y, run backwards."""
    vy = page.height * VP_Y
    span = strike_y(page) - vy
    scale = (y - vy) / span if span else 1.0
    if scale <= 0:
        return 1.0
    return (1.0 / scale - 1.0) / (1.0 / FAR - 1.0)


def board(page: Page) -> pygame.Rect:
    """Everything the road covers, run-off included.

    The widest point is the foot and not the strike line, so a caller that
    clips to this rect does not shave the near corners off.
    """
    foot = _depth_at(page, page.height * ROAD_FOOT)
    wide = _half(page) / (1.0 + (1.0 / FAR - 1.0) * foot)
    vx = page.width * VP_X
    top = int(round(page.height * ROAD_TOP))
    return pygame.Rect(int(round(vx - wide)), top, int(round(wide * 2)),
                       int(round(page.height * ROAD_FOOT)) - top)


def note_box(page: Page, lane: int, lanes: int, depth: float) -> pygame.Rect:
    """The capsule a note fills at `depth`, centred on its lane.

    Width is 0.78 of the lane and height 0.26 of the width, both measured off
    the sheet across seven notes from the horizon down to the strike line.
    """
    x, y, scale = project(page, lane, lanes, depth)
    width = max(10, int(round(_half(page) * 2 / max(1, lanes) * scale
                              * NOTE_W)))
    height = max(6, int(round(width * NOTE_H)))
    return pygame.Rect(x - width // 2, y - height // 2, width, height)


def capsule(surface: pygame.Surface, page: Page, lane: int, lanes: int,
            depth: float, fret: int | str = "", *,
            colour: tuple[int, int, int] | None = None,
            ring: tuple[int, int, int] | None = None,
            halo: bool = True, ring_width: int | None = None,
            rect: pygame.Rect | None = None) -> pygame.Rect:
    """One note, drawn the Stage way: a glowing capsule with a fret digit.

    Here rather than in the note layer because the shape of a note is part of
    what a look is -- Studio's slab, Hi-fi's disc and this are three drawings
    and not one drawing in three palettes. `ring` carries the verdict the way
    the rest of the app does: the fill stays the string's colour, so the note
    never stops saying which string to play it on.
    """
    look = get_look()
    rect = rect if rect is not None else note_box(page, lane, lanes, depth)
    fill = (colour if colour is not None
            else design.string_colour(_string_of(lane, lanes)))
    radius = max(1, rect.height // 2)
    if halo:
        # Measured off the drawing: nine pixels clear of a nineteen pixel
        # capsule the page is back to its floor colour, so the halo is half
        # the note's height and bright, not twice it and soft.
        _bloom(surface, rect, fill, spread=max(4, int(rect.height * 0.55)),
               alpha=200)
    shapes.rounded_rect(surface, fill, rect, border_radius=radius)
    shapes.rounded_rect(surface, ring if ring is not None else look.ink_1, rect,
                     width=(ring_width if ring_width is not None
                            else max(1, round(rect.height * 0.065))), border_radius=radius)
    label = str(fret)
    if label and rect.height >= 11:
        size = _size(design.TEXT_XS, 0 if rect.height >= 17 else 1)
        design.text(surface, label, (rect.centerx, _middle(rect.centery, size)),
                    design.on_accent(fill), size, weight="bold", align="center")
    return rect


def _strike_radii(page: Page, lanes: int) -> tuple[int, int]:
    rx = max(9, int(round(_half(page) / max(1, lanes) * RING_W)))
    # A wide, shallow window still needs space beneath the pad for its verdict.
    ry = max(6, min(round(rx * RING_FLAT), round(page.height * .055)))
    return rx, ry


def strike_ring(surface: pygame.Surface, page: Page, lane: int, lanes: int, *,
                lit: bool = False, name: str = "",
                colour: tuple[int, int, int] | None = None) -> pygame.Rect:
    """One landing ellipse. A wide oval, never a circle.

    `lit` is the lane that is sounding, which the note layer knows and the
    Page does not, so it is a parameter and not a guess. Lit, the ellipse
    fills solid in the string's colour and blooms; unlit it is an outline
    over a dark well, which is what keeps one ink readable on four hues.
    """
    look = get_look()
    x, y, _scale = project(page, lane, lanes, 0.0)
    rx, ry = _strike_radii(page, lanes)
    rect = pygame.Rect(x - rx, y - ry, rx * 2, ry * 2)
    fill = (colour if colour is not None
            else design.string_colour(_string_of(lane, lanes)))
    if lit:
        _bloom(surface, rect, fill, spread=max(6, int(ry * 0.7)), alpha=150,
               ellipse=True)
        shapes.ellipse(surface, fill, rect)
        letter_ink = design.on_accent(fill)
        # The octave is set at 13 px, so it is body text and owes 4.5:1.
        # Lifted the four tenths of the way into the fill that the drawing
        # shows, the violet string measures 2.91 and four of the six strings
        # are under the line. At 0.15 all six clear it, worst case violet at
        # 4.76, and the hierarchy is carried by the size -- which is where it
        # was doing most of the work anyway.
        octave_ink = design.mix(letter_ink, fill, 0.15)
    else:
        shapes.ellipse(surface, look.select_fill, rect)
        letter_ink = look.ink_1
        octave_ink = fill
    shapes.ellipse(surface, fill, rect, width=max(2, ry // 9))

    head, tail = _split_name(name)
    labels: list[pygame.Rect] = []
    down = _step(page)
    if head and tail and ry >= 22:
        big = _size(design.TEXT_LG, down)
        small = _size(design.TEXT_XS, down)
        labels.append(design.text(
            surface, head, (rect.centerx, _middle(y - ry // 4, big)),
            letter_ink, big, weight="bold", italic=True, align="center"))
        labels.append(design.text(
            surface, name, (rect.centerx, _middle(y + ry // 2, small)),
            octave_ink, small, italic=True, align="center"))
    elif head:
        big = _size(design.TEXT_LG if ry >= 18 else design.TEXT_SM, down)
        labels.append(design.text(
            surface, head, (rect.centerx, _middle(y, big)),
            letter_ink, big, weight="bold", italic=True, align="center"))
    _strike_guides(surface, page, rect, labels)
    return rect


def _strike_guides(surface: pygame.Surface, page: Page, rect: pygame.Rect,
                   labels: list[pygame.Rect]) -> None:
    """Keep the musical target visible across both idle and lit landing pads.

    The oval's upper edge is reached long before the musical onset. Hiding
    the white strike line behind the whole pad made that edge the strongest
    apparent target. Continue the actual line through the pad, leaving the
    string name untouched. A dark keyline keeps it distinct on bright fills.
    """
    look = get_look()
    y = strike_y(page)
    width = max(2, frac(page.height, 0.0049))
    segments = [(rect.left, rect.right - 1)]
    band = pygame.Rect(rect.left, y - width, rect.width, width * 2 + 1)
    gap = max(2, min(4, rect.height // 12))
    for label in labels:
        if not band.colliderect(label):
            continue
        protected = label.inflate(gap * 2, 0)
        remaining = []
        for left, right in segments:
            if protected.right <= left or protected.left > right:
                remaining.append((left, right))
                continue
            if left < protected.left:
                remaining.append((left, protected.left - 1))
            if right >= protected.right:
                remaining.append((protected.right, right))
        segments = remaining
    for left, right in segments:
        pygame.draw.line(surface, look.stage_bg, (left, y), (right, y), width + 2)
        pygame.draw.line(surface, look.hit_zone, (left, y), (right, y), width)


def strike_feedback(surface: pygame.Surface, page: Page, lane: int, lanes: int,
                    label: str, symbol: str, colour: tuple[int, int, int],
                    strength: float = 1.0, *, timing_label: str = "",
                    timing_direction: str = "") -> pygame.Rect:
    """A stationary verdict below the pad, clear of the approaching notes."""
    look = get_look()
    x, y, _ = project(page, lane, lanes, 0)
    half_lane = _half(page) / max(1, lanes)
    _, ring_ry = _strike_radii(page, lanes)
    width = max(14, round(half_lane * 2 * NOTE_W))
    height = max(16, min(28, round(page.height * .036)))
    rect = pygame.Rect(0, 0, width, height)
    rect.midtop = (x, y + ring_ry + max(8, round(page.height * .018)))
    timing_face = design.reading_font(11)
    if timing_label:
        # Keep the secondary timing line inside even a wide, shallow window.
        needed = rect.bottom + timing_face.get_linesize() + 4 - page.height
        rect.y -= min(max(0, needed), max(0, rect.top - y - ring_ry - 2))
    ink = design.mix(look.stage_bg, colour, max(0.0, min(1.0, strength)))
    fill = design.mix(look.stage_bg, ink, .16)
    if symbol == "hit":
        _bloom(surface, rect, colour, spread=8, alpha=round(100 * strength))
    shapes.rounded_rect(surface, fill, rect, border_radius=height // 2)
    shapes.rounded_rect(surface, ink, rect, width=2, border_radius=height // 2)
    face = design.reading_font(12)
    if face.size(label)[0] <= width - 14:
        image = face.render(label, True, ink)
        surface.blit(image, image.get_rect(center=rect.center))
    else:
        # Geometric symbols stay crisp and legible even on very narrow lanes.
        cx, cy = rect.center
        if symbol == "hit":
            points = [(cx - 4, cy), (cx - 1, cy + 3), (cx + 5, cy - 4)]
        elif symbol == "early":
            points = [(cx + 3, cy - 4), (cx - 3, cy), (cx + 3, cy + 4)]
        elif symbol == "late":
            points = [(cx - 3, cy - 4), (cx + 3, cy), (cx - 3, cy + 4)]
        elif symbol == "close":
            points = [(cx - 4, cy), (cx + 4, cy)]
        else:
            points = [(cx - 3, cy - 3), (cx + 3, cy + 3)]
            pygame.draw.aaline(surface, ink, (cx - 3, cy + 3), (cx + 3, cy - 3))
        pygame.draw.aalines(surface, ink, False, points)
    if timing_label:
        timing_ink = design.mix(look.stage_bg, look.feedback_early, strength)
        if timing_face.size(timing_label)[0] <= rect.width:
            image = timing_face.render(timing_label, True, timing_ink)
            surface.blit(image, (rect.centerx - image.get_width() // 2, rect.bottom + 2))
        else:
            cy = rect.bottom + 7
            direction = -1 if timing_direction == "early" else 1
            points = [(rect.centerx - direction * 3, cy - 3),
                      (rect.centerx + direction * 3, cy),
                      (rect.centerx - direction * 3, cy + 3)]
            pygame.draw.aalines(surface, timing_ink, False, points)
    return rect


def _split_name(name: str) -> tuple[str, str]:
    """E1 splits into E and 1; E splits into E and nothing."""
    name = (name or "").strip()
    if name and name[-1].isdigit():
        return name[:-1], name[-1]
    return name, ""


def _is_note(token: str) -> bool:
    """Does `token` look like a string name -- E, F#, Bb, E1?"""
    if not 1 <= len(token) <= 3 or token[0].upper() not in "ABCDEFG":
        return False
    return all(ch in "#b♯♭" or ch.isdigit() for ch in token[1:])


def _strings(page: Page) -> list[str]:
    """The string names left to right, lowest first.

    page.tuning_label is where they belong and menu.py already writes exactly
    this string. The play screen may not set it, though, and four unlabelled
    ovals is a worse page than four labelled ones -- so a subtitle that ends
    in a run of note names ("Bass-Ukulele - E A D G") is read as the tuning
    it plainly is. Guarded on both counts: every token has to look like a
    note, and there have to be at least two, so a piece called "Study in E"
    cannot turn into a one-string instrument.

    Empty is still a legal answer. The rings draw, they just carry no letter.
    """
    # The screen states this outright now. What follows it is the fallback
    # for a page that does not -- parsing the tuning back out of a label was
    # the only way to know before Page carried it, and it is still the only
    # way for a caller that builds a Page by hand.
    if page.string_labels:
        # Lowest string first, which is the order the road runs in.
        return [name for _number, _letter, name in
                sorted(page.string_labels, reverse=True)]
    names = (page.tuning_label or "").split()
    if 2 <= len(names) <= 8 and all(_is_note(n) for n in names):
        return names
    tail: list[str] = []
    for token in reversed((page.subtitle or "").split()):
        if not _is_note(token):
            break
        tail.append(token)
    tail.reverse()
    return tail if 2 <= len(tail) <= 8 else []


def _lanes(page: Page) -> int:
    """Four, unless the tuning says otherwise. The player plays a bass ukulele."""
    return len(_strings(page)) or 4


# ── The field, and the glow the road throws onto it ──────────────────────

def _bloom(surface: pygame.Surface, rect: pygame.Rect,
           colour: tuple[int, int, int], *, spread: int = 12,
           alpha: int = 110, ellipse: bool = False) -> None:
    """Smooth capsule/ellipse light, with bounded shape variations in cache."""
    grid = 4
    spread = max(4, (spread + 3) // 4 * 4)
    w = max(grid, (rect.width + grid - 1) // grid * grid)
    h = max(grid, (rect.height + grid - 1) // grid * grid)
    box = pygame.Rect(0, 0, w, h)
    box.center = rect.center
    shapes.glow(surface, box, colour, spread=spread, alpha=alpha,
                radius=h // 2, ellipse=ellipse)


def _field(surface: pygame.Surface, page: Page, look) -> None:
    """One uninterrupted field, lit by the road standing on it.

    Sampled across the drawing in a grid: the page is flat bg at the corners
    and at the top, lifts to +(14, 5, 27) just outside the rails two thirds
    of the way down, and is gone again 300 px away from them. That is a glow
    shaped like the road, not a gradient and not a band -- which is why this
    page has no chrome anywhere: the only thing dividing it is light.

    The last 2.5 % is the footlights: a wide horizontal bloom under the road
    where the floor stops, which is the one place the drawing is brighter
    than anything except the strike line.

    Built at 64x48 and scaled up, then cached. Measured, this costs 7 ms once
    and 0.4 ms a frame to blit; per-pixel at window size costs most of a
    second. At this softness nothing survives the scaling that can be seen.
    """
    size = (page.width, page.height)
    tint = look.menu_selected_bg

    def paint(layer: pygame.Surface) -> None:
        cells_x, cells_y = 64, 48
        small = pygame.Surface((cells_x, cells_y), pygame.SRCALPHA)
        for gy in range(cells_y):
            fy = (gy + 0.5) / cells_y
            # The road's half width here, in width fractions. Linear in y:
            # that is what a vanishing point means.
            scale = (fy - VP_Y) / (STRIKE_F - VP_Y)
            half = ROAD_HALF * max(0.0, scale)
            # Nothing above the road's far edge, strongest at the floor.
            down = max(0.0, min(1.0, (fy - ROAD_TOP) / 0.62)) ** 1.1
            foot = max(0.0, (fy - ROAD_FOOT) / max(1e-6, 1.0 - ROAD_FOOT))
            for gx in range(cells_x):
                fx = (gx + 0.5) / cells_x
                outside = max(0.0, abs(fx - VP_X) - half) / 0.20
                near = math.exp(-outside * outside) * down
                lamps = math.exp(-((fx - VP_X) / 0.22) ** 2) * foot
                alpha = int(min(255, 96 * near + 150 * lamps))
                if alpha > 0:
                    small.set_at((gx, gy), design.with_alpha(tint, alpha))
        layer.blit(pygame.transform.smoothscale(small, size), (0, 0))

    surface.blit(design.sprite(("stage-field", size, tint), size, paint), (0, 0))


# ── The road ─────────────────────────────────────────────────────────────

def _rail_x(page: Page, y: float, side: int) -> float:
    """Where the left (-1) or right (+1) rail crosses row `y`."""
    vy = page.height * VP_Y
    span = strike_y(page) - vy
    scale = (y - vy) / span if span else 1.0
    return page.width * VP_X + side * _half(page) * scale


def _lane_edges(page: Page, lane: int, lanes: int,
                depth: float) -> tuple[float, float]:
    _x, _y, scale = project(page, lane, lanes, depth)
    vx = page.width * VP_X
    span = _half(page) * scale
    left = vx + (lane / lanes * 2.0 - 1.0) * span
    right = vx + ((lane + 1) / lanes * 2.0 - 1.0) * span
    return left, right


def _road(surface: pygame.Surface, page: Page, look, lanes: int) -> None:
    """Four lane floors, the haze over them, the rungs and the rails.

    The floors are drawn live rather than cached: four polygons measure
    0.085 ms and the cached alpha blit that replaced them measured 0.270,
    which is the same answer the highway got to the same question.
    """
    foot = _depth_at(page, page.height * ROAD_FOOT)
    near_y = project(page, 0, lanes, foot)[1]
    far_y = project(page, 0, lanes, 1.0)[1]
    for lane in range(lanes):
        colour = design.string_colour(_string_of(lane, lanes))
        nl, nr = _lane_edges(page, lane, lanes, foot)
        fl, fr = _lane_edges(page, lane, lanes, 1.0)
        pygame.draw.polygon(surface, design.mix(look.stage_bg, colour,
                                                LANE_TINT),
                            [(nl, near_y), (nr, near_y), (fr, far_y),
                             (fl, far_y)])

    # The seams, each on its lane's right. Drawn in the lane's own colour and
    # not in a hairline: on a road the only thing telling you which lane you
    # are looking at is its hue, and a grey seam between two tints reads as a
    # gap between two roads.
    #
    # A loop of their own, after every floor is down. Drawn inside the loop
    # above, each seam was painted over by the very next lane's polygon,
    # whose left edge is exactly where the seam was -- so three of the four
    # were erased and the fourth survived only where rounding put the
    # polygon a pixel clear of it. Measured, the road had seams of +0.6 and
    # -5.0 points where the drawing has +5 at the horizon rising to +11 at
    # the strike line, which is to say it had none.
    for lane in range(lanes - 1):
        colour = design.string_colour(_string_of(lane, lanes))
        _nl, nr = _lane_edges(page, lane, lanes, foot)
        _fl, fr = _lane_edges(page, lane, lanes, 1.0)
        pygame.draw.line(surface, design.mix(look.stage_bg, colour, LANE_SEAM),
                         (nr, near_y), (fr, far_y), 1)

    surface.blit(_haze(page, lanes, look), board(page).topleft)

    # RUNGS rungs across RUNGS + 1 steps of depth, so none of them lands on
    # the far edge. A rung drawn exactly on the edge of the floor is not a
    # beat, it is a lid: it closes the road off instead of letting it run
    # away into the fog.
    #
    # Added to the floor rather than drawn over it. A line is one colour and
    # the floor under it is four, and brighter at the near end than at the
    # far one, so a drawn rung is loudest exactly where the drawing's is
    # faintest. An add of a fraction of the beat colour is the same light on
    # every lane and fades with distance by itself, which is what the sheet
    # shows and what a rung on a road physically is.
    rungs = ([((beat.timestamp_ms - page.elapsed_ms) / max(1.0, page.visible_window_ms),
               beat.accent) for beat in page.timeline_beats]
             if page.timeline_markers else
             [(step / (RUNGS + 1), step % 4 == 0) for step in range(1, RUNGS + 1)])
    runoff_depth = _depth_at(page, page.height * ROAD_FOOT)
    for depth, bar in rungs:
        # Notes now travel past the target; their beat line follows through
        # the same projection until it leaves the visible road.
        if not runoff_depth <= depth <= 1.0:
            continue
        _lx, y, scale = project(page, 0, lanes, depth)
        tint = look.stage_bar_line if bar else look.stage_beat_line
        lift = RUNG_FAR + (RUNG_NEAR - RUNG_FAR) * (1.0 - depth)
        if bar:
            lift *= BAR_LIFT
        # The Look's colour sets the hue and the sheet sets the light, so the
        # bar line reads as a step up and not as a different lamp.
        scaled = lift / max(1.0, sum(tint) / 3.0)
        half = _half(page) * scale
        left = int(round(page.width * VP_X - half))
        surface.fill(tuple(int(c * scaled) for c in tint),
                     pygame.Rect(left, y, int(round(half * 2)), 1),
                     special_flags=pygame.BLEND_RGB_ADD)

    rail = design.mix(look.stage_bg, look.hit_zone, 0.27)
    top = page.height * ROAD_TOP
    bottom = page.height * ROAD_FOOT
    for side in (-1, 1):
        pygame.draw.line(surface, rail, (_rail_x(page, top, side), top),
                         (_rail_x(page, bottom, side), bottom), 2)


def _string_of(lane: int, lanes: int) -> int:
    """Which string number lane `lane` is.

    Lane 0 is the lowest string, which is the highest number, so the colours
    run violet-sky-orange-magenta from left to right the way the drawing has
    them -- and the way a player looking down at the neck has them.
    """
    return lanes - lane


def _haze(page: Page, lanes: int, look) -> pygame.Surface:
    """Distance into the field at the top, floor light at the bottom.

    Two gradients in one cached surface, so the road costs one blit and not
    two. The far third of the floor has to fade out or the road reads as a
    trapezoid with a hard edge across it, and the near end has to lift or
    there is nothing to tell you which way the notes are coming from.

    Masked to the road itself and not to the rectangle it fits in. Both
    gradients are opaque where they start, so an unmasked one paints the
    board's own corners over the glow the field put there -- which drew the
    bounding box of the road as a visible rectangle in the sky.
    """
    area = board(page)
    size = area.size

    def paint(layer: pygame.Surface) -> None:
        height = max(1, size[1])
        for row in range(height):
            # Sampled off the drawing at seven depths in all four lanes: the
            # far quarter of the floor is within four points of the field,
            # and the near end is a violet lift over its own hue rather than
            # more of that hue -- which is why a stronger tint alone never
            # reproduced it and only made the middle of the road orange.
            fog = max(0.0, 1.0 - row / max(1.0, height * 0.46)) ** 1.6
            lift = (14 + 60 * min(1.0, row / max(1.0, height)) ** 1.8) / 255
            # Composited here, into one colour, rather than filled twice.
            # Surface.fill replaces the pixel, it does not blend, so the
            # second of two fills silently threw the first away -- which is
            # why the far end of the road kept its hue however hard the fog
            # was turned up.
            total = 1.0 - (1.0 - fog) * (1.0 - lift)
            if total <= 0.004:
                continue
            blend = tuple(
                int((look.bg[i] * fog * (1.0 - lift)
                     + look.menu_selected_bg[i] * lift) / total)
                for i in range(3))
            layer.fill(design.with_alpha(blend, int(255 * total)),
                       (0, row, size[0], 1))
        foot = _depth_at(page, page.height * ROAD_FOOT)
        near_l, _r = _lane_edges(page, 0, lanes, foot)
        _l, near_r = _lane_edges(page, lanes - 1, lanes, foot)
        far_l, _r2 = _lane_edges(page, 0, lanes, 1.0)
        _l2, far_r = _lane_edges(page, lanes - 1, lanes, 1.0)
        near_y = project(page, 0, lanes, foot)[1] - area.top
        far_y = project(page, 0, lanes, 1.0)[1] - area.top
        mask = pygame.Surface(size, pygame.SRCALPHA)
        pygame.draw.polygon(mask, (255, 255, 255, 255),
                            [(near_l - area.left, near_y),
                             (near_r - area.left, near_y),
                             (far_r - area.left, far_y),
                             (far_l - area.left, far_y)])
        layer.blit(mask, (0, 0), special_flags=pygame.BLEND_RGBA_MULT)

    return design.sprite(("stage-haze", size, lanes, look.bg,
                          look.menu_selected_bg), size, paint)


def _strike(surface: pygame.Surface, page: Page, look, lanes: int) -> None:
    """The white line, and the four ovals sitting on it.

    Each oval continues the line around its label, so the musical target
    stays visible even when a landing pad lights up.
    """
    y = strike_y(page)
    pygame.draw.line(surface, look.hit_zone,
                     (_rail_x(page, y, -1), y), (_rail_x(page, y, 1), y),
                     max(2, frac(page.height, 0.0049)))
    names = _strings(page)
    for lane in range(lanes):
        strike_ring(surface, page, lane, lanes,
                    name=names[lane] if lane < len(names) else "")


# ── The header ───────────────────────────────────────────────────────────

def _header(surface: pygame.Surface, page: Page, look, right: int) -> None:
    """What the player is playing, how far in the player is, and nothing framing it.

    No band, no rule, no card. The drawing's header is four left-aligned
    things in a column of air, and the only reason it reads as a header is
    that nothing else is up there.
    """
    down = _step(page)
    pad = max(design.SPACE_4, frac(page.width, GUTTER))
    room = max(80, right - pad - design.SPACE_6)

    meta = page.subtitle
    if page.bpm:
        meta = t("stage.play_meta", subtitle=page.subtitle, bpm=page.bpm)
    small = _size(design.TEXT_XS, down)
    track = _tracking(page)
    _fitted(surface, meta, (pad, _cap(frac(page.height, META_Y), small)),
            look.ink_3, small, room, tracking=track, upper=True)

    # Stepped down until it fits rather than trimmed. The title is the piece
    # the player is playing; two sizes smaller and whole beats the right size with
    # its last three letters eaten, which is what the chips used to do to it.
    big = _size(design.TEXT_XL, down)
    while big > design.TEXT_SM and design.text_width(
            page.title, big, weight="bold", italic=True, upper=True) > room:
        big = _size(big, 1)
    _fitted(surface, page.title, (pad, _cap(frac(page.height, TITLE_Y), big)),
            look.ink_1, big, room, weight="bold", italic=True, upper=True)

    bar_h = max(4, frac(page.height, BAR_H))
    trough = pygame.Rect(pad, frac(page.height, BAR_Y),
                         min(room, frac(page.width, 0.2329)), bar_h)
    shapes.rounded_rect(surface, look.track_empty, trough,
                     border_radius=bar_h // 2)
    done = page.elapsed_ms / page.total_ms if page.total_ms > 0 else 0.0
    filled = int(trough.width * max(0.0, min(1.0, done)))
    if filled > 0:
        shapes.rounded_rect(surface, look.hud_accent,
                         pygame.Rect(trough.x, trough.y, filled, bar_h),
                         border_radius=bar_h // 2)
    page.mark("progress", trough.inflate(0, design.SPACE_4))

    _fitted(surface,
            t("stage.play_position", bar=page.bar, bars=page.bars,
              elapsed=_clock(page.elapsed_ms), total=_clock(page.total_ms)),
            (pad, _cap(frac(page.height, POSITION_Y), small)), look.ink_3,
            small, room, tracking=track, upper=True)
    if getattr(page, "loop_status", ""):
        top = frac(page.height, POSITION_Y) + small + 10
        loop_room = min(room, int(_rail_x(page, top, -1)) - pad - 16)
        _fitted(surface, page.loop_status, (pad, _cap(top, small)),
                look.hud_accent, small, max(60, loop_room))
        label = t("practice.loop_clear")
        width = design.text_width(label, small) + 20
        button = pygame.Rect(pad, top + small + 8, width, 26)
        shapes.rounded_rect(surface, look.stage_bg, button, border_radius=5)
        shapes.rounded_rect(surface, look.hairline_loud, button, 1, border_radius=5)
        design.text(surface, label, (button.centerx, _middle(button.centery, small)),
                    look.ink_2, small, align="center")
        page.mark("loop_clear", button)


def _toggles(surface: pygame.Surface, page: Page, look) -> int:
    """The four toggles, outlined and never filled. Returns their left edge.

    Not _chips: the library page already has a function of that name for its
    filter chips, and a second def quietly replaced it -- which broke the
    library, not the play page, and in a file that still imported cleanly.

    Outlined because a filled chip on this page would be the fifth brightest
    thing on it and it is a setting, not the music. On and off differ in both
    the border and the ink, so they are still two states in a photograph and
    for anyone who cannot separate the cyan from the grey.
    """
    if not page.toggles:
        return page.width - max(design.SPACE_4, frac(page.width, GUTTER))
    down = _step(page)
    size = _size(design.TEXT_XS, down)
    pad = max(design.SPACE_4, frac(page.width, GUTTER))
    height = max(22, frac(page.height, CHIP_HF))
    top = frac(page.height, CHIP_Y)
    gap = design.SPACE_2 + 1
    inset = design.SPACE_4 if down == 0 else design.SPACE_3

    def lay(named: bool):
        out = []
        for label_key, key_key, on, action in page.toggles:
            label = (t("stage.chip", label=t(label_key), key=t(key_key))
                     if named else t(key_key))
            out.append((action, label,
                        design.text_width(label, size, weight="medium",
                                          tracking=0.08, upper=True)
                        + inset * 2, on))
        return out, (sum(w for _k, _l, w, _on in out)
                     + gap * (len(out) - 1))

    # The header keeps a floor of the window whatever the chips want. Without
    # it a 640 px window gave four chips three quarters of the top edge and
    # the piece the player is playing was drawn as "LEERE SAI...".
    room = page.width - pad * 2 - max(200, frac(page.width, 0.36))
    laid, total = lay(True)
    if total > room:
        # Not smaller chips and not fewer: the same four, saying the key
        # instead of the name. All four states stay on the page, which is the
        # only reason the chips are there -- a toggle you cannot see is one
        # the player will press twice.
        laid, total = lay(False)
    while len(laid) > 1 and total > room:
        total -= laid.pop(0)[2] + gap

    x = page.width - pad - total
    left = x
    for action, label, width, on in laid:
        chip = pygame.Rect(x, top, width, height)
        shapes.rounded_rect(surface, look.hud_accent if on else look.hairline_loud,
                         chip, width=1, border_radius=design.RADIUS_SM)
        # The border carries the colour and the ink carries the brightness,
        # which is what the drawing does: its live chips are near-white in a
        # cyan box, not cyan in one. Sampled, (232, 227, 241) against the
        # (106, 101, 115) of a dead one. Setting the label in hud_accent too
        # said the same thing twice and said it 7 points darker.
        design.text(surface, label, (chip.centerx, _middle(chip.centery, size)),
                    look.ink_1 if on else look.ink_3, size,
                    weight="medium", tracking=0.08, align="center", upper=True)
        # The action the switch performs, which the page hands over with it.
        # A name of this painter's own invention is a control that is drawn,
        # looks live, and does nothing when the player clicks it.
        page.mark(action, chip)
        x += width + gap
    return left


def _play_tools(surface: pygame.Surface, page: Page, look) -> int:
    """Setup and help sit below the switches, tempo one row below them."""
    size = max(11, _size(design.TEXT_XS, _step(page)))
    pad = max(design.SPACE_4, frac(page.width, GUTTER))
    top = frac(page.height, CHIP_Y) + max(22, frac(page.height, CHIP_HF)) + 8
    entries = [(action, label, design.text_width(label, size, weight="medium") + 20)
               for action, label in (
                   ("setup", f"{t('key.s')}  {t('practice.setup')}"),
                   ("help", f"{t('key.h')}  {t('play.help')}"))]
    x = page.width - pad - sum(w for _a, _l, w in entries) - 8
    left = x
    for action, label, width in entries:
        rect = pygame.Rect(x, top, width, 28)
        shapes.rounded_rect(surface, look.hairline_loud, rect, 1, border_radius=5)
        design.text(surface, label, (rect.centerx, _middle(rect.centery, size)),
                    look.ink_2, size, weight="medium", align="center")
        page.mark(action, rect)
        x = rect.right + 8
    top += 36
    caption = f"{t('hifi.tempo')}  {t('hud.percent', percent=page.tempo_percent)}"
    width = max(page.width - pad - left, design.text_width(caption, size) + 80)
    left = min(left, page.width - pad - width)
    minus = pygame.Rect(page.width - pad - width, top, 28, 28)
    plus = pygame.Rect(page.width - pad - 28, top, 28, 28)
    for action, rect, increase in (("tempo_down", minus, False),
                                   ("tempo_up", plus, True)):
        shapes.rounded_rect(surface, look.hairline_loud, rect, 1, border_radius=5)
        pygame.draw.line(surface, look.ink_2, (rect.centerx - 5, rect.centery),
                         (rect.centerx + 5, rect.centery), 2)
        if increase:
            pygame.draw.line(surface, look.ink_2, (rect.centerx, rect.centery - 5),
                             (rect.centerx, rect.centery + 5), 2)
        page.mark(action, rect)
    design.text(surface, caption, ((minus.right + plus.left) // 2,
                                   _middle(minus.centery, size)),
                look.ink_2, size, align="center")
    return left


def count_in(surface: pygame.Surface, page: Page) -> None:
    remaining = getattr(page, "count_in_remaining", 0)
    if remaining <= 0:
        return
    look = get_look()
    size = min(104, max(64, page.height // 8))
    centre = (frac(page.width, VP_X), frac(page.height, 0.52))
    radius = size // 2 + 30
    pygame.draw.circle(surface, look.stage_bg, centre, radius)
    pygame.draw.circle(surface, look.hairline_loud, centre, radius, 2)
    design.text(surface, str(remaining),
                (centre[0], _middle(centre[1], size)), look.ink_1, size,
                weight="bold", align="center")
    design.text(surface, t("play.count_in"),
                (centre[0], centre[1] + radius + 12), look.ink_1,
                design.TEXT_SM, weight="medium", align="center")
    progress = max(0.0, min(1.0, getattr(page, "count_in_progress", 0.0)))
    if progress > 0:
        box = pygame.Rect(centre[0] - radius, centre[1] - radius, radius * 2, radius * 2)
        pygame.draw.arc(surface, look.hud_accent, box,
                        math.pi / 2 - progress * math.tau, math.pi / 2, 3)


# ── The two readouts, and the one thing the drawing has no room for ──────

def _readouts(surface: pygame.Surface, page: Page, look) -> int:
    """Notes found, and of those how many landed in the beat.

    The drawing says SCORE and ACCURACY. Neither survives: see the module
    docstring. These two are counts of things that were actually measured,
    and the second one is drawn in feedback_early rather than in green,
    because the beat is a different skill from the note and a number that
    shares a colour with "you played the wrong note" will be read as one.
    """
    down = _step(page)
    pad = max(design.SPACE_4, frac(page.width, GUTTER))
    label_size = _size(design.TEXT_XS, down)
    pairs = (
        (READ_1, _size(design.TEXT_3XL, down), t("stage.read_found"),
         t("stage.ratio", done=page.found, total=page.expected), look.ink_1),
        # An em dash and not "0 / 0" before anything has been found. The player has
        # not played nought of their notes in time; there is nothing to say yet,
        # and a pair of zeroes on the screen before the first bar reads like a
        # verdict.
        (READ_2, _size(design.TEXT_2XL, down),
         (t("stage.read_in_time") + " · " + t("play.timing_unknown_short", count=page.timing_unknown))
         if page.timing_unknown else t("stage.read_in_time"),
         t("stage.ratio", done=page.in_time,
           total=page.timing_measured if page.timing_unknown else page.found)
         if (page.timing_measured if page.timing_unknown else page.found) else EMPTY, look.feedback_early),
    )
    bottom = 0
    for where, size, label, value, ink in pairs:
        cap_y = frac(page.height, where)
        room = int(_rail_x(page, cap_y + size, -1)) - pad - design.SPACE_6
        _fitted(surface, label, (pad, _cap(cap_y, label_size)), look.ink_3,
                label_size, room, tracking=_tracking(page) - 0.02,
                upper=True)
        value_y = cap_y + int(label_size * 0.70) + design.SPACE_3
        # Upright, although almost everything else on this look leans. The
        # drawing sets both of these numbers upright and it is right to: a
        # count the player checks in the middle of a bar is read, not admired, and
        # italic numerals are the one place the lean costs legibility. The
        # title and the one word under the gauge keep it.
        _fitted(surface, value, (pad, _cap(value_y, size)), ink, size,
                room, weight="bold")
        bottom = value_y + int(size * 0.70)

    # Not in the drawing, and it stays anyway. Everything else on this page
    # is useless if the microphone is not hearing them, and that is the one
    # fault the player actually hits -- so the level gets the quietest corner of the
    # column the drawing leaves empty, with no label and no number.
    top = bottom + design.SPACE_5
    width = min(frac(page.width, 0.082),
                int(_rail_x(page, top, -1)) - pad - design.SPACE_6)
    if width >= 48:
        bars = pygame.Rect(pad, top, width, max(10, frac(page.height, 0.026)))
        shapes.meter(surface, bars, page.signal, on=look.hud_accent,
                     off=look.signal_cold)
        return bars.bottom
    return top


# ── The gauge ────────────────────────────────────────────────────────────

def _gauge(surface: pygame.Surface, page: Page, look) -> None:
    """How far through the piece the player is, how long is left, and one word.

    The drawing puts a x4 multiplier here. A multiplier needs a score and a
    score needs detection this app does not have on the low string, so the
    ring carries the one number on the page that is certainly true: the
    clock. The word under it is the beat, in two states and in two colours
    that are not a ladder -- green when it is tight, feedback_early when it
    is loose. There is no red here and there is not going to be one.
    """
    down = _step(page)
    centre_y = frac(page.height, GAUGE_Y)
    radius = frac(page.height, GAUGE_R)
    centre_x = frac(page.width, GAUGE_X)
    pad = max(design.SPACE_4, frac(page.width, GUTTER))
    # Kept clear of the right rail and of the window edge, both of which move
    # at a size the drawing was never made at.
    radius = int(min(radius, (page.width - pad - centre_x) * 1.0,
                     (centre_x - _rail_x(page, centre_y, 1) - design.SPACE_6)))
    if radius < 22:
        return
    done = max(0.0, min(1.0, page.elapsed_ms / page.total_ms
                        if page.total_ms > 0 else 0.0))
    stroke = max(4, int(round(radius * 0.106)))
    pygame.draw.circle(surface, look.track_empty, (centre_x, centre_y),
                       radius, width=stroke)
    _arc(surface, (centre_x, centre_y), radius, stroke, done, look.hud_accent)
    value = _clock(max(0.0, page.total_ms - page.elapsed_ms))
    size = _size(design.TEXT_2XL, down)
    while size > design.TEXT_SM and design.text_width(
            value, size, weight="bold") > radius * 1.5:
        size = _size(size, 1)
    design.text(surface, value, (centre_x, _middle(centre_y, size)),
                look.ink_1, size, weight="bold", align="center")

    room = min(page.width - pad * 2, radius * 3)
    caption = _size(design.TEXT_MD, down)
    _fitted(surface, t("stage.ring_left"),
            (centre_x, _cap(frac(page.height, CAPTION_Y), caption)),
            look.ink_1, caption, room, weight="bold",
            tracking=_tracking(page) - 0.02, align="center", upper=True)

    word = _size(design.TEXT_2XL, down)
    if page.paused:
        # Pause outranks the beat. It is the only state on this page that is
        # about this second rather than about the whole run, and it takes the
        # one slot that is -- in ink_1, because a stopped clock is neither a
        # good thing nor a bad one.
        label, ink = t("play.pause"), look.ink_1
    elif page.found <= 0:
        return
    elif page.timing_unknown:
        label, ink = t("play.timing_unknown"), look.ink_2
        while word > design.TEXT_SM and design.text_width(
                label.upper(), word, weight="bold", italic=True) > room:
            word -= 1
    else:
        tight = page.in_time / page.found >= TIGHT
        label = t("stage.timing_tight") if tight else t("stage.timing_loose")
        ink = look.feedback_hit if tight else look.feedback_early
    _fitted(surface, label, (centre_x, _cap(frac(page.height, VERDICT_Y), word)),
            ink, word, room, weight="bold", italic=True, align="center",
            upper=True)


def _arc(surface: pygame.Surface, centre: tuple[int, int], radius: int,
         stroke: int, fraction: float,
         colour: tuple[int, int, int]) -> None:
    """The filled part of the gauge, clockwise from the top.

    Not shapes.ring_gauge, and this is the one place this file refuses a
    primitive it has. That one ends in pygame.draw.arc, which at a stroke
    this thick draws a stack of concentric circles and leaves white gaps
    between them -- fine on a thin rail and visibly broken on the largest
    round thing on the hot screen. An annulus polygon has no gaps.

    Drawn live rather than cached: the fraction changes every frame, so a
    cache keyed on it would mint a key a frame, and a hundred-odd points
    measures well under a tenth of a millisecond.
    """
    if fraction <= 0.0 or radius <= 0:
        return
    sweep = min(1.0, fraction) * math.tau
    steps = max(4, int(sweep / math.tau * 180))
    outer, inner = radius + stroke / 2.0, radius - stroke / 2.0
    cx, cy = centre
    front, back = [], []
    for step in range(steps + 1):
        angle = -math.pi / 2 + sweep * step / steps
        cos, sin = math.cos(angle), math.sin(angle)
        front.append((cx + outer * cos, cy + outer * sin))
        back.append((cx + inner * cos, cy + inner * sin))
    # The live gauge changes every frame. AA its contour directly rather
    # than evicting reusable controls from the supersampling cache.
    points = [(round(x), round(y)) for x, y in front + back[::-1]]
    pygame.gfxdraw.filled_polygon(surface, points, colour)
    pygame.gfxdraw.aapolygon(surface, points, colour)


# ── The footer ───────────────────────────────────────────────────────────

def _play_hints(surface: pygame.Surface, page: Page, look) -> None:
    """Three lines of plain tracked caps. Not buttons, and not a band.

    Drawn here rather than through shapes.hints for the same reason the
    library's are: that one sets the key in the mono face, and the mono key
    is the only thing on a Chakra Petch page that is not Chakra Petch.

    A narrow window drops lines rather than trimming them. A hint cut to
    "LEERTASTE PAUSE . POS1 VON..." tells them about a key the player cannot read
    the name of, which is worse than one fewer hint.
    """
    down = _step(page)
    size = _size(design.TEXT_XS, down)
    pad = max(design.SPACE_4, frac(page.width, GUTTER))
    track = _tracking(page)
    pitch = max(size + 7, frac(page.height, HINT_PITCH))
    # Clamped to the window as well as placed by the sheet. On a short window
    # the sheet's own row sits low enough that the descenders of the last line
    # fall off the bottom edge, which is a hint you can read three quarters of.
    last = min(frac(page.height, HINT_Y) + pitch * 2,
               page.height - size - design.SPACE_3)
    wanted = (t("stage.hint_transport"),
              t("stage.hint_tempo", percent=page.tempo_percent),
              t("stage.hint_exit"))

    def room_at(y: int) -> int:
        # The road leans away as it climbs, so a line has more room the
        # higher it sits. Asking at each line's own row rather than at the
        # tightest one is the difference between three hints and two on a
        # 900 px window.
        return int(_rail_x(page, y, -1)) - pad - design.SPACE_4

    # Laid out from the bottom up, because the block is anchored to the floor
    # and a hint that wraps has to push the others up rather than down into
    # the road. Wrapped and not dropped: on a 900 px window the one line that
    # did not fit was the one carrying the pause key.
    rows: list[tuple[str, int]] = []
    y = last
    for line in reversed(wanted):
        for piece in reversed(_wrap_caps(line, size, track, room_at(y))):
            rows.append((piece, y))
            y -= pitch
    for piece, row in rows:
        _fitted(surface, piece, (pad, _cap(row, size)), look.ink_3, size,
                room_at(row), tracking=track, upper=True)


def _wrap_caps(line: str, size: int, tracking: float,
               room: int) -> list[str]:
    """`line` broken on spaces to `room`, as the tracked caps it is drawn in.

    One word that still will not fit comes back on its own and is trimmed by
    the caller, which is the only case where a hint loses a letter.
    """
    words = line.upper().split()
    if not words:
        return []
    out: list[str] = []
    current = ""
    for word in words:
        trial = f"{current} {word}".strip()
        if current and design.text_width(trial, size,
                                         tracking=tracking) > room:
            out.append(current)
            current = word
        else:
            current = trial
    if current:
        out.append(current)
    return out


def _notice(surface: pygame.Surface, page: Page, look, top: int) -> None:
    """Anything the player must not miss, in the one column notes never cross.

    Three things decided where this goes, and the first two are the seam.
    play() draws the shell and the note layer draws over it afterwards, so
    anything on the road ends up under a note -- and the message that brings
    this slab out is "I cannot hear you", which is the worst sentence in the
    app to draw a note through. Second, this page has no bands: design.banner
    is a full width strip and a strip is the one shape the drawing refuses.

    Third, it belongs here anyway. The level meter two lines above it is the
    other half of the same sentence: the strip says I hear nothing and the
    meter shows that nothing arriving.
    """
    message = page.banner
    if not message:
        return
    down = _step(page)
    size = _size(design.TEXT_SM, max(0, down - 1))
    pad = max(design.SPACE_4, frac(page.width, GUTTER))
    room = int(_rail_x(page, top + design.SPACE_12, -1)) - pad - design.SPACE_6
    if room < 120:
        return
    inset = design.SPACE_4
    lines = _wrap_caps(message, size, 0.0, room - inset * 2)[:4]
    if not lines:
        return
    height = len(lines) * (size + 6) + design.SPACE_5
    slab = pygame.Rect(pad, top, room, height)
    shapes.glow(surface, slab, look.banner_bg, spread=14, alpha=80, lean=LEAN)
    shapes.skewed(surface, slab, look.banner_bg, lean=LEAN)
    y = slab.y + design.SPACE_3
    for line in lines:
        _fitted(surface, line, (slab.x + inset + int(height * LEAN), _cap(y, size)),
                look.banner_text, size, room - inset * 2, weight="medium")
        y += size + 6
    page.mark("banner", slab)


# ── The page ─────────────────────────────────────────────────────────────

def play(surface: pygame.Surface, page: Page) -> None:
    """Draw the whole play page for the Stage look, notes excepted.

    Order is the whole of it: one field, the light the road throws on it,
    the road, the line the notes land on, and only then the words. Every
    piece of chrome is drawn over the field with nothing behind it, which is
    what makes this page one surface instead of five boxes.
    """
    look = get_look()
    lanes = _lanes(page)
    surface.fill(look.bg)
    _field(surface, page, look)
    _road(surface, page, look, lanes)
    _strike(surface, page, look, lanes)
    right = _toggles(surface, page, look)
    right = min(right, _play_tools(surface, page, look))
    _header(surface, page, look, right)
    below = _readouts(surface, page, look)
    _gauge(surface, page, look)
    _play_hints(surface, page, look)
    _notice(surface, page, look, below + design.SPACE_6)


# ── Temporary harness: delete with the wiring ────────────────────────────
# menu.py builds the Page and calls the painter itself now, so this exists
# only so the look can be rendered to a file from a two-line script while
# the three painters are being written side by side.

def _demo_page(screen, surface: pygame.Surface) -> Page:
    """A Page from a live MenuScreen, for looking at this file's output."""
    builder = getattr(screen, "_page", None)
    if builder is not None:
        return builder(surface)
    width, height = surface.get_size()
    return Page(width=width, height=height)


def _demo(screen, surface: pygame.Surface) -> Page:
    page = _demo_page(screen, surface)
    library(surface, page)
    return page
