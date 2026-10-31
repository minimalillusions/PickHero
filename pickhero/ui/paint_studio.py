"""Studio draws the library: the calm dark one, set in Geist.

Everything here is measured off `scratchpad/skins/seite1.png`, a 1280x766
sheet, with a pygame probe rather than by eye -- the two earlier attempts at
this page were drawn from a description of the drawing and both missed, so
the numbers below are sampled pixel positions and the colours are the Look
fields those same pixels turned out to be.

The shape, in one paragraph. A full-height sidebar on the left, five points
darker than the field beside it, carrying the wordmark, the filters grouped
under tracked labels, and a quiet two-line foot. The field on the right opens
with its own header -- a search box and one status pill, inside the column
rather than in a band across the window -- then the piece the player was in the
middle of, then the list. The list rows are not cards: an ordinal, a title, a
meta line, a difficulty, a rail and a number, separated by a hairline and by
space. What makes the page read as expensive is how little of it is a shape.

Three things are deliberate and easy to undo by accident:

  Nothing is drawn in a colour that is not a Look field. Not one lightened
  background, not one module constant. The two places that need a value
  between two Look fields -- the hero's faint teal cast -- mix two Look
  fields, which is a different thing from guessing at the page.

  The hero's gradient and the wordmark's diamond go through design.sprite.
  Both are per-pixel work that does not change between frames, and building
  them live costs more than the rest of the page put together.

  Vertical placement centres on the glyphs, not on the text surface's top.
  The sheet gives the ink's centre line for every label, and a y read off the
  sheet and passed straight to design.text lands a face-dependent amount too
  low -- which is exactly how a layout stops matching when the look changes
  typeface.
"""

from __future__ import annotations

import pygame

from pickhero.i18n import t
from pickhero.ui import design, library_setup, shapes
from pickhero.ui.colors import get_look
from pickhero.ui.page import Page, split

# ── Geometry ─────────────────────────────────────────────────────────────
# Sheet coordinates, 1280 wide. The window is 1280x720 and resizable, so the
# horizontal numbers are offsets from the two panel edges and the vertical
# ones are anchored to the top or to the bottom; only the list band flexes.

SIDEBAR_FRACTION = 0.193        # 247 of 1280
# Below this the sidebar is dropped rather than squeezed. 190 keeps it at
# 1024 (198 px, which still holds "SCHWIERIGKEIT" and a count) and drops it
# at 900, where it would have to lose either the word or the number.
SIDEBAR_MIN = 190

GUTTER = 26                     # field edge to content, both sides
HEADER_RULE = 68                # the line under the search row
FOOTER_RULE = 56                # from the bottom
ROW_H = 56
HERO_H = 132

# Sidebar, from its own left edge.
SIDE_PAD = 26
SIDE_INSET = 16                 # the selected block's left and right air
ENTRY_PITCH = 37                # between two filter entries, ink centre to ink centre
LABEL_LEAD = 48                 # entry ink centre to the section label above the next group
LABEL_TRAIL = 33                # section label to the first entry under it
FIRST_ENTRY = 88                # ink centre of the first filter
WORDMARK = 32                   # ink centre of the wordmark row

# Row internals, from the row's top edge or from the field's right edge.
ROW_ORDINAL = 29                # right edge of the ordinal, from the content left
ROW_TEXT = 66                   # title and meta, from the content left
ROW_TITLE_MID = 22
ROW_META_MID = 40
RAIL_W, RAIL_H = 171, 5
RAIL_RIGHT = 93                 # rail's right edge, from the field's right
LEVEL_RIGHT = 373               # difficulty dot, from the field's right
LEVEL_RIGHT_TIGHT = 200         # ... once the rail has been dropped
SCORE_RIGHT = 6                 # the percentage, from the field's right

# What a row may drop, and when. Measured against the German strings, which
# are the long ones: "Schwierigkeit" and "noch nicht gespielt" both have to
# fit before the window is allowed to call itself wide.
NEEDS_RAIL = 760
NEEDS_LEVEL = 520

# The tracked small-caps labels. 12 px rather than TEXT_XS because the sheet
# says so: "DIFFICULTY" measures 68 px wide with a cap height of 8.8, which
# is 12 px letterspaced, not 13 px set solid. The difference is the whole
# character of the label -- at 13 px with no tracking it reads as small body
# text, which is what it was.
LABEL_SIZE = 12
LABEL_TRACK = 0.04
EYEBROW_TRACK = 0.06


def _mid(size: int, **kw) -> int:
    """How far above a centre line design.text has to start.

    design.text positions the text surface's top. Centring on the surface
    puts the ink's centre within half a pixel of the centre line for every
    bundled face, which is not true of ascent-based maths and is the whole
    reason this is one function rather than a literal per call site.
    """
    return design.font(size, **kw).get_height() // 2


def _ink(surface: pygame.Surface, value: str, pos: tuple[int, int],
         colour, size: int = design.TEXT_SM, *, align: str = "left",
         **kw) -> pygame.Rect:
    """design.text, but `pos` is where the ink's centre line goes."""
    font_kw = {k: v for k, v in kw.items()
               if k in ("bold", "mono", "face", "weight", "italic")}
    return design.text(surface, value, (pos[0], pos[1] - _mid(size, **font_kw)),
                       colour, size, align=align, **kw)


# Measuring a string costs about as much as drawing it, and the page asks
# the same questions of the same strings every frame: 58 measurements a frame
# before this cache, 21 after, which is 1.1 ms of a 4.9 ms frame. Bounded and
# cleared wholesale, because a resize drag produces a new limit per frame.
_FIT_LIMIT = 400
_fitted: dict[tuple, str] = {}


def _fit(value: str, limit: int, size: int, **kw) -> str:
    """`value`, shortened with an ellipsis until it fits in `limit`."""
    if limit <= 0:
        return ""
    # The look is in the key because it chooses the typeface, and the same
    # string is a different width in Geist and in Hanken.
    key = (get_look().key, value, limit, size, tuple(sorted(kw.items())))
    found = _fitted.get(key)
    if found is not None:
        return found
    shown = value
    if design.text_width(value, size, **kw) > limit:
        # Guess the cut from the average advance first; the loop then only
        # has to walk the last glyph or two instead of the whole title.
        per = max(1.0, design.text_width(value, size, **kw) / max(1, len(value)))
        shown = value[:max(0, int(limit / per))]
        while shown and design.text_width(shown + "…", size, **kw) > limit:
            shown = shown[:-1]
        shown = shown + "…" if shown else ""
    if len(_fitted) >= _FIT_LIMIT:
        _fitted.clear()
    _fitted[key] = shown
    return shown


def _dot(surface: pygame.Surface, centre: tuple[int, int], radius: int,
         colour) -> None:
    """A round dot, cached.

    pygame.draw.circle has no anti-aliasing, and at the four-pixel radius
    this page uses it draws a visible octagon -- which on a row of five
    difficulty dots is the one thing on the screen that looks drawn by a
    program. Supersampled four times and scaled down, once per colour.

    The big surface is filled with the dot's own colour at zero alpha before
    the circle goes on it. smoothscale averages all four channels, so over
    the default transparent black it averages the colour towards black as
    well as towards nothing -- and a dark fringe on a dark page subtracts
    exactly the pixels that were carrying the curve. Measured on the play
    page's three-pixel toggle dot: the corners landed at 29 against a
    background of 28, so the only part of it anyone could see was the solid
    four-by-four core, and a disc meant to be round read as a little square.
    Filled with the colour first, those corners land at 74 and it is a dot.
    """
    size = radius * 2 + 2

    def paint(layer: pygame.Surface) -> None:
        big = pygame.Surface((size * 4, size * 4), pygame.SRCALPHA)
        big.fill((*colour, 0))
        pygame.draw.circle(big, colour, (size * 2, size * 2), radius * 4)
        layer.blit(pygame.transform.smoothscale(big, (size, size)), (0, 0))

    sprite = design.sprite(("studio.dot", radius, colour), (size, size), paint)
    surface.blit(sprite, sprite.get_rect(center=centre))


def _untitled(song, number: int) -> str:
    """The title, without the ordinal the row is about to draw beside it.

    Their pieces are files named "05 Leere Saiten rauf und runter", so the
    number is already in the title and the drawing's ordinal column would
    print it a second time. Only a leading number that IS this row's ordinal
    is taken off, so a piece actually called "1984 Nocturne" keeps its name
    and a filtered list, where the two disagree, keeps the file's own number
    rather than quietly swapping it for a position.
    """
    title = song.display_title
    head, sep, tail = title.partition(" ")
    if sep and tail and head.isdigit() and int(head) == number:
        return tail
    return title


# ── The page ─────────────────────────────────────────────────────────────

def library(surface: pygame.Surface, page: Page) -> None:
    """The browser, Studio's way."""
    look = get_look()
    width, height = page.width, page.height
    surface.fill(look.panel_right)

    side_w, _field_w = split(width, SIDEBAR_FRACTION, SIDEBAR_MIN)
    if side_w:
        _sidebar(surface, page, side_w, height)
        pygame.draw.line(surface, look.panel_rule,
                         (side_w - 1, 0), (side_w - 1, height), 1)

    left = side_w + GUTTER
    right = width - GUTTER - 1
    if right - left < 180:          # a window too narrow for two gutters
        left, right = side_w + 8, width - 9

    _header(surface, page, left, right, side_w, width)
    _footer(surface, left, right, side_w, width, height)

    base = HEADER_RULE
    floor = height - FOOTER_RULE
    if page.carry is not None and floor - HEADER_RULE >= HERO_H + 46 + ROW_H * 2:
        base = _hero(surface, page, left, right)
    _list(surface, page, left, right, base, floor)

    # Last, so it is on top of what it covers, and so the covering is honest:
    # _setup takes the things under the card back out of the hit map.
    if page.setup_open:
        library_setup.panel(surface, page)


# ── Sidebar ──────────────────────────────────────────────────────────────

def _sidebar(surface: pygame.Surface, page: Page, side_w: int,
             height: int) -> None:
    look = get_look()
    pygame.draw.rect(surface, look.panel_left, (0, 0, side_w, height))

    pad = SIDE_PAD if side_w >= 220 else 18
    inset = SIDE_INSET if side_w >= 220 else 10
    count_x = side_w - pad - 1

    # The wordmark. The diamond is the app's only piece of pure decoration,
    # so it is the one place the accent appears without naming a control.
    _diamond(surface, (pad + 9, WORDMARK), 17, look.hud_accent)
    _ink(surface, "PickHero", (pad + 28, WORDMARK), look.ink_1, 19,
         weight="semibold")

    hair_y = height - 86
    y = FIRST_ENTRY
    for kind, label, value, count, active in _groups(page):
        if kind == "heading":
            y += LABEL_LEAD - ENTRY_PITCH
            # A section label with nothing under it is worse than no label:
            # it reads as a group that failed to load.
            if y + LABEL_TRAIL > hair_y - 16:
                break
            _ink(surface, label, (pad, y), look.ink_4, LABEL_SIZE,
                 weight="medium", tracking=LABEL_TRACK, upper=True)
            y += LABEL_TRAIL
            continue
        if y > hair_y - 16:
            break
        rect = pygame.Rect(inset, y - 18, side_w - inset * 2, 36)
        if active:
            pygame.draw.rect(surface, look.select_fill, rect, border_radius=10)
        text_x = pad
        if kind == "level":
            # One dot, not five bars. The bars spent fifty pixels
            # repeating what the word beside them already said.
            _dot(surface, (pad + 4, y), 4,
                 design.difficulty_colour(int(value)))
            text_x = pad + 20
        number = str(count)
        gap = design.text_width(number, design.TEXT_SM) + 14
        _ink(surface, _fit(label, count_x - text_x - gap, design.TEXT_SM),
             (text_x, y), look.ink_1 if active else look.ink_2, design.TEXT_SM)
        _ink(surface, number, (count_x, y), look.ink_4, design.TEXT_SM,
             align="right")
        page.mark(f"filter.{kind}.{value}", rect)
        y += ENTRY_PITCH

    # The foot: what the library holds, and the one key that leaves.
    pygame.draw.line(surface, look.hairline, (inset, hair_y),
                     (side_w - inset, hair_y), 1)
    pieces, played = page.counts
    _ink(surface, _fit(t("menu.summary", count=pieces, played=played),
                       side_w - pad * 2, design.TEXT_XS),
         (pad, height - 66), look.ink_3, design.TEXT_XS)
    _ink(surface, _escape_hint(page), (pad, height - 45), look.ink_3,
         design.TEXT_XS)


def _escape_hint(page: Page) -> str:
    """What Esc would undo right now.

    Esc used to quit the app while a filter was narrowing ten pieces down to
    four and the line above it still counted all ten. Every rung is on the
    Page, so the line can name the live one instead of guessing.
    """
    if page.setup_open:
        return t("menu.esc_setup")
    if page.search_active or page.search_text:
        return t("menu.esc_search")
    if any(active for _, value, _, active in page.filters
           if value != ("all", None)):
        return t("menu.esc_filter")
    return t("menu.quit_hint")


def _groups(page: Page):
    """The sidebar's entries, regrouped into the drawing's four blocks.

    `page.filters` arrives flat, with its headings already dropped, so the
    grouping is rebuilt here from each filter's kind. Yields either
    ("heading", label, None, 0, False) or (kind, label, value, count, active).

    One entry moves. The drawing puts "Weiter" at the top beside the two
    unconditional filters rather than inside FORTSCHRITT, and that is right:
    the piece the player started and has not finished is the reason the player opened the
    app at all, and it does not belong three section labels down. It is the
    same filter either way, so it is hoisted out of its group rather than
    copied into a second place that would then have to agree with the first.
    """
    headings = {"level": "menu.heading_difficulty",
                "progress": "menu.heading_progress",
                "folder": "menu.heading_folder"}
    hoisted = [entry for entry in page.filters
               if entry[1] == ("progress", "wip")]
    for label, filter_value, count, active in page.filters:
        if filter_value == ("progress", "wip"):
            continue
        kind, value = (filter_value if isinstance(filter_value, tuple)
                       else (str(filter_value), None))
        if kind == "all":
            yield "all", label, value, count, active
            for _, _, wip_count, wip_active in hoisted:
                yield ("progress", t("menu.continue_filter"), "wip",
                       wip_count, wip_active)
            hoisted = []
        elif kind in headings:
            if headings[kind]:
                yield "heading", t(headings[kind]), None, 0, False
                headings[kind] = ""
            yield kind, label, value, count, active


def _diamond(surface: pygame.Surface, centre: tuple[int, int], size: int,
             colour) -> None:
    """A rounded square stood on its corner, cached.

    pygame has no rotated rounded rect, and rotating one costs about as much
    as the rest of the sidebar, so it is built once per colour and size.
    """
    span = int(size * 1.4143) + 2

    def paint(layer: pygame.Surface) -> None:
        flat = pygame.Surface((size, size), pygame.SRCALPHA)
        pygame.draw.rect(flat, colour, flat.get_rect(),
                         border_radius=max(2, size // 4))
        spun = pygame.transform.rotate(flat, 45)
        layer.blit(spun, spun.get_rect(center=layer.get_rect().center))

    sprite = design.sprite(("studio.diamond", size, colour), (span, span), paint)
    surface.blit(sprite, sprite.get_rect(center=centre))


# ── Header ───────────────────────────────────────────────────────────────

def _header(surface: pygame.Surface, page: Page, left: int, right: int,
            side_w: int, width: int) -> None:
    look = get_look()
    pygame.draw.line(surface, look.hairline, (side_w, HEADER_RULE),
                     (width, HEADER_RULE), 1)

    setup = library_setup.button_rect(page.width)
    box_w = min(348, setup.left - left - 16)
    if box_w >= 96:
        box = pygame.Rect(left, 15, box_w, 38)
        pygame.draw.rect(surface, look.select_fill, box, border_radius=10)
        if page.search_active:
            pygame.draw.rect(surface, look.hud_accent, box, width=1,
                             border_radius=10)
        page.mark("search", box)

        cap_w = 22
        cap = pygame.Rect(box.right - 12 - cap_w, box.centery - 11, cap_w, 22)
        room = cap.left - box.left - 26
        if page.search_active or page.search_text:
            _ink(surface, _fit(page.search_text + "_", room, design.TEXT_SM),
                 (box.left + 14, box.centery), look.ink_1, design.TEXT_SM)
        else:
            _ink(surface, _fit(t("menu.search_placeholder"), room,
                               design.TEXT_SM),
                 (box.left + 14, box.centery), look.ink_3, design.TEXT_SM)
            _keycap(surface, cap, t("key.f"), outline=True)

    available = setup.left - left - max(0, box_w) - 32
    if available >= 110:
        _ink(surface, _fit(page.instrument_short or page.instrument_name, available, design.TEXT_SM),
             (setup.left - 16, setup.centery), get_look().ink_2, design.TEXT_SM, align="right")
    library_setup.button(surface, page)


def _keycap(surface: pygame.Surface, rect: pygame.Rect, letter: str, *,
            outline: bool = False) -> None:
    """A key, drawn as a key. Outlined in the search box, filled in the pill.

    The filled one sits on `select_fill`, and `hairline` on `select_fill` is
    five points of separation where the sheet has thirteen -- the key read as
    a smudge rather than as something to press. Half a step past `hairline`
    lands on the sheet's own value, and it is still two Look fields rather
    than a number: the keycap is the one surface on the page that has to be
    lighter than the control it sits in.
    """
    look = get_look()
    if outline:
        pygame.draw.rect(surface, look.hairline_loud, rect, width=1,
                         border_radius=design.RADIUS_SM)
    else:
        pygame.draw.rect(surface,
                         design.mix(look.hairline, look.hairline_loud, 0.4),
                         rect, border_radius=design.RADIUS_SM + 2)
    _ink(surface, letter, (rect.centerx, rect.centery), look.ink_3,
         design.TEXT_XS, mono=True, align="center")


def _hero(surface: pygame.Surface, page: Page, left: int, right: int) -> int:
    """Pick up where the player left off, before the list the player would scan for it."""
    look = get_look()
    song, record = page.carry
    rect = pygame.Rect(left, HEADER_RULE + 24, right - left, HERO_H)

    # The drawing's block is not flat: it carries a hair of the accent's hue
    # at the left and has lost all of it by the right edge, where it lands on
    # exactly the value the setup card is painted in. That relationship is
    # the point -- the hero is the same surface as the card, lit from the
    # left -- so the right end is the field itself and only the left end is
    # a mix. It used to mix the accent into both ends, which left the whole
    # block faintly teal and the relationship invisible.
    cool = look.popover
    warm = design.mix(look.hairline_quiet, look.hud_accent, 0.02)
    _gradient(surface, rect, warm, cool, design.RADIUS_LG)
    pygame.draw.rect(surface, look.hairline, rect, width=1,
                     border_radius=design.RADIUS_LG)

    pad = 27 if rect.width > 420 else 16
    room = rect.width - pad * 2
    _ink(surface, t("menu.continue"), (rect.left + pad, rect.top + 33),
         look.hud_accent, LABEL_SIZE, weight="semibold",
         tracking=EYEBROW_TRACK, upper=True)

    index = next((i for i, row in enumerate(page.rows) if row is song), None)
    heading = (f"{index + 1:02d} {_untitled(song, index + 1)}"
               if index is not None else song.display_title)
    size = 32 if room >= 560 else design.TEXT_XL if room >= 360 else design.TEXT_LG
    _ink(surface, _fit(heading, room, size, weight="semibold"),
         (rect.left + pad, rect.top + 66), look.ink_1, size, weight="semibold")

    parts = [song.summary()]
    if record is not None:
        parts.append(t("menu.best", percent=f"{float(record.best_accuracy):.0f}"))
        parts.append(t("menu.attempts", count=record.attempts))
    _ink(surface, _fit("  ·  ".join(p for p in parts if p), room,
                       design.TEXT_XS),
         (rect.left + pad, rect.top + 98), look.ink_3, design.TEXT_XS)

    page.mark("hero", rect)
    return rect.bottom


def _gradient(surface: pygame.Surface, rect: pygame.Rect, start, end,
              radius: int) -> None:
    """A rounded block with a left-to-right ramp, cached.

    A ramp is per-pixel work and the block is a thousand pixels wide, so it
    is built once and blitted after that. One column per four pixels: at this
    contrast the steps are not visible and the build is fifty times cheaper
    than doing it a pixel at a time.
    """
    if rect.width <= 0 or rect.height <= 0:
        return

    def paint(layer: pygame.Surface) -> None:
        step = 4
        for x in range(0, rect.width, step):
            layer.fill(design.mix(start, end, x / max(1, rect.width - 1)),
                       (x, 0, step, rect.height))
        mask = pygame.Surface(rect.size, pygame.SRCALPHA)
        pygame.draw.rect(mask, (255, 255, 255, 255), mask.get_rect(),
                         border_radius=radius)
        layer.blit(mask, (0, 0), special_flags=pygame.BLEND_RGBA_MULT)

    surface.blit(design.sprite(("studio.hero", rect.size, start, end, radius),
                               rect.size, paint), rect.topleft)


# ── The list ─────────────────────────────────────────────────────────────

def _list(surface: pygame.Surface, page: Page, left: int, right: int,
          base: int, floor: int) -> None:
    look = get_look()
    _ink(surface, t("menu.all_songs"), (left + 13, base + 28), look.ink_4,
         LABEL_SIZE, weight="medium", tracking=LABEL_TRACK, upper=True)

    top = base + 46
    band = max(0, floor - 8 - top)
    page.clamp_scroll(ROW_H, band)
    if band <= 0:
        return

    if not page.rows:
        _ink(surface, _fit(page.empty_title or t("menu.empty_search"), right - left,
                          design.TEXT_MD), ((left + right) // 2, top + 48),
             look.ink_2, design.TEXT_MD, align="center")
        if page.empty_hint:
            _ink(surface, _fit(page.empty_hint, right - left,
                               design.TEXT_SM),
                 ((left + right) // 2, top + 76), look.ink_3, design.TEXT_SM,
                 align="center")
        return

    clip = surface.get_clip()
    surface.set_clip(pygame.Rect(0, top, page.width, band))
    first = max(0, page.scroll_px // ROW_H)
    last = min(len(page.rows), (page.scroll_px + band) // ROW_H + 1)
    for index in range(first, last):
        _row(surface, page, index, left, right,
             top + index * ROW_H - page.scroll_px)
    surface.set_clip(clip)


def _row(surface: pygame.Surface, page: Page, index: int, left: int,
         right: int, y: int) -> None:
    look = get_look()
    song = page.rows[index]
    rect = pygame.Rect(left, y, right - left, ROW_H)
    selected = index == page.selected
    # Only the part inside the band is clickable. The bottom row is cut
    # by the band on purpose -- that is how the page says there is more
    # list -- and the half hanging over the footer must not take a click.
    visible = rect.clip(surface.get_clip())
    if visible.height > 0:
        page.mark_row(visible, index)

    if selected:
        pygame.draw.rect(surface, look.select_fill, rect, border_radius=10)
    elif index != page.selected - 1 and index != len(page.rows) - 1:
        # The selected block swallows the hairlines either side of it: a
        # fill with a line resting on its edge reads as a box, and the whole
        # point of this list is that nothing in it is boxed.
        pygame.draw.line(surface, look.hairline_quiet, (rect.left, rect.bottom),
                         (rect.right, rect.bottom), 1)

    _ink(surface, f"{index + 1:02d}", (left + ROW_ORDINAL, rect.centery),
         look.ink_4, design.TEXT_XS, mono=True, align="right")

    wide = right - left
    accuracy = page.scores.get(index)
    # Where the title has to stop. The columns to its right are dropped
    # outside in, so the last thing a narrow window keeps is the score.
    if wide >= NEEDS_RAIL:
        stop = right - LEVEL_RIGHT
    elif wide >= NEEDS_LEVEL:
        stop = right - LEVEL_RIGHT_TIGHT
    else:
        stop = right - 60

    text_x = left + ROW_TEXT
    room = stop - text_x - 24
    _ink(surface, _fit(_untitled(song, index + 1), room, design.TEXT_MD,
                       weight="semibold"),
         (text_x, y + ROW_TITLE_MID), look.ink_1, design.TEXT_MD,
         weight="semibold")
    _ink(surface, _fit(_meta(page, index, song), room, design.TEXT_XS),
         (text_x, y + ROW_META_MID), look.ink_3, design.TEXT_XS)

    if wide >= NEEDS_LEVEL:
        _dot(surface, (stop, rect.centery), 4,
             design.difficulty_colour(song.difficulty))
        _ink(surface, song.level_name, (stop + 12, rect.centery), look.ink_2,
             design.TEXT_SM)

    rail = pygame.Rect(right - RAIL_RIGHT - RAIL_W,
                       rect.centery - RAIL_H // 2, RAIL_W, RAIL_H)
    if accuracy is None:
        # A piece never played still gets its rail, empty. Leaving it out
        # made "Neu" look like a row that had failed to draw rather than
        # like the start of the scale the rows above it are on.
        if wide >= NEEDS_RAIL:
            pygame.draw.rect(surface, look.track_empty, rail,
                             border_radius=RAIL_H // 2)
        _ink(surface, t("menu.progress_new"),
             (right - SCORE_RIGHT, rect.centery), look.ink_2,
             design.TEXT_SM, weight="semibold", align="right")
        return

    # From the look, not from a constant: the green tuned for this page
    # measures 1.38:1 on Hi-fi's paper, and a percentage the player cannot read
    # is the one number on the row that matters.
    colour = (look.feedback_hit if accuracy >= 80
              else look.feedback_close if accuracy >= 50 else look.ink_3)
    if wide >= NEEDS_RAIL:
        pygame.draw.rect(surface, look.track_empty, rail,
                         border_radius=RAIL_H // 2)
        filled = int(rail.width * min(accuracy, 100) / 100)
        if filled > 0:
            pygame.draw.rect(surface, colour,
                             (rail.x, rail.y, filled, rail.height),
                             border_radius=RAIL_H // 2)
    _ink(surface, t("menu.percent", percent=f"{accuracy:.0f}"),
         (right - SCORE_RIGHT, rect.centery), colour, design.TEXT_SM,
         weight="semibold", align="right")


def _artist(song, number: int) -> str:
    """The artist, unless it is this row's number said a second time.

    Their exercises carry "Übung 7" in the artist field, so a row that opened
    with the artist would print the ordinal sitting beside it all over again
    -- which is what the drawing's row does not do. A real tab's artist is a
    name, has no number in it or has one that is not this row's, and stays.
    """
    name = song.artist.strip()
    digits = "".join(c for c in name if c.isdigit())
    return "" if digits and int(digits) == number else name


def _meta(page: Page, index: int, song) -> str:
    """Tempo, then how often and how recently -- one line, under the title.

    Deliberately not song.summary(). That is the hero's line, and on a row
    it opens with what the ordinal and the title have already said -- "Übung
    1  ·  8 Takte  ·  60 BPM" -- and then runs out of room, so at 1024 px
    the ellipsis ate the date, which is the one part of the line the player cannot
    read anywhere else on the page. The drawing's row is tempo, tries, date,
    with the artist in front of it when there is one, because the artist is
    the only part of the summary a title does not already carry.
    """
    if not song.playable:
        return t("menu.no_track")
    parts = [part for part in (_artist(song, index + 1),
                               t("song.bpm", bpm=song.bpm) if song.bpm else "")
             if part]
    record = page.history.get(index)
    if record is None:
        parts.append(t("menu.never_played"))
    else:
        attempts, date = record
        parts.append(t("menu.attempts_date", count=attempts, date=date) if date
                     else t("menu.attempts", count=attempts))
    return "  ·  ".join(parts)


# ── Footer ───────────────────────────────────────────────────────────────

def _footer(surface: pygame.Surface, left: int, right: int, side_w: int,
            width: int, height: int) -> None:
    """Keys as words, not as buttons.

    They were four filled rectangles, which promise a click, carry a border
    each and take four times the room to say the same thing.
    """
    look = get_look()
    y = height - FOOTER_RULE
    pygame.draw.line(surface, look.hairline, (side_w, y), (width, y), 1)
    pairs = [(t("key.updown"), t("menu.hint_select")),
             (t("key.enter"), t("menu.play")),
             (t("key.o"), t("menu.folder")),
             (t("key.s"), t("menu.setup"))]
    while pairs and _hints_width(pairs) > right - left:
        pairs.pop()
    if pairs:
        shapes.hints(surface, (left, height - 36), pairs, ink=look.ink_3,
                     key_ink=look.ink_3, size=design.TEXT_XS,
                     gap=design.SPACE_6)


_hint_widths: dict[tuple, int] = {}


def _hints_width(pairs, gap: int = design.SPACE_6) -> int:
    """What shapes.hints will occupy, measured once per set of words.

    `gap` has to be the gap the draw call will actually use. It was not: the
    card lays its hints out at SPACE_5 and this measured them at SPACE_6, so
    the card threw away a hint that had twelve pixels of room -- and the hint
    it threw away was O, the one key on that panel with nowhere else to say
    it was live.
    """
    key = (get_look().key, tuple(pairs), gap)
    found = _hint_widths.get(key)
    if found is None:
        found = sum(design.text_width(k, design.TEXT_XS, mono=True) + 6
                    + design.text_width(label, design.TEXT_XS) + gap
                    for k, label in pairs) - gap
        if len(_hint_widths) >= 64:
            _hint_widths.clear()
        _hint_widths[key] = found
    return found


# ── The play screen ──────────────────────────────────────────────────────
# Measured off `scratchpad/skins/seite2.png` the same way seite1 was: an
# 1867x1225 export of the 1280x766 sheet, origin (59, 117), scale 1.36875,
# read back with a pygame probe. Every number below with "sheet" against it
# is a sampled pixel position, not a guess.
#
# The shape, in one paragraph. No chrome band and no panels: the board colour
# runs from the top of the window to the bottom, and what divides it is one
# full-width rule which is also the progress bar. Above the rule, the piece
# on the left with its tuning under it, three stat columns in the middle, the
# clock on the right. Below it a flat field -- no lane stripes, no lane bands
# -- with one 1 px line per string tinted in that string's own colour, a dim
# beat and bar grid, the string letters in a gutter on the left, and a thin
# white playhead a fifth of the way across. At the foot a round transport
# button, then everything the player can reach with a key, each carrying its key.
#
# WHAT A NOTE PAINTER MAY ASK THIS MODULE FOR
#
# The notes are not on the Page and are not drawn here: scrolling.py owns
# note layout and note drawing. What it needs from the look is geometry, and
# this is all of it:
#
#   board(page)        -> Rect   the lane band the notes scroll through. Its
#                                left edge is past the string gutter; its top
#                                and bottom are the four lanes and nothing
#                                else. The playhead and the bar grid are
#                                drawn taller than this, down to the footer
#                                rule, which is the sheet's own proportion
#                                and the reason this is a band rather than
#                                "everything between the two rules".
#   playhead_x(page)   -> int    where a card's left edge meets "now".
#                                Studio marks the start of the rectangle;
#                                the circular Hi-fi notes keep a centre anchor.
#   lane_y(page, string, strings)
#                      -> int    the centre line of one string's lane.
#                                `string` is 1 for the highest string, the
#                                way the tab files number them.
#   note_box(page, string, strings, x, w)
#                      -> Rect   the rounded card starting at onset `x`,
#                                with collision-limited head width `w`.
#                                Its duration is drawn as a separate tail.
#
# and three numbers, which are part of the same contract:
#
#   VISIBLE_WINDOW_MS  how much music fits across the board. With
#                      playhead_x that is the whole time-to-x map:
#                          px_per_ms = (board.right - playhead_x)
#                                      / VISIBLE_WINDOW_MS
#                          x         = playhead_x + (t - now) * px_per_ms
#                      The beat and bar grid drawn here uses exactly that, so
#                      a note painter that derives x any other way will drift
#                      off the bars its notes are supposed to sit on.
#   NOTE_RADIUS        corner radius, as a fraction of the note's height.
#   NOTE_FRET          the fret number's size, likewise -- large, bold, and
#                      in design.FRET_INK, the one dark ink that clears
#                      4.5:1 on all six string colours.
#
# WHAT IT REPORTS BACK
#
# page.mark names, so the screen can route a click without knowing this
# layout: "transport", "restart", "tempo.slower", "tempo.faster", "menu",
# "signal", and one per toggle named "toggle." plus the toggle's own label
# key -- "toggle.play.listen". A control the window was too narrow to draw is
# not in the map, so it cannot be clicked, which is the whole point of it.

# Sheet coordinates, 1280 wide and 766 tall. Horizontal numbers are offsets
# from an edge; the two bands are fixed heights and the board takes the rest,
# because the board is the only part of this page that wants to be bigger.
PAD = 27                        # window edge to the header's and footer's words
HEAD_H = 82                     # header band; the rule sits in its last 3 px
FOOT_H = 95                     # footer band, measured up from the bottom
FOOT_AIR = 36                   # lane band's foot to the footer rule
RULE_H = 3                      # the progress rule, which is the header rule
GUTTER = 77                     # the string letters' column, left of the board

# The playhead. 327.3 of 1280 on the sheet, against a board that starts at 77
# -- 0.208 of the board, within a pixel of the 0.20 scrolling.py has used all
# along, and measured rather than inherited.
PLAYHEAD_FRACTION = 0.208
VISIBLE_WINDOW_MS = 8000.0

# The note, as fractions of the lane pitch and of its own height. The sheet's
# note is 90 x 74.6 on a 138.4 lane, with a 16 px corner and a 28 px digit.
NOTE_HEIGHT = 0.54
NOTE_RADIUS = 0.21
NOTE_FRET = 0.38
# How much of the string colour a 1 px lane line is. Measured by conserving
# ink rather than by reading the brightest pixel: the sheet is a 1.37x export
# and its lane line is spread over four rows, none of them the value it was
# drawn in -- the brightest is 0.24 of the string colour, and a 1 px line
# drawn at 0.24 is half the line the drawing has. Summed and divided back
# down, all four lanes land between 0.50 and 0.56.
LANE_TINT = 0.5

# The gutter. Cap height 11.7 for the letter and 8.8 for the octave on the
# sheet; the ink centres sit either side of the lane line rather than being
# stacked away from it, which is what keeps the pair reading as a label FOR
# that line rather than as a column of its own.
LETTER_SIZE = design.TEXT_MD
LETTER_MID = -10
OCTAVE_MID = 15

# Header. The stat columns share their two centre lines with the title and
# the subtitle, which is what makes the band read as one row of things
# rather than four stacked blocks.
TITLE_SIZE = 19                 # "05 Open strings up and down" measures 261.6
TITLE_MID = 30                  # ink centre lines, sheet
SUB_MID = 52
STAT_SIZE = design.TEXT_MD
STAT_GAP = 36                   # between two stat columns
CLOCK_MID = 39                  # the clock is centred in the band, not on a row

# Footer, from the rule at its top. One row of ink at FOOT_MID, or two at
# FOOT_HIGH and FOOT_LOW for everything that carries its key underneath.
FOOT_MID = 47
BLOCK_H = 50                    # the tempo stepper and the toggle group
BLOCK_PAD = 7                   # group edge to the first segment
SEG_GAP = 15                    # between two toggle segments
SEG_PAD = 10                    # segment edge to its dot, and to its keycap
TRANSPORT_R = 28                # the round play/pause button, 55.5 across
GAP = 23                        # between two footer groups


def play(surface: pygame.Surface, page: Page) -> None:
    """The playing screen, Studio's way."""
    look = get_look()
    surface.fill(look.panel_right)

    head, _foot = _bands(page)
    _stage(surface, page)
    _play_header(surface, page, head)
    _play_footer(surface, page)

    # In the air at the foot of the board, which is what that gap is for. It
    # cannot go over the lanes: the notes are drawn after this call returns,
    # so a strip up there would end up underneath the one thing it needs to
    # be read instead of. Nothing is ever drawn below the last lane.
    message = page.banner or getattr(page, "loop_status", "")
    if not message and _air(page) >= 24:
        head = playhead_x(page)
        y = board(page).bottom + _air(page) // 2
        # Keep this cue outside the notes. The marker points to the exact
        # strike line; the coloured card starts here on its musical onset.
        shapes.polygon(surface, look.hit_zone,
                       [(head, y - 7), (head - 4, y + 1), (head + 4, y + 1)])
        available = page.width - head - 24
        _ink(surface, _fit(t("play.studio_strike"), available, design.TEXT_SM),
             (head + 12, y), look.ink_2, design.TEXT_SM)
    if message:
        field = _field(page)
        height = max(28, _air(page))
        # Anchored to the foot of the lanes rather than to the foot of the
        # field. At every size the player uses they are the same line -- the air is
        # 36 px and the strip is 36 px -- but on a window short enough for
        # the air to be thinner than the strip, taking the difference off the
        # footer costs them a keycap for a moment and taking it off the board
        # costs them the bottom of the lowest string, which is where the piece
        # the player is playing is.
        top = max(board(page).bottom, field.bottom - height)
        clear_label = t("practice.loop_clear") if getattr(page, "loop_status", "") else ""
        clear_width = design.text_width(clear_label, design.TEXT_XS) + 24 if clear_label else 0
        message_width = page.width - clear_width - (PAD + 8 if clear_label else 0)
        if page.banner:
            design.banner(surface,
                          pygame.Rect(0, top, message_width, height),
                          _fit(message, message_width - PAD * 2, design.TEXT_MD,
                               bold=True))
        else:
            _ink(surface, _fit(message, message_width - PAD * 2, design.TEXT_SM),
                 (message_width // 2, top + height // 2), look.ink_3,
                 design.TEXT_SM, align="center")
        if clear_label:
            button = pygame.Rect(page.width - PAD - clear_width, top + 3,
                                 clear_width, max(22, height - 6))
            pygame.draw.rect(surface, look.panel_right, button, border_radius=6)
            pygame.draw.rect(surface, look.hairline_loud, button, 1, border_radius=6)
            _ink(surface, clear_label, button.center, look.ink_2, design.TEXT_XS,
                 align="center")
            page.mark("loop_clear", button)


# ── The geometry the notes are drawn on ──────────────────────────────────

def board(page: Page) -> pygame.Rect:
    """The lane band: past the gutter, and only as tall as the four lanes."""
    field = _field(page)
    left = _gutter_w(page)
    return pygame.Rect(field.left + left, field.top,
                       max(40, field.width - left),
                       max(40, field.height - _air(page)))


def playhead_x(page: Page) -> int:
    """The onset line, touching the card's leading (left) edge on the beat."""
    rect = board(page)
    return rect.left + int(round(rect.width * PLAYHEAD_FRACTION))


def lane_y(page: Page, string: int, strings: int) -> int:
    """The centre line of one string's lane. String 1 is the highest, on top."""
    rect = board(page)
    strings = max(1, int(strings))
    string = max(1, min(strings, int(string)))
    return int(round(rect.top + rect.height * (string - 0.5) / strings))


def note_size(page: Page, strings: int) -> int:
    return round(max(14, round(board(page).height / max(1, strings) * NOTE_HEIGHT)) * 1.2)


def note_box(page: Page, string: int, strings: int, x: float,
             w: float) -> pygame.Rect:
    """The rounded card, with its left edge at onset and its centre on a lane.

    Positive `w` is the available head width, already limited by both
    neighbours. Shrink the height with it; never enlarge a collision limit.
    Duration is drawn separately behind the heads.
    """
    strings = max(1, int(strings))
    height = max(14, int(round(board(page).height / strings * NOTE_HEIGHT)))
    width = max(1, int(round(w))) if w > 0 else round(height * 1.2)
    height = min(height, max(1, round(width / 1.2)))
    return pygame.Rect(int(round(x)), lane_y(page, string, strings) - height // 2,
                       width, height)


def note_onset(surface: pygame.Surface, rect: pygame.Rect, colour) -> None:
    """A crisp leading edge separates the strike cue from a soft halo."""
    if rect.width < 8 or rect.height < 10:
        return
    inset = max(2, rect.height // 5)
    pygame.draw.line(surface, colour, (rect.left, rect.top + inset),
                     (rect.left, rect.bottom - inset - 1), 2)


def _field(page: Page) -> pygame.Rect:
    """Everything between the two rules, gutter included."""
    head, foot = _bands(page)
    return pygame.Rect(0, head, page.width,
                       max(40, page.height - head - foot))


def _bands(page: Page) -> tuple[int, int]:
    """Header and footer heights. Both are fixed until the board would starve.

    They hold type at fixed sizes, so they do not scale with the window --
    but a 280 px window is 103 px of board between them, four lanes of 25 px.
    Below that floor the two bands give ground together rather than the
    header taking all of it.
    """
    if page.height - HEAD_H - FOOT_H >= 120:
        return HEAD_H, FOOT_H
    share = max(40, page.height - 120) / (HEAD_H + FOOT_H)
    return max(34, int(HEAD_H * share)), max(40, int(FOOT_H * share))


def _air(page: Page) -> int:
    """The gap the sheet leaves between the last lane and the footer rule."""
    return min(FOOT_AIR, max(0, (_field(page).height - 120) // 4))


def _gutter_w(page: Page) -> int:
    """The string letters' column. Dropped rather than squeezed."""
    if page.width >= 900:
        return GUTTER
    return 48 if page.width >= 480 else 0


def _pad(page: Page) -> int:
    return PAD if page.width >= 760 else 14


def _px_per_ms(page: Page) -> float:
    rect = board(page)
    return max(0.0, rect.right - playhead_x(page)) / max(1.0, page.visible_window_ms)


# ── The board ────────────────────────────────────────────────────────────

def _stage(surface: pygame.Surface, page: Page) -> None:
    """One flat fill, a dim grid, four tinted lines, the letters, the playhead.

    No lane bands. The drawing's board is the same colour as the header and
    the footer, and what separates the four strings is a line in the string's
    own colour and a letter in the gutter -- nothing here is a filled shape.
    That is most of why this page reads as expensive rather than as a
    wireframe with stripes in it.
    """
    look = get_look()
    field = _field(page)
    rect = board(page)
    strings = _tuning(page)
    count = len(strings)

    _grid(surface, page, field, rect.left)

    for index in range(count):
        colour = design.string_colour(index + 1)
        y = lane_y(page, index + 1, count)
        pygame.draw.line(surface, design.mix(look.panel_right, colour, LANE_TINT),
                         (rect.left, y), (rect.right - 1, y), 1)
        # The lane's own edge, full width so it crosses the gutter too. It is
        # quieter than anything the notes are drawn in, which is what keeps
        # it decoration rather than a band: the top edge is under the header
        # rule and is left to it.
        edge = int(round(rect.top + rect.height * (index + 1) / count))
        pygame.draw.line(surface, look.hairline_quiet, (0, edge),
                         (page.width, edge), 1)

    if rect.left:
        _gutter(surface, page, rect, strings)

    # Taller than the lanes, down to the footer rule, which is the sheet's
    # own proportion. Drawn in the Look's hit_zone rather than in ink_1,
    # which is what the sheet measures: a look is allowed to choose the
    # colour of its own playhead, and Studio's choice happens to be white.
    head = playhead_x(page)
    pygame.draw.line(surface, look.hit_zone, (head, field.top),
                     (head, field.bottom - 1), 2)


def _grid(surface: pygame.Surface, page: Page, field: pygame.Rect,
          left: int) -> None:
    """Beats, and every fourth of them a bar.

    Derived from what a Page carries -- the tempo, the clock and how many
    bars the piece has -- rather than from the timeline, which a painter is
    not allowed to see. That is also why the beats per bar is divided out of
    the total rather than assumed to be four: their exercises are in four, but
    a piece in three would otherwise get its bar lines in the wrong place,
    every bar, with nothing on screen admitting it.
    """
    look = get_look()
    if page.timeline_markers:
        per_ms = _px_per_ms(page)
        head = playhead_x(page)
        last_x = -1000
        for beat in page.timeline_beats:
            x = round(head + (beat.timestamp_ms - page.elapsed_ms) * per_ms)
            if not left <= x < field.right or (not beat.accent and x - last_x < 7):
                continue
            pygame.draw.line(surface, look.hairline_loud if beat.accent else look.hairline_quiet,
                             (x, field.top), (x, field.bottom - 1), 1)
            last_x = x
        return
    if page.bpm <= 0:
        return
    per_ms = _px_per_ms(page)
    beat_ms = 60_000.0 / page.bpm
    step = beat_ms * per_ms
    if step < 7:                # closer together than this is a texture
        return
    per_bar = 4
    if page.bars > 0 and page.total_ms > 0:
        per_bar = max(1, min(16, int(round(page.total_ms / page.bars / beat_ms))))

    head = playhead_x(page)
    first = int((left - head) / step) - 1
    last = int((field.right - head) / step) + 1
    beat = int(page.elapsed_ms // beat_ms)
    for index in range(first, last + 1):
        x = int(round(head + (beat + index) * beat_ms * per_ms
                      - page.elapsed_ms * per_ms))
        if not left <= x < field.right:
            continue
        bar_line = (beat + index) % per_bar == 0
        pygame.draw.line(surface,
                         look.hairline_loud if bar_line else look.hairline_quiet,
                         (x, field.top), (x, field.bottom - 1), 1)


def _gutter(surface: pygame.Surface, page: Page, rect: pygame.Rect,
            strings: tuple[tuple[str, str], ...]) -> None:
    """One coloured letter per string, its note name and octave underneath.

    Cached whole. It is eight pieces of text that change only when the window
    or the instrument does, and eight font renders a frame is a third of this
    page's budget spent on something that has not moved since it was drawn.
    """
    look = get_look()
    column = pygame.Rect(0, rect.top, rect.left, rect.height)
    large = LETTER_SIZE if rect.left >= GUTTER else design.TEXT_SM
    small = design.TEXT_XS if rect.left >= GUTTER else 11

    def paint(layer: pygame.Surface) -> None:
        for index, (name, octave) in enumerate(strings):
            y = lane_y(page, index + 1, len(strings)) - rect.top
            _ink(layer, name, (column.width // 2, y + LETTER_MID),
                 design.string_colour(index + 1), large, weight="bold",
                 align="center")
            if octave:
                _ink(layer, name + octave, (column.width // 2, y + OCTAVE_MID),
                     look.ink_3, small, align="center")

    surface.blit(design.sprite(("studio.play.gutter", look.key, _lang(),
                                column.size, strings), column.size, paint),
                 column.topleft)


_tunings: dict[tuple, tuple] = {}


def _tuning(page: Page) -> tuple[tuple[str, str], ...]:
    """(letter, octave) per string, the highest string first.

    A Page carries no string count and no tuning for the play screen. What it
    does carry is `tuning_label` -- "E A D G", lowest string first, the way
    the library writes it -- and a subtitle that ends in the same thing,
    which is where it comes from on this screen. Neither carries the octave,
    so that is matched back against the shipped instruments: the letters and
    the string count identify the tuning and the profile holds the MIDI
    notes.

    Nothing here guesses. A tuning that matches no profile gets its letters
    and no octave, which is the honest answer and still says which row is
    which string.
    """
    # The screen states this outright now. What follows it is the fallback
    # for a page that does not -- parsing the tuning back out of a label was
    # the only way to know before Page carried it, and it is still the only
    # way for a caller that builds a Page by hand.
    if page.string_labels:
        return tuple((letter, name[len(letter):])
                     for _number, letter, name in page.string_labels)
    key = (page.tuning_label, page.subtitle)
    found = _tunings.get(key)
    if found is not None:
        return found
    tail = page.subtitle
    for mark in ("·", "–", "—", "-"):
        tail = tail.replace(mark, "|")
    names = _tuning_names(page.tuning_label) or _tuning_names(
        tail.rpartition("|")[2])
    if not names:
        names = ["E", "A", "D", "G", "B", "E"]
    names = list(reversed(names))                 # string 1, the highest, first
    found = tuple(zip(names, _octaves(names)))
    if len(_tunings) > 32:
        _tunings.clear()
    _tunings[key] = found
    return found


def _tuning_names(value: str) -> list[str]:
    """The note letters in `value`, or nothing if it is not a tuning.

    Deliberately strict. This also runs over a subtitle, which is prose, and
    a loose test turns "Bass-Ukulele" into a six string instrument tuned B.
    """
    parts = value.split()
    if len(parts) < 3:
        return []
    out = []
    for part in parts:
        head = part[0:1].upper()
        tail = part[1:].lstrip("#b♯♭").rstrip("0123456789")
        if head not in "ABCDEFG" or tail:
            return []
        out.append(head)
    return out


def _octaves(names: list[str]) -> list[str]:
    """The octave digit per string, from whichever shipped tuning matches."""
    from pickhero import instruments
    from pickhero.audio.note_utils import midi_to_name

    for profile in instruments.PROFILES.values():
        tuning = getattr(profile, "tuning", None) or {}
        if len(tuning) != len(names):
            continue
        notes = [midi_to_name(tuning[s]) for s in sorted(tuning)]
        if [n.rstrip("0123456789")[:1] for n in notes] == names:
            return [n[len(n.rstrip("0123456789")):] for n in notes]
    return [""] * len(names)


# ── Header ───────────────────────────────────────────────────────────────

def _play_header(surface: pygame.Surface, page: Page, height: int) -> None:
    """The piece, three readouts, the clock, and the rule that is the bar.

    Only the piece is cached. The readouts and the clock are redrawn every
    frame on purpose: they are eight pieces of text, and a cached band keyed
    on an accuracy that moves with every note would allocate a fresh
    1280 x 82 layer several times a second and evict the note sprites with
    it -- a cache whose key changes as fast as its contents is a slower way
    of not caching.
    """
    look = get_look()
    pad = _pad(page)
    scale = height / HEAD_H
    two_rows = height >= HEAD_H - 8
    title_mid = int(TITLE_MID * scale) if two_rows else height // 2 - 2
    sub_mid = int(SUB_MID * scale)

    clock = f"{_clock(page.elapsed_ms)} / {_clock(page.total_ms)}"
    stats = _stats(page)
    level, columns, show_clock = _header_plan(page, stats, clock, pad)
    total = sum(w for _c, w in columns) + STAT_GAP * max(0, len(columns) - 1)
    stat_left = (page.width - total) // 2
    stop = max(40, (stat_left - 20 if columns else page.width - pad))

    def paint(layer: pygame.Surface) -> None:
        _ink(layer, _fit(page.title, stop - pad, TITLE_SIZE, weight="semibold"),
             (pad, title_mid), look.ink_1, TITLE_SIZE, weight="semibold")
        if page.subtitle and two_rows:
            _ink(layer, _fit(page.subtitle, stop - pad, design.TEXT_XS),
                 (pad, sub_mid), look.ink_3, design.TEXT_XS)

    surface.blit(design.sprite(("studio.play.title", look.key, _lang(),
                                (stop, height), page.title, page.subtitle,
                                pad, title_mid, sub_mid),
                               (stop, height), paint), (0, 0))

    x = stat_left
    for (loud, quiet, label, colour), width in columns:
        centre = x + width // 2
        run = (design.text_width(loud, STAT_SIZE, weight="semibold")
               + design.text_width(quiet, STAT_SIZE, weight="semibold"))
        end = _ink(surface, loud, (centre - run // 2, title_mid), colour,
                   STAT_SIZE, weight="semibold")
        if quiet:
            # The slash and the total are the half the player is not reading. A bar
            # number the player has to pick out of the count beside it was the one
            # readout on the old screen that could not be found.
            _ink(surface, quiet, (end.right, title_mid), look.ink_4,
                 STAT_SIZE, weight="semibold")
        if level == 0 and two_rows:
            _ink(surface, label, (centre, sub_mid), look.ink_4, LABEL_SIZE,
                 weight="medium", tracking=LABEL_TRACK, upper=True,
                 align="center")
        x += width + STAT_GAP

    if show_clock:
        elapsed, _slash, rest = clock.partition(" / ")
        mid = int(59 * scale) if two_rows else height // 2
        right = page.width - pad
        tail = " / " + rest
        _ink(surface, tail, (right, mid), look.ink_4, design.TEXT_XS,
             mono=True, align="right")
        _ink(surface, elapsed,
             (right - design.text_width(tail, design.TEXT_XS, mono=True), mid),
             look.ink_2, design.TEXT_XS, mono=True, align="right")

    _play_tools(surface, page, height)

    # The rule is the progress bar, so it is the one line on this page that
    # moves every frame and it is drawn last, over whatever ran into it.
    rule = pygame.Rect(0, height - RULE_H - 1, page.width, RULE_H)
    pygame.draw.rect(surface, look.hairline_quiet, rule)
    done = page.elapsed_ms / page.total_ms if page.total_ms > 0 else 0.0
    played = int(rule.width * max(0.0, min(1.0, done)))
    if played > 0:
        pygame.draw.rect(surface, look.ink_1, (0, rule.top, played, RULE_H))


def _stats(page: Page) -> list[tuple[str, str, str, tuple[int, int, int]]]:
    """(loud part, quiet part, label, colour) for the three readouts."""
    look = get_look()
    accuracy = page.accuracy
    if accuracy is None:
        hit, hit_ink = "–", look.ink_4
    else:
        hit = t("hud.percent", percent=f"{accuracy:.0f}")
        hit_ink = (look.feedback_hit if accuracy >= 80
                   else look.feedback_close if accuracy >= 50 else look.ink_2)
    return [
        (str(max(0, page.bar)), f"/{page.bars}" if page.bars else "",
         t("play.bar_label"), look.ink_1),
        (str(max(0, page.bpm)), "", t("play.bpm_label"), look.ink_1),
        (hit, "", t("play.hit_label"), hit_ink),
    ]


def _header_plan(page: Page, stats, clock: str, pad: int):
    """How much of the band survives this width, outside in.

    The title is what a narrow window keeps: nothing else on the page says
    which piece this is. So the tracked labels go first, then the tempo --
    which the piece does not change and the footer shows a percentage of
    anyway -- then the accuracy, then the clock. The bar number is the last
    readout standing, because it is the only one the player cannot work out from
    the board in front of them, and the title still has 150 px under it.
    """
    clock_w = design.text_width(clock, design.TEXT_XS, mono=True) + pad + 20
    tools_w = sum(width for _action, _label, width in _play_tool_labels()) + 8
    for level in range(4):
        columns = _columns(stats, level)
        total = sum(w for _c, w in columns) + STAT_GAP * max(0, len(columns) - 1)
        for show_clock in (True, False):
            tail = max(clock_w if show_clock else pad, tools_w + pad + 20)
            if total and (page.width + total) // 2 > page.width - tail:
                continue
            if total:
                if (page.width - total) // 2 - pad - 20 < 150:
                    continue
                if page.width - tail - pad < total + 40:
                    continue
            elif page.width - tail - pad < 150:
                continue
            return level, columns, show_clock
    return 3, [], False


def _play_tool_labels():
    return [(action, label, design.text_width(label, design.TEXT_XS,
                                              weight="medium") + 20)
            for action, label in (
                ("setup", f"{t('key.s')}  {t('practice.setup')}"),
                ("help", f"{t('key.h')}  {t('play.help')}"))]


def _play_tools(surface: pygame.Surface, page: Page, height: int) -> None:
    look = get_look()
    entries = _play_tool_labels()
    x = page.width - _pad(page) - sum(w for _a, _l, w in entries) - 8
    top = max(3, min(14, height // 2 - 14))
    for action, label, width in entries:
        rect = pygame.Rect(x, top, width, 28)
        pygame.draw.rect(surface, look.hairline_loud, rect, 1, border_radius=8)
        _ink(surface, label, rect.center, look.ink_2, design.TEXT_XS,
             weight="medium", align="center")
        page.mark(action, rect)
        x = rect.right + 8


def count_in(surface: pygame.Surface, page: Page) -> None:
    """Draw over the notes so the count remains legible during preroll."""
    remaining = getattr(page, "count_in_remaining", 0)
    if remaining <= 0:
        return
    look = get_look()
    rect = board(page)
    size = min(120, max(72, rect.height // 3))
    card = pygame.Rect(0, 0, size + 100, size + 54)
    card.center = rect.center
    pygame.draw.rect(surface, look.panel_right, card, border_radius=18)
    pygame.draw.rect(surface, look.hairline_loud, card, 1, border_radius=18)
    _ink(surface, str(remaining), (card.centerx, card.centery - 8),
         look.ink_1, size, weight="semibold", align="center")
    _ink(surface, t("play.count_in"), (card.centerx, card.bottom - 21),
         look.ink_2, design.TEXT_SM, align="center")
    progress = max(0.0, min(1.0, getattr(page, "count_in_progress", 0.0)))
    pygame.draw.rect(surface, look.hud_accent,
                     (card.x + 18, card.bottom - 6, int((card.width - 36) * progress), 2))


# Which readout goes at each step down, by position in _stats: the tempo
# first, then the accuracy. The bar number has no entry, so it never goes.
STAT_DROP = (1, 2)


def _columns(stats, level: int):
    """(stat, width) for the columns this level keeps."""
    dropped = set(STAT_DROP[:max(0, level - 1)])
    kept = [s for i, s in enumerate(stats) if i not in dropped]
    out = []
    for loud, quiet, label, colour in kept:
        width = (design.text_width(loud, STAT_SIZE, weight="semibold")
                 + design.text_width(quiet, STAT_SIZE, weight="semibold"))
        if level == 0:
            width = max(width, design.text_width(label, LABEL_SIZE,
                                                 weight="medium",
                                                 tracking=LABEL_TRACK,
                                                 upper=True))
        out.append(((loud, quiet, label, colour), width))
    return out


def _clock(ms: float) -> str:
    whole = max(0, int(ms // 1000))
    return f"{whole // 60}:{whole % 60:02d}"


# ── Footer ───────────────────────────────────────────────────────────────

def _play_footer(surface: pygame.Surface, page: Page) -> None:
    """Everything the player can reach, each thing carrying its own key.

    Laid out once into the band's own coordinates, then painted from a cache
    and marked from the same rects -- so a control can never be drawn in one
    place and clicked in another, which is the failure the hit map exists to
    make impossible.
    """
    look = get_look()
    _head, height = _bands(page)
    band = pygame.Rect(0, page.height - height, page.width, height)
    pygame.draw.line(surface, look.hairline, (0, band.top),
                     (page.width, band.top), 1)

    plan = _footer_plan(page, band.width, band.height)

    def paint(layer: pygame.Surface) -> None:
        _footer_ink(layer, page, plan)

    surface.blit(design.sprite(("studio.play.foot", look.key, _lang(), band.size,
                                plan["level"], page.paused, page.tempo_percent,
                                tuple(page.toggles)), band.size, paint),
                 band.topleft)

    if plan["meter"] is not None:
        rect = plan["meter"].move(0, band.top)
        shapes.meter(surface, rect, page.signal, on=look.feedback_hit,
                     off=look.hairline_loud, bars=5)
        page.mark("signal", rect.union(plan["mic"].move(0, band.top)))

    for name, rect in plan["hits"]:
        page.mark(name, rect.move(0, band.top))


def _footer_plan(page: Page, width: int, height: int) -> dict:
    """What fits, and where. In the footer band's own coordinates.

    Dropped outside in, by LADDER. The transport button and the way out are
    never dropped -- a screen the player cannot stop or leave with the mouse is not
    a smaller version of this one.
    """
    for level in range(len(LADDER)):
        plan = _footer_try(page, width, height, level)
        if plan is not None:
            return plan
    return _footer_try(page, width, height, len(LADDER) - 1, force=True)


# What each step down gives up, and in what order.
#
# The toggles' words go late rather than first. The first draft dropped them
# first, on the grounds that a lit dot and a keycap still name every toggle
# -- they do not. "A", "W", "I" and "M" are four identical pills to someone
# who is reading the German, and Hoeren, Warten, Schleife and Metronom are
# the four words on this page the player has to understand before the player can practise
# at all. Measured at 900 px, which is one of the sizes this page is built
# for: the words needed 120 px that were not there, and the two controls that
# have another way in -- Neu is also Pos1, and the meter is a check the player makes
# once before the player starts -- give back 174 between them. So at 900 all four
# words survive now, where they used to vanish in one step. 1024 and 1280
# are unchanged (nothing is dropped at either) and so is 640, which lands on
# the same step either way.
#
#          restart  meter  words  tempo  toggles
LADDER = ((True,    True,  True,  True,  True),
          (False,   True,  True,  True,  True),
          (False,   False, True,  True,  True),
          (False,   False, False, True,  True),
          (False,   False, False, False, True),
          (False,   False, False, False, False))


def _footer_try(page: Page, width: int, height: int, level: int,
                force: bool = False) -> dict | None:
    keep_restart, keep_meter, words, keep_tempo, keep_toggles = \
        LADDER[max(0, min(level, len(LADDER) - 1))]
    pad = _pad(page)
    mid = int(FOOT_MID * height / FOOT_H)
    radius = max(13, int(TRANSPORT_R * height / FOOT_H))
    block_h = max(30, int(BLOCK_H * height / FOOT_H))

    hits: list[tuple[str, pygame.Rect]] = []
    plan: dict = {"level": level, "mid": mid, "hits": hits, "meter": None,
                  "mic": None, "restart": None, "tempo": None,
                  "segments": [], "group": None}

    # The way out is measured before anything else is allowed to claim room.
    menu_w = max(design.text_width(t("play.menu"), design.TEXT_SM),
                 design.text_width(t("key.escape"), design.TEXT_XS, mono=True))
    plan["menu"] = pygame.Rect(width - pad - menu_w, mid - 22, menu_w, 44)
    hits.append(("menu", plan["menu"].inflate(16, 0)))
    right = plan["menu"].left - GAP

    if keep_meter:
        mic_w = design.text_width(t("play.mic_short"), design.TEXT_XS)
        meter = pygame.Rect(right - 33, mid - 9, 33, 18)
        plan["mic"] = pygame.Rect(meter.left - 8 - mic_w, mid - 8, mic_w, 16)
        plan["meter"] = meter
        right = plan["mic"].left - GAP

    # Then left to right.
    plan["transport"] = pygame.Rect(pad - 6, mid - radius, radius * 2,
                                    radius * 2)
    hits.append(("play", plan["transport"]))
    x = plan["transport"].right + GAP

    if keep_restart:
        label_w = max(design.text_width(t("play.restart"), design.TEXT_SM),
                      design.text_width(t("key.home"), design.TEXT_XS, mono=True))
        plan["restart"] = pygame.Rect(x, mid - 22, label_w, 44)
        hits.append(("restart", plan["restart"].inflate(12, 0)))
        x += label_w + GAP

    if keep_tempo:
        step_w = max(150, design.text_width(t("play.tempo_hint"),
                                            design.TEXT_XS) + 46)
        plan["tempo"] = pygame.Rect(x, mid - block_h // 2, step_w, block_h)
        hits.append(("tempo_down",
                     pygame.Rect(x, mid - block_h // 2, 42, block_h)))
        hits.append(("tempo_up",
                     pygame.Rect(x + step_w - 42, mid - block_h // 2, 42,
                                 block_h)))
        x += step_w + 14

    if keep_toggles and page.toggles:
        segments = []
        cursor = x + BLOCK_PAD
        for label_key, key_key, on, action in page.toggles:
            word = (design.text_width(t(label_key), design.TEXT_SM,
                                      weight="medium") + 10) if words else 0
            cap = design.text_width(t(key_key), design.TEXT_XS, mono=True)
            seg = pygame.Rect(cursor, mid - (block_h - 8) // 2,
                              SEG_PAD * 2 + 8 + word + 10 + cap, block_h - 8)
            segments.append((seg, label_key, key_key, on, words))
            hits.append((action, seg))
            cursor = seg.right + SEG_GAP
        plan["segments"] = segments
        plan["group"] = pygame.Rect(x, mid - block_h // 2,
                                    cursor - SEG_GAP - x + BLOCK_PAD, block_h)
        x = plan["group"].right

    if x <= right or force:
        return plan
    return None


def _footer_ink(layer: pygame.Surface, page: Page, plan: dict) -> None:
    look = get_look()
    mid = plan["mid"]

    _transport(layer, plan["transport"], page.paused)

    if plan["restart"] is not None:
        left = plan["restart"].left
        _ink(layer, t("play.restart"), (left, mid - 8), look.ink_2,
             design.TEXT_SM)
        _ink(layer, t("key.home"), (left, mid + 10), look.ink_3,
             design.TEXT_XS, mono=True)

    if plan["tempo"] is not None:
        rect = plan["tempo"]
        pygame.draw.rect(layer, look.select_fill, rect, border_radius=12)
        _ink(layer, t("hud.percent", percent=page.tempo_percent),
             (rect.centerx, mid - 8), look.ink_1, design.TEXT_SM,
             weight="semibold", align="center")
        _ink(layer, _fit(t("play.tempo_hint"), rect.width - 24,
                         design.TEXT_XS),
             (rect.centerx, mid + 10), look.ink_3, design.TEXT_XS,
             align="center")
        # The two steppers are drawn rather than set. A hyphen and a plus in
        # Geist are two different weights sitting on two different lines, and
        # at this size that reads as a mistake rather than as a pair.
        for cx, plus in ((rect.left + 20, False), (rect.right - 20, True)):
            pygame.draw.rect(layer, look.ink_3, (cx - 5, mid - 1, 11, 2))
            if plus:
                pygame.draw.rect(layer, look.ink_3, (cx - 1, mid - 5, 2, 11))

    if plan["group"] is not None:
        pygame.draw.rect(layer, look.select_fill, plan["group"],
                         border_radius=12)
        for seg, label_key, key_key, on, words in plan["segments"]:
            if on:
                # One step past the group it sits in -- the same two Look
                # fields the library's keycap mixes, for the same reason:
                # this is the one surface in the footer that has to be
                # lighter than the control it is inside.
                pygame.draw.rect(layer,
                                 design.mix(look.hairline, look.hairline_loud,
                                            0.4),
                                 seg, border_radius=10)
            _dot(layer, (seg.left + SEG_PAD + 4, mid), 4 if on else 3,
                 look.feedback_hit if on
                 else design.mix(look.hairline_loud, look.ink_4, 0.2))
            x = seg.left + SEG_PAD + 8
            if words:
                x = _ink(layer, t(label_key), (x + 10, mid),
                         look.ink_1 if on else look.ink_3, design.TEXT_SM,
                         weight="medium").right
            _ink(layer, t(key_key), (x + 10, mid), look.ink_3,
                 design.TEXT_XS, mono=True)

    if plan["mic"] is not None:
        _ink(layer, t("play.mic_short"), (plan["mic"].left, mid), look.ink_3,
             design.TEXT_XS)

    menu = plan["menu"]
    _ink(layer, t("play.menu"), (menu.centerx, mid - 8), look.ink_2,
         design.TEXT_SM, align="center")
    _ink(layer, t("key.escape"), (menu.centerx, mid + 10), look.ink_3,
         design.TEXT_XS, mono=True, align="center")


def _transport(layer: pygame.Surface, rect: pygame.Rect, paused: bool) -> None:
    """One white disc with the glyph cut out of it rather than drawn on top.

    Cached, and supersampled like the library's dots: pygame.draw.circle has
    no anti-aliasing, and a 54 px octagon is the one thing on this page that
    would look drawn by a program.
    """
    look = get_look()
    size = rect.size

    def paint(surf: pygame.Surface) -> None:
        big = pygame.Surface((size[0] * 4, size[1] * 4), pygame.SRCALPHA)
        pygame.draw.circle(big, look.ink_1, (size[0] * 2, size[1] * 2),
                           size[0] * 2 - 2)
        surf.blit(pygame.transform.smoothscale(big, size), (0, 0))
        cx, cy = size[0] // 2, size[1] // 2
        bar = max(3, size[0] // 9)
        tall = max(7, int(size[1] * 0.2))
        if paused:
            pygame.draw.polygon(surf, look.panel_right,
                                [(cx - bar, cy - tall), (cx + tall, cy),
                                 (cx - bar, cy + tall)])
        else:
            for dx in (-bar - 2, 2):
                pygame.draw.rect(surf, look.panel_right,
                                 (cx + dx, cy - tall, bar, tall * 2),
                                 border_radius=1)

    layer.blit(design.sprite(("studio.play.transport", look.key, size, paused),
                             size, paint), rect.topleft)


def _lang() -> str:
    """The current language, for every cached layer on this page.

    A cached layer holds translated words, so the catalogue is part of what
    built it. Leaving it out of the key is invisible until the player switches
    language and the footer keeps the one it was drawn in.
    """
    from pickhero import i18n
    return i18n.current_language()


# ── Temporary harness ────────────────────────────────────────────────────
# menu.py builds the Page and calls painter(look.key).library already, but
# the two files are mid-change against each other, so this is how the page
# can be rendered on its own while that settles. It reads nothing a painter
# is not allowed to see; it only assembles the same Page menu.py does.
# DELETE THIS, together with its import of menu, once the wiring lands.

def _demo_page(screen, surface: pygame.Surface) -> Page:  # pragma: no cover
    """One frame's Page, built from a live MenuScreen."""
    width, height = surface.get_size()
    rows = screen._rows
    scores = {i: screen._accuracy(song) for i, song in enumerate(rows)}
    history = {}
    for i, song in enumerate(rows):
        record = screen._record_for(song)
        if record is not None:
            history[i] = (record.attempts, (record.last_played or "")[:10])
    played = sum(1 for song in screen._songs
                 if screen._accuracy(song) is not None)
    instrument = screen._instrument()
    return Page(
        width=width, height=height, rows=rows, selected=screen._selected,
        scroll_px=screen._scroll_px,
        filters=[(label, value, count, value == screen._filter)
                 for label, value, count, heading in screen._filter_entries()
                 if not heading],
        carry=screen._continue_song(), scores=scores, history=history,
        search_active=screen._search_active, search_text=screen._search_text,
        setup_open=screen._setup_open,
        instrument_name=screen._instrument_name(),
        instrument_short=screen._instrument_short_name(),
        tuning_label=screen._tuning_label(instrument),
        device_name=screen._device_name, device_ready=bool(screen._device_name),
        counts=(len(screen._songs), played),
    )


def demo(screen, surface: pygame.Surface) -> Page:  # pragma: no cover
    """Build a Page from `screen` and paint it. Temporary; see above."""
    page = _demo_page(screen, surface)
    library(surface, page)
    return page
