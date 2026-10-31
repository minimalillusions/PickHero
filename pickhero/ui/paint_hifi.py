"""The Hi-fi library page: Braun and Bang & Olufsen, not "the light theme".

What makes a good amplifier look expensive is subtraction. There is no border
where whitespace would do, no fill where a hairline would do, and no second
accent anywhere -- the orange marks exactly one thing on the page, the row the player
is on. Everything else is set in one family at four sizes, aligned to one
margin, on two panels that differ by nine points out of two hundred and
thirty-six. That hair is the look. Widening it would turn a panel divide into
a box, and a box is the thing this drawing is made of not having.

Every number here was measured off scratchpad/skins/seite5.png, which is a
1749x1048 sheet shown at 1280x720. Horizontal distances carry over at
1280/1749; the sheet is 47 px taller than the window, so the poster is stacked
upward from the floor rather than read off the sheet's y values -- the space
between the blocks absorbs the difference, which is the only part of the
drawing that can give.

The two things that cost pixels per frame are the circles. pygame has no
antialiased disc, and an aliased 90 px circle is the single cheapest-looking
mark a page can carry, so the play button, the accent dot and the shortcut
badge are supersampled four times and cached through design.sprite. They are
built once each and blitted thereafter.

The second half of the file is the playing screen, measured off seite6.png
the same way. It draws the shell -- the paper, the stave, the gutter, the
playhead, the header and the footer -- and hands the note drawing the
geometry it needs rather than letting it work the geometry out again:

    play(surface, page)                      the whole screen
    board(page)                              the rectangle the notes live in
    lane_y(page, string, strings)            one string's line, 1 = highest
    lane_gap(page, strings)                  the distance between two of them
    note_size(page, strings)                 the diameter of a note's disc
    note_box(page, string, strings, x, w)    the square one is drawn in
    note(surface, box, label, ...)           and how this look draws one
    playhead_x(page)                         where now is
    visible_ms(page) / time_x(page, ms)      and what x means in time

time_x is the one that has to be shared rather than reinvented: the bar
lines are drawn with it, so notes placed by any other mapping land somewhere
the bar lines are not.
"""

from __future__ import annotations

import math
from typing import NamedTuple

import pygame

from pickhero.i18n import t
from pickhero.ui import design, library_setup, shapes
from pickhero.ui.colors import get_look
from pickhero.ui.page import Page, frac, split

# ── The sheet ────────────────────────────────────────────────────────────
# One margin for the whole page. The drawing sets the wordmark, the rail,
# the ordinals, the hairlines, the key hints and the right edge of the pill
# all to the same 52 px at 1280 -- in a poster the margin is the grid.
MARGIN_F = 0.0406
SPLIT_F = 0.40           # where the hard rule falls
PANEL_FLOOR = 280        # below this the poster is dropped, not squeezed
RULE_W = 2               # measured: two pixels, and it is the whole look

# Re-measured off the sheet: the wordmark, "All 10" and the pill's word all
# start their ink 71 px below the top of the drawing, which is 49 px at 720,
# not 44. Five pixels is not a layout change, it is the top margin, and on a
# page whose whole argument is the air around things the top margin is the
# first thing anyone sees.
HEAD_F = 0.0681          # the header line's ink top: 49 px at 720
HEAD_SIZE = 17
WORDMARK_TRACK = 0.26    # measured: 110 px of PICKHERO at 17 px
LABEL_TRACK = 0.14       # every other small-caps label
TAB_GAP = 31             # measured: the air between one filter and the next
# The drawing leaves 170 px between the last filter and the group at the far
# right, so the top line reads as a cluster at each end rather than as one
# strip of words. That void is the composition, not slack to be spent on
# fitting more in, so it is a floor and everything else gives before it: the
# counts come off the labels first, then the air between the filters, then a
# filter. Measured on the German page the old sum left 38 px here, which is
# an accident of arithmetic rather than a gap.
TAB_SPLIT_F = 0.09       # of the header's room: 60 px at 1280
TAB_SPLIT = design.SPACE_8   # and never less than 32, at any width
PILL_GAP = 28            # measured: what sets the pill off whatever precedes

LIST_GAP = 23            # header ink bottom -> the first hairline
ROW_TITLE = 22
ROW_TITLE_W = "light"
ROW_META = design.TEXT_XS
ROW_ORD = 15
ROW_H = 68               # 97.7 sheet px; title ink at 17, meta ink at 44
ROW_TITLE_Y = 17
ROW_ORD_Y = 29
ROW_META_Y = 44
ORD_COL = 64             # ordinal column -> title column
# The right end of a row. The drawing fills it with a 76 px hairline rail on
# the ordinal's own line and leaves 84 px beyond it for the one word a row is
# allowed -- "Neu". Without the rail the right two thirds of every row is
# empty, which on a page with no other ornament reads as unfinished.
ROW_RAIL_W = 76
ROW_RAIL_H = 2
ROW_TAIL = 84            # rail's right edge -> the row's right edge
# Below this the rail goes and the title gets the room back. 150 was too low
# to be that promise: measured at 900x600 it left 216 px where their longest
# exercise, "Leere Saiten rauf und runter", needs 274, so every window under
# 1000 px traded a readable name for a progress hair. A title cut short is
# the piece the player cannot find; the rail is only how far the player got with it.
ROW_MIN_TITLE = 275

# The drawing stops its list after seven rows and leaves a hand's width of
# nothing between the last hairline and the key hints. Filling that gap with
# one more row is the difference between a page that was composed and a page
# that was packed.
LIST_FOOT = design.SPACE_12

FOOT_UP = 57             # the key hints' ink top, above the floor
HINT_SIZE = design.TEXT_XS
HINT_GAP = 24
HINT_KEY_GAP = 6
CARET_W = 2              # the typing bar, drawn because the face has none

FLOOR_F = 0.0569         # 41 px at 720 -- what the poster keeps below the disc
NUMERAL_F = 0.293        # of the panel: 150 px at a 512 px panel
DISC_F = 0.0879          # of the panel: a 45 px radius at 512
POSTER_TITLE = 34
POSTER_META = design.TEXT_SM
POSTER_LABEL = 14

# ── Type, placed by its ink rather than by its box ───────────────────────
# Everything above is an ink-top measured off the drawing, and a font surface
# carries leading above the ink that differs with the size. metrics() gives
# the exact rise of a glyph over the baseline, so the two are reconciled once
# per size instead of being eyeballed per call site.

_INK: dict[tuple, int] = {}


def _rise(size: int, weight: str, glyph: str) -> int:
    """How far `glyph` rises above the baseline at this size."""
    key = ("rise", size, weight, glyph)
    found = _INK.get(key)
    if found is None:
        face = design.font(size, face=get_look().face_sans or None,
                           weight=weight)
        metrics = face.metrics(glyph)
        found = metrics[0][3] if metrics and metrics[0] else face.get_ascent()
        _INK[key] = found
    return found


def _lead(size: int, weight: str, glyph: str) -> int:
    """The blank band above `glyph` in its own rendered surface."""
    key = ("lead", size, weight, glyph)
    found = _INK.get(key)
    if found is None:
        face = design.font(size, face=get_look().face_sans or None,
                           weight=weight)
        found = face.get_ascent() - _rise(size, weight, glyph)
        _INK[key] = found
    return found


def _text(surface, value, pos, colour, size, *, weight="regular",
          align="left", tracking=0.0, upper=False, glyph="H") -> pygame.Rect:
    """design.text, but `pos[1]` is where the ink starts, not the surface.

    Optical alignment is the whole game here: the ordinal, the title and the
    meta line in a row are three sizes that have to agree on where they sit,
    and three surface tops never do.
    """
    look = get_look()
    x, y = pos
    return design.text(surface, value, (x, y - _lead(size, weight, glyph)),
                       colour, size, face=look.face_sans or None,
                       weight=weight, align=align, tracking=tracking,
                       upper=upper)


def _width(value: str, size: int, weight: str = "regular",
           tracking: float = 0.0, upper: bool = False) -> int:
    return design.text_width(value, size, face=get_look().face_sans or None,
                             weight=weight, tracking=tracking, upper=upper)


# ── Tabular figures ──────────────────────────────────────────────────────
# Hanken's digits are nearly but not quite equal width -- the 4 is a pixel
# wider than the rest -- so a column of ordinals drifts. On a page whose only
# ornament is alignment that is visible, and it costs one dict lookup to fix.

_DIGIT_W: dict[tuple, int] = {}


def _digit_w(size: int, weight: str) -> int:
    key = (size, weight)
    found = _DIGIT_W.get(key)
    if found is None:
        found = max(_width(d, size, weight) for d in "0123456789")
        _DIGIT_W[key] = found
    return found


def _figures(surface, value, pos, colour, size, *, weight="regular",
             align="left", optical: bool = False) -> pygame.Rect:
    """Digits on a fixed pitch, so every ordinal lands on the same stems.

    `optical` pulls the first digit's own sidebearing back out, which is what
    the display numeral needs: a tabular 1 sits in the middle of its cell, so
    on a page aligned to one margin a "10" would stand a visible step right
    of the title under it while an "05" would not.
    """
    cell = _digit_w(size, weight)
    width = cell * len(value)
    x, y = pos
    if align == "right":
        x -= width
    elif align == "center":
        x -= width // 2
    if optical and value:
        face = design.font(size, face=get_look().face_sans or None,
                           weight=weight)
        metrics = face.metrics(value[0])
        bearing = metrics[0][0] if metrics and metrics[0] else 0
        x -= (cell - _width(value[0], size, weight)) // 2 + bearing
    for index, char in enumerate(value):
        _text(surface, char, (x + index * cell + cell // 2, y), colour, size,
              weight=weight, align="center", glyph="0")
    return pygame.Rect(x, y, width, _rise(size, weight, "0"))


# ── Circles ──────────────────────────────────────────────────────────────

def _disc(surface: pygame.Surface, centre: tuple[int, int], radius: int,
          fill: tuple[int, int, int], *,
          triangle: tuple[int, int, int] | None = None,
          bars: tuple[int, int, int] | None = None) -> None:
    """A smooth filled circle, optionally with a glyph cut out of it.

    `triangle` is play and `bars` is pause, both measured off the drawings
    as fractions of the radius so one call site serves a 10 px badge and a
    75 px transport button.

    Supersampled and cached. pygame.draw.circle has no antialiasing, and the
    one thing a stepped edge cannot look like is a machined one.
    """
    size = radius * 2 + 2
    if size <= 0:
        return

    def paint(layer: pygame.Surface) -> None:
        scale = 4
        big = _ghost((size * scale, size * scale), fill)
        cx = cy = size * scale // 2
        reach = radius * scale
        pygame.draw.circle(big, fill, (cx, cy), reach)
        if triangle is not None:
            pygame.draw.polygon(big, triangle, [
                (cx - int(reach * 0.24), cy - int(reach * 0.40)),
                (cx + int(reach * 0.46), cy),
                (cx - int(reach * 0.24), cy + int(reach * 0.40)),
            ])
        if bars is not None:
            width = max(1, int(reach * 0.133))
            high = max(1, int(reach * 0.552))
            gap = max(1, int(reach * 0.152))
            for side in (-1, 1):
                x = cx + side * (gap // 2 + (width if side > 0 else 0))
                pygame.draw.rect(big, bars, (x - (0 if side > 0 else width),
                                             cy - high // 2, width, high),
                                 border_radius=max(1, width // 3))
        pygame.transform.smoothscale(big, (size, size), layer)

    layer = design.sprite(("hifi-disc", size, fill, triangle, bars),
                          (size, size), paint)
    surface.blit(layer, (centre[0] - size // 2, centre[1] - size // 2))


def _ring(surface: pygame.Surface, rect: pygame.Rect,
          colour: tuple[int, int, int]) -> None:
    """A hairline pill outline, supersampled so the arcs do not stair-step."""
    if rect.width <= 0 or rect.height <= 0:
        return

    def paint(layer: pygame.Surface) -> None:
        scale = 4
        big = pygame.Surface((rect.width * scale, rect.height * scale),
                             pygame.SRCALPHA)
        pygame.draw.rect(big, colour, big.get_rect(), width=scale,
                         border_radius=rect.height * scale // 2)
        layer.blit(pygame.transform.smoothscale(big, rect.size), (0, 0))

    surface.blit(design.sprite(("hifi-ring", rect.size, colour), rect.size,
                               paint), rect.topleft)


# ── Strings that have to fit ─────────────────────────────────────────────

_FITTED: dict[tuple, tuple[str, ...]] = {}


def _wrap(value: str, size: int, weight: str, limit: int,
          lines: int = 2) -> tuple[str, ...]:
    """`value` broken over at most `lines`, the last one elided if it must be.

    Cached: this measures word by word and the poster title does not change
    between frames.
    """
    key = (value, size, weight, limit, lines)
    found = _FITTED.get(key)
    if found is not None:
        return found
    out: list[str] = []
    line = ""
    words = value.split()
    spilled = False
    for index, word in enumerate(words):
        probe = f"{line} {word}".strip()
        if line and _width(probe, size, weight) > limit:
            out.append(line)
            line = word
            if len(out) == lines:
                # The rest will not be shown, so the last line has to say so.
                # A title cut off at a word boundary reads as the whole title
                # and sends their looking for a piece that is not called that.
                spilled = True
                line = out[-1]
                out = out[:-1]
                break
        else:
            line = probe
    if len(out) < lines and line:
        out.append(line)
    if not out:
        out = [value]
    out[-1] = _elide(out[-1] + ("…" if spilled else ""), size, weight,
                     limit)
    result = tuple(out[:lines])
    if len(_FITTED) > 200:
        _FITTED.clear()
    _FITTED[key] = result
    return result


def _elide(value: str, size: int, weight: str, limit: int) -> str:
    return design.elide(value, lambda s: _width(s, size, weight) <= limit)


def _split_ordinal(title: str) -> tuple[str, str]:
    """("05", "Leere Saiten rauf und runter") out of one filed name.

    ("", the name) when it does not start with one.
    """
    head = title.split(" ", 1)
    if len(head) == 2 and head[0].isdigit() and 1 <= len(head[0]) <= 3:
        return f"{int(head[0]):02d}", head[1].strip()
    return "", title


def _numbered(song, index: int) -> tuple[str, str]:
    """(ordinal, title) for a piece, with the number said exactly once.

    Their exercises are filed as "05 Leere Saiten rauf und runter", so a row
    that sets an ordinal beside the title prints the number twice and they
    disagree the moment a filter is on -- the column would count the rows on
    screen while the title keeps the piece's own number. Taking it out of the
    title and putting it in the column is the only arrangement where the two
    cannot differ.
    """
    number, rest = _split_ordinal(song.display_title)
    return number or f"{index + 1:02d}", rest


# ── The page ─────────────────────────────────────────────────────────────

def library(surface: pygame.Surface, page: Page) -> None:
    look = get_look()
    width, height = page.width, page.height
    margin = max(18, frac(width, MARGIN_F))
    left_w, _ = split(width, SPLIT_F, minimum=PANEL_FLOOR)

    surface.fill(look.panel_right)
    if left_w:
        surface.fill(look.panel_left, (0, 0, left_w, height))
        # The rule is drawn, not implied by the panels meeting: two colours
        # nine points apart do not make an edge, they make a smudge.
        surface.fill(look.panel_rule, (left_w, 0, RULE_W, height))
        _poster(surface, page, look, pygame.Rect(0, 0, left_w, height), margin)

    body_x = left_w + RULE_W if left_w else 0
    body = pygame.Rect(body_x, 0, width - body_x, height)
    library_setup.button(surface, page)
    _field(surface, page, look, body, margin)
    if page.setup_open:
        library_setup.panel(surface, page)


# ── Left: one poster, almost entirely empty ──────────────────────────────

def _poster(surface: pygame.Surface, page: Page, look, panel: pygame.Rect,
            margin: int) -> None:
    inner = panel.width - margin * 2
    if inner <= 40:
        return
    head_y = frac(panel.height, HEAD_F)

    _text(surface, "PickHero", (margin, head_y), look.ink_1, HEAD_SIZE,
          weight="medium", tracking=WORDMARK_TRACK, upper=True)
    short = page.instrument_short or page.instrument_name
    if short:
        _text(surface, _elide(short.upper(), POSTER_LABEL - 1, "regular",
                              inner // 2),
              (panel.right - margin, head_y + 2), look.ink_3,
              POSTER_LABEL - 1, align="right", tracking=LABEL_TRACK)

    carry = page.carry
    song = record = None
    if carry is not None:
        song, record = carry
    elif page.rows and 0 <= page.selected < len(page.rows):
        song = page.rows[page.selected]
    if song is None:
        return

    try:
        number, title = _numbered(song, page.rows.index(song))
    except ValueError:
        number, title = _numbered(song, page.selected)

    floor = panel.height - frac(panel.height, FLOOR_F)
    disc_r = max(20, frac(panel.width, DISC_F))
    disc_cy = floor - disc_r
    disc_cx = margin + disc_r

    # -- the bottom block: one button, one rule --------------------------
    # "hero" plays the piece the player was in the middle of; with none to carry the
    # poster is showing the selected row instead, and that is what "play" is.
    hero_name = "hero" if carry is not None else "play"
    disc = pygame.Rect(disc_cx - disc_r, disc_cy - disc_r,
                       disc_r * 2, disc_r * 2)
    _disc(surface, (disc_cx, disc_cy), disc_r, look.ink_1,
          triangle=look.panel_left)

    rail_x = disc_cx + disc_r + design.SPACE_6
    rail_end = panel.right - margin
    rail_y = disc_cy - 13
    if rail_end - rail_x > 60:
        rail = pygame.Rect(rail_x, rail_y, rail_end - rail_x, 2)
        surface.fill(look.track_empty, rail)
        best = float(getattr(record, "best_accuracy", 0.0) or 0.0)
        if best > 0:
            filled = max(2, int(rail.width * min(1.0, best / 100.0)))
            surface.fill(look.ink_1, (rail.x, rail.y, filled, rail.height))
            _text(surface, t("hifi.best", percent=round(best)),
                  (rail.x, rail_y + 13), look.ink_3, design.TEXT_XS)
        _text(surface, t("key.enter"), (rail.right, rail_y + 13), look.ink_3,
              design.TEXT_XS, align="right")

    # -- stacked upward from the button ----------------------------------
    meta_parts = [t("song.bars", count=song.bars), t("song.bpm", bpm=song.bpm)]
    attempts = int(getattr(record, "attempts", 0) or 0)
    if attempts:
        meta_parts.append(t("menu.attempts", count=attempts))
    meta = _elide(" · ".join(meta_parts), POSTER_META, "regular", inner)

    title_lines = _wrap(title, POSTER_TITLE, "light", inner)
    numeral = max(40, frac(panel.width, NUMERAL_F))
    label = t("hifi.continue") if carry is not None else t("menu.play")

    meta_y = disc_cy - disc_r - 52
    title_y = meta_y - 46 - (len(title_lines) - 1) * 37
    num_y = title_y - 36 - _rise(numeral, "light", "0")
    label_y = num_y - 29

    # Nothing is drawn that would land on the wordmark. The numeral is the
    # one thing on the page with room to give, so it is what gives.
    guard = frac(panel.height, HEAD_F) + 30
    while numeral > 48 and label_y < guard:
        numeral -= 8
        num_y = title_y - 36 - _rise(numeral, "light", "0")
        label_y = num_y - 29
    if label_y >= guard:
        _text(surface, label, (margin, label_y), look.hud_accent,
              POSTER_LABEL, weight="medium", tracking=0.20, upper=True)
    if num_y >= guard - 12:
        _figures(surface, number, (margin, num_y), look.ink_1, numeral,
                 weight="light", optical=True)

    for index, line in enumerate(title_lines):
        _text(surface, line, (margin, title_y + index * 37), look.ink_1,
              POSTER_TITLE, weight="light")
    _text(surface, meta, (margin, meta_y), look.ink_3, POSTER_META)
    # The whole poster is the button, not just the disc: a page with one
    # thing on it should not ask them to find the small part of it that works.
    page.mark(hero_name, disc.union(
        pygame.Rect(margin, title_y - 8, inner, meta_y - title_y + 30)))


# ── Right: the tabs, the list, the hints ─────────────────────────────────

def _field(surface: pygame.Surface, page: Page, look, body: pygame.Rect,
           margin: int) -> None:
    left = body.x + margin
    right = body.right - margin
    if right - left < 120:
        return
    head_y = 76
    entries = _tab_entries(page)
    typed, typed_w = "", 0
    if page.search_active:
        typed = _elide(page.search_text, HEAD_SIZE, "regular", max(60, (right - left) // 3))
        typed_w = _width(typed, HEAD_SIZE) + CARET_W + 5
    labels, gap, void = _fit_header(entries, right - left, 0, typed_w)
    limit = right
    if typed_w:
        _search(surface, page, look, right, head_y, typed, typed_w)
        limit -= typed_w + void
    _tabs(surface, page, look, left, head_y, entries, labels, gap, limit)

    top = head_y + _rise(HEAD_SIZE, "regular", "H") + LIST_GAP
    # Whole rows only. A row sliced off by the footer is the one thing on
    # this page that would look unfinished rather than restrained, and the
    # drawing shows seven rows and a hand's width of nothing under them.
    room = body.height - FOOT_UP - LIST_FOOT - top
    _rows(surface, page, look,
          pygame.Rect(left, top, right - left,
                      max(0, room - room % ROW_H if room > 0 else 0)))
    _hints(surface, page, look, left, right, body.height - FOOT_UP)


def _tab_entries(page: Page) -> list[tuple[str, str, str, bool, tuple]]:
    """(with count, without count, weight, active, value) for shown filters.

    Only the piece-wide filters are offered here: every difficulty and every
    folder would be fourteen words across the top of a page whose whole
    argument is that it does not shout. The active one is always shown, even
    when it is one of the others.
    """
    shown = [f for f in page.filters if f[1][0] in ("all", "progress")]
    for entry in page.filters:
        if entry[3] and entry not in shown:
            shown.append(entry)
    return [(t("hifi.tab", label=label, count=count), label,
             "medium" if is_on else "regular", is_on, value)
            for label, value, count, is_on in shown]


def _fit_header(entries, room: int, pill_w: int,
                typed_w: int) -> tuple[int, int, int]:
    """Which labels the header carries, the air between them, and the void.

    The drawing is English and its four words are short; "Alle Stücke" and
    "Geschafft" are not. What gives first is the counts, which the list below
    already implies; then the air between the filters, down to a floor where
    four words still read as four words; then a filter, and `_tabs` never
    drops the one that is on. The void between the two groups is the last
    thing to give and never goes below `TAB_SPLIT`, because a left group that
    runs into its right group is a strip of words, and a strip of words is
    the thing this page is made of not having.
    """
    void = max(TAB_SPLIT, frac(room, TAB_SPLIT_F))
    fixed = pill_w + (typed_w + PILL_GAP if typed_w else 0)
    inner = max(1, len(entries) - 1)
    for form in (0, 1):
        total = sum(_width(e[form], HEAD_SIZE, e[2]) for e in entries)
        if total + inner * TAB_GAP + fixed + void <= room:
            return form, TAB_GAP, void
    total = sum(_width(e[1], HEAD_SIZE, e[2]) for e in entries)
    gap = (room - fixed - void - total) // inner
    return 1, max(design.SPACE_5, min(TAB_GAP, gap)), void


def _tabs(surface: pygame.Surface, page: Page, look, x: int, y: int,
          entries, form: int, gap: int, limit: int) -> None:
    """Filters as plain words. No chip, no box, no underline, no rule.

    When the row runs out of room the tail falls off it, except that the
    filter which is actually on never does: Tab cycles through every filter
    there is, including the folders, and a page that hides which one is on
    is a page that is lying about what it is showing.
    """
    kept, cursor = [], x
    for entry in entries:
        width = _width(entry[form], HEAD_SIZE, entry[2])
        if cursor + width > limit:
            break
        kept.append(entry)
        cursor += width + gap
    if not any(e[3] for e in kept):
        live = [e for e in entries if e[3]]
        if live:
            # Including when nothing at all fitted, which a long search query
            # can do: half the header goes to what the player typed, and at 1280 that
            # leaves 65 px where "Alle Stücke" needs 84. Drawing no filter at
            # all is the one outcome worse than drawing a cramped one.
            kept = kept[:-1] + [live[0]] if kept else [live[0]]

    for index, entry in enumerate(kept):
        label, weight, is_on = entry[form], entry[2], entry[3]
        # Every label here was measured to fit except the one kept by force
        # above, and that one has to be cut rather than allowed to run into
        # what the player is typing. On this page two words a hair apart read as one
        # word, which is worse than a word with its tail cut off.
        label = _elide(label, HEAD_SIZE, weight, max(0, limit - x))
        if not label:
            break
        width = _width(label, HEAD_SIZE, weight)
        _text(surface, label, (x, y), look.ink_1 if is_on else look.ink_3,
              HEAD_SIZE, weight=weight)
        kind, value = entry[4]
        page.mark(f"filter.{kind}.{value}",
                  pygame.Rect(x, y - 6, width, HEAD_SIZE + 12))
        x += width + gap


def _search(surface: pygame.Surface, page: Page, look, right: int, y: int,
            typed: str, width: int) -> None:
    """What the player has typed, and the bar after it. Only while the player is typing.

    The word "Suchen" used to sit here permanently, and in German it cost
    the header the one thing the drawing spends its width on -- the void
    between the filters and the pill. It is at the foot of the page now,
    beside the key that starts a search, with every other key; so the top
    line carries the filters and the one enclosed shape and nothing else.

    The bar is drawn rather than set: Hanken has no U+2502, and a missing
    glyph renders as the font's empty box -- which on this page would be the
    only rectangle outside the pill, and would be an accident.
    """
    x = right - width
    _text(surface, typed, (x, y), look.ink_1, HEAD_SIZE)
    surface.fill(look.ink_1, (right - CARET_W, y - 2, CARET_W,
                              _rise(HEAD_SIZE, "regular", "H") + 5))
    page.mark("search", pygame.Rect(x, y - 6, max(width, 24),
                                    HEAD_SIZE + 12))


def _rows(surface: pygame.Surface, page: Page, look, band: pygame.Rect) -> None:
    page.clamp_scroll(ROW_H, band.height)
    if band.height < ROW_H // 2:
        return
    if not page.rows:
        _text(surface, _elide(page.empty_title or t("menu.empty_search"),
                             design.TEXT_SM, "regular", band.width),
              (band.x, band.y + 30),
              look.ink_3, design.TEXT_SM)
        if page.empty_hint:
            _text(surface, _elide(page.empty_hint, design.TEXT_XS, "regular", band.width),
                  (band.x, band.y + 57), look.ink_3, design.TEXT_XS)
        return

    clip = surface.get_clip()
    surface.set_clip(band.clip(surface.get_rect()))
    first = max(0, page.scroll_px // ROW_H)
    last = min(len(page.rows), (page.scroll_px + band.height) // ROW_H + 1)
    for index in range(first, last):
        top = band.y + index * ROW_H - page.scroll_px
        _row(surface, page, look, band, top, index)
    surface.set_clip(clip)

    # The closing hairline, so the list ends on a line rather than on air.
    end = band.y + len(page.rows) * ROW_H - page.scroll_px
    if band.y <= end <= band.bottom:
        surface.fill(look.hairline, (band.x, end, band.width, 1))


def _row(surface: pygame.Surface, page: Page, look, band: pygame.Rect,
         top: int, index: int) -> None:
    song = page.rows[index]
    selected = index == page.selected
    surface.fill(look.hairline, (band.x, top, band.width, 1))

    title_x = band.x + ORD_COL
    tail = band.right
    score = page.scores.get(index)

    # The rail says how far the player got without saying a number: a filled hair on
    # a page whose only other mark is a filled hair, under the play button.
    # A percentage per row would be eight numbers nobody reads, and the one
    # that matters is already set at 150 px on the poster.
    rail_x = band.right - ROW_TAIL - ROW_RAIL_W
    if rail_x - title_x >= ROW_MIN_TITLE:
        rail_y = top + ROW_ORD_Y + _rise(ROW_ORD, "light", "0") // 2
        surface.fill(look.track_empty,
                     (rail_x, rail_y, ROW_RAIL_W, ROW_RAIL_H))
        if score:
            filled = max(ROW_RAIL_H, int(ROW_RAIL_W * min(1.0, score / 100.0)))
            # ink_3 rather than the poster's ink_1. Seven rails at title
            # weight make a second column of marks down the page and the eye
            # reads them before the titles; one step quieter and they are a
            # measure the player can glance at, which is all they are for.
            surface.fill(look.ink_3, (rail_x, rail_y, filled, ROW_RAIL_H))
        tail = rail_x - design.SPACE_6
    if score is None:
        _text(surface, t("menu.progress_new"), (band.right, top + ROW_ORD_Y),
              look.ink_3, ROW_ORD, align="right")
        if tail == band.right:
            tail = band.right - _width(t("menu.progress_new"), ROW_ORD) \
                - design.SPACE_6

    number, title = _numbered(song, index)
    _figures(surface, number, (band.x, top + ROW_ORD_Y), look.ink_3, ROW_ORD,
             weight="light")
    _text(surface, _elide(title, ROW_TITLE, ROW_TITLE_W, tail - title_x),
          (title_x, top + ROW_TITLE_Y),
          look.hud_accent if selected else look.ink_1, ROW_TITLE,
          weight=ROW_TITLE_W)

    meta = f"{song.level_name} · {t('song.bpm', bpm=song.bpm)}"
    _text(surface, _elide(meta, ROW_META, "regular", tail - title_x),
          (title_x, top + ROW_META_Y), look.ink_3, ROW_META)
    visible = pygame.Rect(band.x, top, band.width, ROW_H).clip(surface.get_clip())
    if visible.height > 0:
        page.mark_row(visible, index)


def _hints(surface: pygame.Surface, page: Page, look, left: int, right: int,
           y: int) -> None:
    pairs = (
        # The drawing writes the two arrows rather than the word for them,
        # and this is the one place the look cannot follow it: Hanken's
        # metrics() answers for U+2191 and U+2193, but it draws them as two
        # empty boxes -- rendered, looked at, and that is what they are. The
        # word is the version the player can read.
        (t("key.arrows"), t("hifi.select"), ""),
        # "Suchen" came off the top line so the header could keep the void
        # the drawing spends its width on. It is written down here, beside
        # the key that starts it, which is where this page writes down every
        # other key -- and it is second, so a narrow window drops the setup
        # hint before it, and the setup hint is the one the pill repeats.
        (t("key.f"), t("hifi.search"), "search"),
        (t("key.n"), page.sort_label or t("menu.sort"), "sort"),
        (t("key.o"), t("menu.folder"), "folder"),
        (t("key.w"), t("menu.suggest"), "suggest"),
        (t("key.s"), t("menu.setup"), "setup"),
    )
    x = left
    for key, label, name in pairs:
        key_w = _width(key, HINT_SIZE)
        label_w = _width(label, HINT_SIZE)
        width = key_w + HINT_KEY_GAP + label_w
        if x + width > right:
            return
        _text(surface, key, (x, y), look.ink_3, HINT_SIZE)
        _text(surface, label, (x + key_w + HINT_KEY_GAP, y), look.ink_3,
              HINT_SIZE)
        if name:
            page.mark(name, pygame.Rect(x, y - 6, width, HINT_SIZE + 12))
        x += width + HINT_GAP


# ── The setup card ───────────────────────────────────────────────────────
# Beside the button that opens it, not in the middle of a darkened screen:
# nothing about this page wants a scrim over it.
#
# Four rows, each one a small tracked label with the answer set large under
# it. That is what the drawing shows and it is the whole difference between
# a card and a settings list: the label is the question, which the player reads
# once, and the value is the answer, which is the thing the player came to look at.
# The keys the four rows cannot carry go on one hint line at the card's
# floor, in the same shape the page already uses at its own floor, because a
# key that works but is written down nowhere is a key the player does not have.

# ── The play page ────────────────────────────────────────────────────────
# One sheet of paper with the whole piece on it. Nothing is a panel, nothing
# is a card and nothing has an edge: the page is one field of the look's own
# paper from corner to corner, and every mark on it is either a hairline, a
# disc or a word. A card inset on a desk was tried and was wrong -- it reads
# as a dialog, it costs a margin on all four sides, and the library page in
# this same look is full bleed, so the two halves disagreed about what the
# window was.
#
# Every number below was measured off scratchpad/skins/seite6.png, an
# 1867x1225 sheet whose own screen frame is x60-1809 by y118-1164 and stands
# for the 1280x720 window: a sheet pixel is 0.732 of a window pixel. That
# frame is the window, not an object in it -- seite5 draws the library inside
# the same frame and fills it edge to edge. Four independent measurements
# agree once it is read that way: the margin is 50 px, the stave's right end
# 1229, the playhead 331 and the bar pitch 495.
#
# Horizontal distance carries over directly. Vertical does not: the window is
# 720 px tall against the sheet frame's 765, so the header hangs from the top
# edge, the footer stands on the floor, and the stave is whatever is left
# between them. That is the only part of the drawing that can give, and
# giving it is what keeps the type the size it was drawn at.
#
# WHAT THE NOTES NEED FROM HERE
# scrolling.py owns the notes. It does not own where they go, because where
# they go is the stave this file drew. Everything it needs is a module-level
# function, all of them pure and cheap enough to call per note per frame:
#
#   board(page)                      the rectangle notes live in
#   lane_y(page, string, strings)    the y of one string's line, 1 = highest
#   note_box(page, string, strings, x, w)   the disc a note is drawn in
#   note_size(page, strings)         that disc's diameter
#   playhead_x(page)                 where "now" is, in x
#   time_x(page, ms)                 where a moment in the piece is, in x
#   note(surface, box, label, ...)   and the house drawing of one
#
# time_x is the one that has to be shared rather than reinvented: this file
# draws the bar lines with it, so a caller that maps time to x its own way
# puts the notes somewhere the bar lines are not.

# The window at 1280x720, which is what the sheet's vertical measurements
# mean. Below it the blocks and the type come down together: a header at full
# size over a 200 px stave is not the drawing at a smaller size, it is a
# different and worse one.
PLAY_REF_H = 720
PLAY_ZOOM_MIN = 0.72

# -- the header, measured down from the card's top edge -------------------
HEAD_EYEBROW = 26        # the small tracked caps over the title
HEAD_TITLE = 48          # the title's ink
HEAD_CLOCK = 27          # leave a separate row below for setup and help
HEAD_BAR = 34            # "Takt 1 / 8" stands this far left of the clock
HEAD_SCORE = 82          # the quiet line under the title, once the player has played
# The drawing leaves 30 px between the title and the first hairline and has
# nothing in it. The score line goes there, so the header is ten px taller
# than the sheet -- paid for out of the stave, which is the one block with
# room to give, and not out of the air around the title, which is the look.
STAVE_HEAD = 108         # the first hairline

# -- the footer, measured up from the card's floor ------------------------
STAVE_FOOT = 155         # the second hairline
FOOT_LABEL = 109         # TEMPO and EINGANG, and the toggle dots ride on it
FOOT_VALUE = 87          # 100 %, the toggle words, the meter's top
FOOT_HINT = 57           # the keycaps and the two key hints
FOOT_MID = 78            # the centre line of the three round controls
DIV_HEAD = 108           # the one vertical hairline in the footer
DIV_FOOT = 47

# -- the stave ------------------------------------------------------------
# The four lines are not centred in the band: there is two thirds of a gap
# over the top string and a gap and a quarter under the bottom one, which is
# what stops the stave reading as a table. Stated as multiples of the gap so
# a six-string instrument gets the same drawing with six lines in it.
LANE_HEAD = 0.664
LANE_FOOT = 1.243
GUTTER_F = 0.0354        # 44 px at 1280: the margin to where the lines start
GUTTER_MIN = 24
GUTTER_SIZE = 22         # the string letters, and they are the whole gutter
PLAYHEAD_F = 0.207       # of the board: where now is
PLAYHEAD_W = 2
PLAYHEAD_OVER = 7        # how far it runs past the two hairlines
BARS_VISIBLE = 2.285     # how much of the piece the board shows at once

# -- the notes ------------------------------------------------------------
NOTE_D = 50              # the disc at 1280x720
NOTE_LANE_F = 0.60       # ... and never more than this much of a lane gap
NOTE_SHADOW = 11
NOTE_DIGIT_F = 0.38      # the fret number, as a share of the disc
HALO_F = 1.33            # the accent note's halo, as a share of the disc
HALO_ALPHA = 46          # measured: the accent at 18 % over the card

# -- type -----------------------------------------------------------------
PLAY_LABEL = 12          # the footer's tracked caps: TEMPO, EINGANG
# The header's two tracked labels are a step larger than the footer's -- the
# drawing sets them at a 10.2 px cap against the footer's 8.8, and "BAR 1 / 8"
# measures 72 px wide, which is 13 to the pixel. One size for all four made
# the top of the page quieter than its bottom.
HEAD_LABEL = 13
PLAY_TRACK = 0.14
# The title's cap measures 25.6 px in the drawing, which is a light 35; its
# string measures 342, which is a light 30. The drawn face is narrower than
# Hanken, so neither number can be had on its own and 32 is the one that is
# wrong by least in both directions. 28 was wrong in one only -- six points
# short on the largest word on the page.
PLAY_TITLE = 32
PLAY_CLOCK = 32
PLAY_VALUE = 26          # 100 %
PLAY_WORD = 15           # a toggle's word: an 11 px cap, "Metronome" at 76
PLAY_SMALL = 12          # a keycap, a key hint, the line under the title

# -- the footer's horizontal rhythm, measured ------------------------------
RESTART_R = 24
PAUSE_R = 37
KNOB_R = 38
GAP_RESTART = 17         # restart -> pause
GAP_PAUSE = 41           # pause -> knob
GAP_KNOB = 16            # knob -> TEMPO
GAP_TEMPO = 43           # TEMPO -> the hairline
GAP_DIV = 41             # the hairline -> the first toggle
GAP_TOGGLE = 32          # one toggle -> the next
DOT_R = 6                # the toggle's dot
METER_W = 40
METER_H = 19
METER_BARS = 7
HINT_DOT = "·"      # between the two key hints, as the drawing has it
HINT_SPLIT = 4           # measured: 11 px of air holding a 3 px dot


# ── Geometry, which is the public half of this page ──────────────────────
# Built from the page size alone and cached on it, because board() and
# lane_y() are called once per note per frame by code that is not in this
# file and must not have to think about what it costs.

class _Shape(NamedTuple):
    card: pygame.Rect       # the paper, which is the whole window
    zoom: float             # what the drawing's own numbers are multiplied by
    left: int               # the one margin, which is also the text column
    right: int
    head: int               # the first hairline
    foot: int               # the second
    board: pygame.Rect      # where the notes go


_SHAPES: dict[tuple[int, int], _Shape] = {}


def _shape(page: Page) -> _Shape:
    key = (page.width, page.height)
    found = _SHAPES.get(key)
    if found is not None:
        return found
    card = pygame.Rect(0, 0, max(40, page.width), max(40, page.height))
    zoom = max(PLAY_ZOOM_MIN, min(1.0, card.height / PLAY_REF_H))
    margin = max(12, frac(card.width, MARGIN_F))
    left = card.x + margin
    right = card.right - margin
    head = card.y + int(round(STAVE_HEAD * zoom))
    foot = card.bottom - int(round(STAVE_FOOT * zoom))
    # A window short enough that the two hairlines would cross takes it out
    # of the stave, which is the only block with room to lose: the header and
    # the footer are type, and type that overlaps is unreadable where a thin
    # stave is merely thin.
    if foot - head < 60:
        middle = (head + foot) // 2
        head, foot = middle - 30, middle + 30
    gutter = max(GUTTER_MIN, frac(card.width, GUTTER_F))
    board = pygame.Rect(left + gutter, head, max(40, right - left - gutter),
                        max(1, foot - head))
    found = _Shape(card, zoom, left, right, head, foot, board)
    if len(_SHAPES) > 8:
        _SHAPES.clear()
    _SHAPES[key] = found
    return found


def board(page: Page) -> pygame.Rect:
    """The rectangle the notes live in: right of the gutter, between the rules.

    Its top and bottom are the two hairlines, not the outermost strings, so a
    note sitting on the top string has the drawing's air above it.
    """
    return _shape(page).board


def lane_y(page: Page, string: int, strings: int) -> int:
    """The y of one string's line. `string` is 1 for the highest, as the app
    numbers them everywhere else -- so on their bass ukulele 1 is G and 4 is E,
    and 1 is the line at the top of the stave.
    """
    band = _shape(page).board
    strings = max(1, strings)
    gap = band.height / (strings - 1 + LANE_HEAD + LANE_FOOT)
    index = min(max(string, 1), strings) - 1
    return int(round(band.y + gap * (LANE_HEAD + index)))


def lane_gap(page: Page, strings: int) -> int:
    """The distance between two string lines."""
    band = _shape(page).board
    return int(round(band.height
                     / (max(1, strings) - 1 + LANE_HEAD + LANE_FOOT)))


def note_size(page: Page, strings: int) -> int:
    """The diameter of a note's disc.

    One size for every note on the stave. A held note is not a bigger note
    here -- the drawing says duration with where the next disc falls, which
    is how a stave has always said it.
    """
    return max(14, min(max(32, int(round(NOTE_D * _shape(page).zoom))),
                       int(lane_gap(page, strings) * 0.8)))


def note_box(page: Page, string: int, strings: int, x: int,
             w: int = 0) -> pygame.Rect:
    """The square a note is drawn in, centred on its attack at `x`.

    `w` is the width the timeline gave the note for its duration, and this
    look does not draw it: see note_size. It is in the signature because the
    other two looks need it and a caller should not have to ask which look it
    is talking to.
    """
    size = note_size(page, strings)
    if w > 0:
        size = min(size, max(1, int(w)))
    centre = lane_y(page, string, strings)
    return pygame.Rect(int(x) - size // 2, centre - size // 2, size, size)


def playhead_x(page: Page) -> int:
    """Where now is. A fifth of the way across, so most of the board is what
    is coming rather than what has gone."""
    band = _shape(page).board
    return band.x + int(round(band.width * PLAYHEAD_F))


def visible_ms(page: Page) -> float:
    """How much of the piece the board shows at once.

    Measured off the drawing as two and a third bars, which at them 60 BPM is
    about nine seconds -- long enough to see the next phrase coming, short
    enough that a note crosses the board slowly enough to aim at.
    """
    return page.visible_window_ms if page.timeline_markers else _bar_ms(page) * BARS_VISIBLE


def time_x(page: Page, ms: float) -> int:
    """Where a moment of the piece falls on the board.

    This is the mapping the bar lines are drawn with, so it is the mapping
    the notes have to be drawn with.
    """
    band = _shape(page).board
    span = max(1.0, visible_ms(page))
    width = band.right - playhead_x(page) if page.timeline_markers else band.width
    return playhead_x(page) + int(round(
        (ms - page.elapsed_ms) * width / span))


def _bar_ms(page: Page) -> float:
    """How long one bar lasts, in ms.

    From the piece's own length where there is one, because that already has
    every tempo change and the tempo slider in it; from the BPM otherwise,
    which assumes four beats and is only ever used before a piece is loaded.
    """
    if page.total_ms > 0 and page.bars > 0:
        return page.total_ms / page.bars
    if page.bpm > 0:
        return 60000.0 / page.bpm * 4
    return 2000.0


# ── The round things ─────────────────────────────────────────────────────
# This page is discs on paper, and pygame has no antialiased disc. Every one
# of them is drawn four times oversize and smoothscaled down once, then kept
# -- a stepped edge on a 50 px circle is the single cheapest-looking mark a
# page can carry, and on a page with nothing else on it, it is the page.

def _outline(surface: pygame.Surface, centre: tuple[int, int], radius: int,
             colour: tuple[int, int, int], width: int = 2) -> None:
    """A smooth circle outline: the unplayed note, the toggle that is off.

    Two discs rather than one stroke, and `_ghost` rather than a clear
    surface. pygame's thick circle outline walks concentric aliased circles,
    which leaves the stroke a third heavier on the diagonals than on the
    axes -- measured at 168 against 199 on a 24 px ring, which at a glance
    is a dotted line. Filling the outer disc and punching the inner one out
    gives an edge that is the same weight the whole way round.
    """
    size = radius * 2 + width * 2 + 2
    if size <= 0:
        return

    def paint(layer: pygame.Surface) -> None:
        scale = 4
        big = _ghost((size * scale, size * scale), colour)
        middle = size * scale // 2
        pygame.draw.circle(big, colour, (middle, middle), radius * scale)
        pygame.draw.circle(big, (*colour, 0), (middle, middle),
                           max(1, (radius - width) * scale))
        pygame.transform.smoothscale(big, (size, size), layer)

    layer = design.sprite(("hifi-outline", size, radius, colour, width),
                          (size, size), paint)
    surface.blit(layer, (centre[0] - size // 2, centre[1] - size // 2))


def _ghost(size: tuple[int, int],
           colour: tuple[int, int, int]) -> pygame.Surface:
    """A transparent scratch surface already carrying `colour`.

    smoothscale averages the four channels independently, so a clear
    SRCALPHA surface -- which is transparent *black* -- drags every edge
    pixel towards black as it is averaged with the shape. On paper that is
    a grey fringe around every disc on the page. Starting from the shape's
    own colour at zero alpha makes the average right by construction, which
    is the cheap half of premultiplied alpha.
    """
    layer = pygame.Surface(size, pygame.SRCALPHA)
    layer.fill((*colour, 0))
    return layer


def _shadow(surface: pygame.Surface, centre: tuple[int, int], radius: int,
            spread: int, alpha: int = 26) -> None:
    """What lifts a disc off the paper. Drawn under it, and a little below.

    Nested discs of rising alpha rather than a blur, which is the measurement
    shapes.glow already made: a real blur at this size costs twenty times as
    much and at a 12 px spread nobody can tell the two apart.
    """
    size = (radius + spread) * 2 + 4
    if size <= 0:
        return

    def paint(layer: pygame.Surface) -> None:
        middle = size // 2
        for step in range(spread, 0, -1):
            a = int(alpha * (1.0 - step / spread) ** 1.6)
            if a <= 0:
                continue
            pygame.draw.circle(layer, (0, 0, 0, a),
                               (middle, middle + max(1, spread // 3)),
                               radius + step)

    layer = design.sprite(("hifi-shadow", size, radius, spread, alpha),
                          (size, size), paint)
    surface.blit(layer, (centre[0] - size // 2, centre[1] - size // 2))


def _restart(surface: pygame.Surface, centre: tuple[int, int], radius: int,
             ring: tuple[int, int, int], ink: tuple[int, int, int]) -> None:
    """The small outlined button with the turn-again arrow in it.

    The arrow is drawn rather than set: the bundled faces carry no U+21BA,
    and a missing glyph renders as the font's empty box -- which on this page
    would be the only rectangle anywhere.
    """
    size = radius * 2 + 4
    if size <= 0:
        return

    def paint(layer: pygame.Surface) -> None:
        scale = 4
        big = _ghost((size * scale, size * scale), ring)
        middle = size * scale // 2
        pygame.draw.circle(big, ring, (middle, middle), radius * scale)
        pygame.draw.circle(big, (*ring, 0), (middle, middle),
                           max(1, radius * scale - int(scale * 1.5)))
        # Measured: a 15 px mark inside a 67 px ring, so it is a quarter of
        # the ring and not a third. Three quarters of a circle with the gap
        # at the upper left, and a head small enough to still be a hairline.
        inner = max(2, int(radius * scale * 0.26))
        box = pygame.Rect(middle - inner, middle - inner, inner * 2, inner * 2)
        pygame.draw.arc(big, ink, box, math.radians(-150), math.radians(140),
                        max(1, int(scale * 1.1)))
        angle = math.radians(140)
        point = (middle + inner * math.cos(angle),
                 middle - inner * math.sin(angle))
        run = (-math.sin(angle), -math.cos(angle))
        side = (math.cos(angle), -math.sin(angle))
        head = max(2.0, inner * 0.78)
        pygame.draw.polygon(big, ink, [
            (point[0] + run[0] * head, point[1] + run[1] * head),
            (point[0] + side[0] * head * 0.55 - run[0] * head * 0.2,
             point[1] + side[1] * head * 0.55 - run[1] * head * 0.2),
            (point[0] - side[0] * head * 0.55 - run[0] * head * 0.2,
             point[1] - side[1] * head * 0.55 - run[1] * head * 0.2),
        ])
        pygame.transform.smoothscale(big, (size, size), layer)

    layer = design.sprite(("hifi-restart", size, radius, ring, ink),
                          (size, size), paint)
    surface.blit(layer, (centre[0] - size // 2, centre[1] - size // 2))


def _knob(surface: pygame.Surface, centre: tuple[int, int], radius: int,
          fraction: float, look) -> None:
    """The tempo dial, with its own shadow, supersampled once and kept.

    shapes.knob draws it; what is here is the part the shape cannot do for
    itself, which is to not be made of stairs. The fraction is quantised into
    the sprite key, so turning the dial costs one new sprite per step and
    nothing at all per frame.
    """
    step = max(0.0, min(1.0, round(fraction * 40) / 40))
    size = radius * 2 + 12
    if size <= 0:
        return

    def paint(layer: pygame.Surface) -> None:
        scale = 3
        big = _ghost((size * scale, size * scale), look.popover)
        middle = size * scale // 2
        spread = 5 * scale
        for drop in range(spread, 0, -1):
            a = int(30 * (1.0 - drop / spread) ** 1.6)
            if a > 0:
                pygame.draw.circle(big, (0, 0, 0, a),
                                   (middle, middle + scale * 2),
                                   radius * scale + drop)
        shapes.knob(big, (middle, middle), radius * scale, step,
                    face=look.popover, edge=look.hairline,
                    marker=look.hit_zone)
        pygame.transform.smoothscale(big, (size, size), layer)

    layer = design.sprite(("hifi-knob", size, radius, step, look.key),
                          (size, size), paint)
    surface.blit(layer, (centre[0] - size // 2, centre[1] - size // 2))


def note(surface: pygame.Surface, box: pygame.Rect, label: str, *,
         fill: tuple[int, int, int] | None = None,
         ink: tuple[int, int, int] | None = None,
         halo: bool = False, gone: bool = False) -> None:
    """One note, drawn the way this look draws notes. For scrolling.py.

    A solid near-black disc with the fret number in white and a soft shadow
    under it; the one at the playhead in the accent with a flat halo behind
    it; one that has gone by unplayed as an outline with a quiet digit. The
    verdict colours are the caller's to pass -- what belongs here is the
    shape, not the opinion.
    """
    look = get_look()
    radius = min(box.width, box.height) // 2
    if radius <= 2:
        return
    centre = box.center
    size = max(9, int(round(radius * 2 * NOTE_DIGIT_F)))
    if gone:
        _outline(surface, centre, radius - 1, look.hairline_loud, 2)
        _text(surface, label, (centre[0],
                               centre[1] - _rise(size, "regular", "0") // 2),
              look.ink_3, size, align="center", glyph="0")
        return
    if halo:
        # The halo and the accent are one idea -- the note that is due now --
        # so asking for one gets the other. A near-black disc inside an
        # orange halo would be two marks saying different things.
        _halo(surface, centre, radius)
        fill = fill or look.hit_zone
    else:
        # NOTE_SHADOW is the spread on a disc of NOTE_D, and it travels with
        # the disc rather than being restated as a fraction here -- the two
        # had drifted apart, with the constant declared and never read.
        _shadow(surface, centre, radius,
                max(4, int(round(radius * 2 * NOTE_SHADOW / NOTE_D))))
    _disc(surface, centre, radius, fill or look.note_fill or look.ink_1)
    _text(surface, label, (centre[0],
                           centre[1] - _rise(size, "medium", "0") // 2),
          ink or look.note_ink or look.panel_right, size, weight="medium",
          align="center", glyph="0")


def _halo(surface: pygame.Surface, centre: tuple[int, int],
          radius: int) -> None:
    """The flat disc of accent behind the note at the playhead.

    Flat, not a glow: a gradient here would be the one soft-edged thing on a
    page made entirely of hard ones, and it would read as a mistake.
    """
    look = get_look()
    outer = max(radius + 3, int(round(radius * HALO_F)))
    size = outer * 2 + 2

    def paint(layer: pygame.Surface) -> None:
        scale = 4
        big = _ghost((size * scale, size * scale), look.hit_zone)
        pygame.draw.circle(big, design.with_alpha(look.hit_zone, HALO_ALPHA),
                           (size * scale // 2, size * scale // 2),
                           outer * scale)
        pygame.transform.smoothscale(big, (size, size), layer)

    layer = design.sprite(("hifi-halo", size, outer, look.hit_zone),
                          (size, size), paint)
    surface.blit(layer, (centre[0] - size // 2, centre[1] - size // 2))


# ── The page ─────────────────────────────────────────────────────────────

def play(surface: pygame.Surface, page: Page) -> None:
    """The playing screen: a sheet of paper, a stave on it, and two controls.

    Everything that does not change between frames -- the paper, the two
    hairlines, the string lines and the letters in the gutter -- is one
    cached surface and one blit. What is left per frame is the bar lines,
    which move, and the type, which counts.
    """
    look = get_look()
    shape = _shape(page)
    letters = _letters(page)
    strings = len(letters) or 4

    surface.blit(_chrome(page, look, letters, strings), (0, 0))
    _bar_lines(surface, page, look, shape)
    _playhead(surface, look, shape)
    _head(surface, page, look, shape)
    _foot(surface, page, look, shape)
    page.mark("board", shape.board)
    if page.banner or getattr(page, "loop_status", ""):
        _banner(surface, page, look, shape, strings)


_CHROME: dict[tuple, pygame.Surface] = {}
_CHROME_HOOK = False


def _drop_chrome() -> None:
    """Let go of the page surfaces. Called by pygame on the way down."""
    global _CHROME_HOOK
    _CHROME.clear()
    _CHROME_HOOK = False


def _chrome(page: Page, look, letters: tuple[str, ...],
            strings: int) -> pygame.Surface:
    """Paper, stave and gutter: the part of the page that is furniture.

    It costs one fill, six rules and four letters to build, and one opaque
    blit to show. Keyed by everything that can change it, which is the
    window, the look and the tuning -- not the piece, and not the clock.

    Kept here rather than in design.sprite, which is the only cache in this
    file that is not. That one holds 240 entries before it clears, which is
    right for a 50 px disc and wrong for a full window: a resize drag makes
    a new key every frame, and two hundred and forty pages at three and a
    half megabytes each is most of a gigabyte. Two is the whole need -- the
    window the player has, and the one the player is dragging towards. The quit hook is
    design.font's, for its own reason: pygame.quit() frees the surface under
    a cached Surface and blitting one afterwards is an access violation
    rather than an exception.
    """
    global _CHROME_HOOK
    if not _CHROME_HOOK:
        pygame.register_quit(_drop_chrome)
        _CHROME_HOOK = True
    size = (page.width, page.height)
    shape = _shape(page)
    key = (size, look.key, strings, letters)
    found = _CHROME.get(key)
    if found is not None:
        return found
    if len(_CHROME) >= 2:
        _CHROME.clear()

    layer = pygame.Surface(size)
    layer.fill(look.panel_right)
    width = shape.right - shape.left
    if width > 0:
        # The two rules that hold the stave. They run the full width of the
        # margin, past where the string lines start, so the gutter letters
        # hang inside the stave rather than beside it.
        layer.fill(look.hairline, (shape.left, shape.head, width, 1))
        layer.fill(look.hairline, (shape.left, shape.foot, width, 1))
        band = shape.board
        for index in range(strings):
            y = lane_y(page, index + 1, strings)
            # A string is a mid warm grey, not a hairline: it is the thing
            # the notes sit on, and the two rules and the bar lines are the
            # paper it is ruled on. One step of weight is the difference.
            layer.fill(look.stage_bar_line, (band.x, y, band.width, 2))
        _gutter(layer, page, look, shape, letters, strings)
    _CHROME[key] = layer
    return layer


def _gutter(surface: pygame.Surface, page: Page, look, shape: _Shape,
            letters: tuple[str, ...], strings: int) -> None:
    """The string letters, each in its own string's colour. Nothing else.

    No octave, no number, no box. The raw palette is equiluminant and
    measures 1.4 to 1 on paper, so each letter is walked towards the page's
    own ink until it reads -- which keeps the hue, and the hue is the whole
    reason the letter is coloured at all.
    """
    size = max(12, int(round(GUTTER_SIZE * shape.zoom)))
    for index, letter in enumerate(letters[:strings]):
        colour = design.readable(design.string_colour(index + 1),
                                 look.panel_right)
        _text(surface, letter,
              (shape.left, lane_y(page, index + 1, strings)
               - _rise(size, "regular", "H") // 2),
              colour, size)


def _letters(page: Page) -> tuple[str, ...]:
    """The open-string names, highest first, or nothing if the page has none.

    The tuning reaches a painter as a label -- "E A D G", lowest first, the
    way a tuner says it -- so the drawing's order is that one reversed. A
    page that carries no tuning gets a stave with no letters rather than a
    guess: a gutter that says G D A E over a guitar would be the one thing
    on this screen that is confidently wrong.
    """
    # The screen states this outright now. What follows it is the fallback
    # for a page that does not -- parsing the tuning back out of a label was
    # the only way to know before Page carried it, and it is still the only
    # way for a caller that builds a Page by hand.
    if page.string_labels:
        return tuple(letter for _number, letter, _name in page.string_labels)
    for source in (page.tuning_label, _tail(page.subtitle)):
        tokens = [piece for piece in source.replace(",", " ").split() if piece]
        if 3 <= len(tokens) <= 8 and all(len(piece) <= 3 for piece in tokens):
            return tuple(reversed(tokens))
    return ()


SEPARATORS = (" - ", " – ", " — ", " · ", " | ")


def _tail(value: str) -> str:
    for mark in SEPARATORS:
        if mark in value:
            return value.rsplit(mark, 1)[1]
    return ""


def _lead_part(value: str) -> str:
    for mark in SEPARATORS:
        if mark in value:
            return value.split(mark, 1)[0]
    return value


# ── The stave's moving parts ─────────────────────────────────────────────

def _bar_lines(surface: pygame.Surface, page: Page, look,
               shape: _Shape) -> None:
    """One hairline at each bar, drawn with the same mapping as the notes."""
    band = shape.board
    if page.timeline_markers:
        for beat in page.timeline_beats:
            if not beat.accent:
                continue
            x = time_x(page, beat.timestamp_ms)
            if band.left <= x <= band.right:
                surface.fill(look.hairline, (x, shape.head + 1, 1, shape.foot - shape.head - 1))
        return
    span = _bar_ms(page)
    if span <= 0 or band.width <= 0:
        return
    first = int((page.elapsed_ms - visible_ms(page)) // span)
    last = int((page.elapsed_ms + visible_ms(page)) // span) + 1
    top, bottom = shape.head + 1, shape.foot
    # Bar zero is the start of the piece and bar `bars` is the end of it.
    # Nothing is ruled past that: the stave carries on to the right because
    # the drawing is a stave, not because there is more piece out there.
    for index in range(max(0, first), min(last, max(1, page.bars)) + 1):
        x = time_x(page, index * span)
        if band.x <= x <= band.right:
            # The same hairline as the two rules that close the stave, which
            # is what the drawing uses: measured, all four are #D4D1C9. A
            # louder step made the bar rules the strongest marks on an empty
            # stave, so the page read as a table with the strings drawn in it.
            surface.fill(look.hairline, (x, top, 1, bottom - top))


def _playhead(surface: pygame.Surface, look, shape: _Shape) -> None:
    """The one orange line, and the only thing on the page that says now."""
    x = shape.board.x + int(round(shape.board.width * PLAYHEAD_F))
    over = int(round(PLAYHEAD_OVER * shape.zoom))
    surface.fill(look.hit_zone,
                 (x - PLAYHEAD_W // 2, shape.head - over, PLAYHEAD_W,
                  shape.foot - shape.head + over * 2))


# ── The header ───────────────────────────────────────────────────────────

def _head(surface: pygame.Surface, page: Page, look, shape: _Shape) -> None:
    z = shape.zoom
    label = max(10, int(round(HEAD_LABEL * z)))
    title_size = max(15, int(round(PLAY_TITLE * z)))
    clock_size = max(16, int(round(PLAY_CLOCK * z)))
    small = max(10, int(round(PLAY_SMALL * z)))

    # -- the right end first, because it decides what the title has ------
    # One line: which bar the player is in, then how far through the piece the player is.
    # The clock is two colours and therefore two draws -- what has run in the
    # page's own ink, what is left of the piece a step quieter. ink_4 is the
    # quietest step there is and it is only allowed because this is 32 px of
    # type; nothing small on this page may use it.
    right = shape.right
    clock_y = shape.card.y + int(round(HEAD_CLOCK * z))
    total = t("hifi.of_total", time=_clock(page.total_ms))
    done = _clock(page.elapsed_ms)
    total_w = _width(total, clock_size, "light")
    gap = max(6, clock_size // 4)
    _text(surface, total, (right, clock_y), look.ink_4, clock_size,
          weight="light", align="right", glyph="0")
    _text(surface, done, (right - total_w - gap, clock_y), look.ink_1,
          clock_size, weight="light", align="right", glyph="0")
    used = total_w + gap + _width(done, clock_size, "light")

    if page.bars > 0:
        bar_label = t("hifi.bar_of", bar=max(1, page.bar), bars=page.bars)
        # Set on the clock's own optical centre rather than on its top: two
        # sizes on one line agree about their middles, never about their boxes.
        bar_y = clock_y + (_rise(clock_size, "light", "0")
                           - _rise(label, "medium", "H")) // 2
        _text(surface, bar_label,
              (right - used - int(round(HEAD_BAR * z)), bar_y), look.ink_3,
              label, weight="medium", align="right", tracking=PLAY_TRACK,
              upper=True)
        used += (int(round(HEAD_BAR * z))
                 + _width(bar_label, label, "medium", PLAY_TRACK, True))

    # -- and the left ----------------------------------------------------
    room = max(60, right - used - design.SPACE_8 - shape.left)
    number, title = _split_ordinal(page.title)
    name = _lead_part(page.subtitle)
    eyebrow = t("hifi.piece", number=number, name=name) if number else name
    if eyebrow:
        _text(surface, _elide(eyebrow.upper(), label, "medium", room),
              (shape.left, shape.card.y + int(round(HEAD_EYEBROW * z))),
              look.ink_3, label, weight="medium", tracking=PLAY_TRACK)
    if title:
        _text(surface, _elide(title, title_size, "light", room),
              (shape.left, shape.card.y + int(round(HEAD_TITLE * z))),
              look.ink_1, title_size, weight="light")

    score = _score(page)
    if score and shape.head - shape.card.y > HEAD_SCORE * z + 12:
        _text(surface, _elide(score, small, "regular", room),
              (shape.left, shape.card.y + int(round(HEAD_SCORE * z))),
              look.ink_3, small)

    _play_tools(surface, page, look, shape)


def _play_tools(surface: pygame.Surface, page: Page, look, shape: _Shape) -> None:
    size = max(11, int(round(PLAY_SMALL * shape.zoom)))
    entries = [(action, label, _width(label, size, "medium") + 18)
               for action, label in (
                   ("setup", f"{t('key.s')}  {t('practice.setup')}"),
                   ("help", f"{t('key.h')}  {t('play.help')}"))]
    x = shape.right - sum(w for _a, _l, w in entries) - 8
    height = max(25, int(round(28 * shape.zoom)))
    top = shape.head - height - 8
    for action, label, width in entries:
        rect = pygame.Rect(x, top, width, height)
        pygame.draw.rect(surface, look.hairline_loud, rect, 1, border_radius=4)
        _text(surface, label, (rect.centerx,
                               rect.centery - _rise(size, "medium", "H") // 2),
              look.ink_2, size, weight="medium", align="center")
        page.mark(action, rect)
        x = rect.right + 8


def count_in(surface: pygame.Surface, page: Page) -> None:
    remaining = getattr(page, "count_in_remaining", 0)
    if remaining <= 0:
        return
    look = get_look()
    shape = _shape(page)
    size = min(110, max(64, shape.board.height // 3))
    radius = size // 2 + 28
    centre = shape.board.center
    if page.banner or getattr(page, "loop_status", ""):
        centre = (centre[0], min(centre[1], shape.foot - radius - 42))
    _disc(surface, centre, radius, look.panel_right)
    _outline(surface, centre, radius, look.hairline_loud, 2)
    _figures(surface, str(remaining),
             (centre[0], centre[1] - _rise(size, "light", "0") // 2 - 9),
             look.ink_1, size, weight="light", align="center")
    _text(surface, t("play.count_in"),
          (centre[0], centre[1] + radius - 36), look.ink_1,
          design.TEXT_XS, align="center")
    progress = max(0.0, min(1.0, getattr(page, "count_in_progress", 0.0)))
    if progress > 0:
        rect = pygame.Rect(centre[0] - radius, centre[1] - radius, radius * 2, radius * 2)
        pygame.draw.arc(surface, look.hud_accent, rect,
                        math.pi / 2 - progress * math.tau, math.pi / 2, 3)


def _score(page: Page) -> str:
    """How it is going, as separate facts rather than one verdict.

    Found and in time are two different skills and the page never adds them
    up: a note found early is found, and a page that scored it as a miss
    would be teaching them that hesitating is safer than playing.
    """
    if page.expected <= 0 or page.found <= 0:
        return ""
    parts = []
    if page.accuracy is not None:
        parts.append(t("hud.percent", percent=int(round(page.accuracy))))
    parts.append(t("hifi.found", found=page.found, total=page.expected))
    if not page.timing_unknown or page.timing_measured:
        # In time out of the notes the player found and whose timing was measured,
        # not out of every note in the piece. Ten found and all ten in time
        # read "10 von 26 im Takt" -- a timing fault the player does not have. Pitch
        # and timing are two skills; a note not found has no timing to judge.
        parts.append(t("play.in_time", in_time=page.in_time,
                       total=page.timing_measured or page.found))
    if page.timing_unknown:
        parts.append(t("play.timing_unknown_count", count=page.timing_unknown))
    return "   ·   ".join(parts)


def _clock(ms: float) -> str:
    """m:ss, and h:mm:ss for a piece long enough to need it."""
    seconds = max(0, int(ms // 1000))
    if seconds >= 3600:
        return f"{seconds // 3600}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"
    return f"{seconds // 60}:{seconds % 60:02d}"


def _banner(surface: pygame.Surface, page: Page, look, shape: _Shape,
            strings: int) -> None:
    """The one thing the player must not miss, laid on the stave rather than over it.

    In the band under the lowest string, which the drawing leaves empty on
    purpose -- a gap and a quarter of nothing is where this look puts its
    air, and it is the one place a strip can go without covering a note.

    Sized to its sentence rather than to the card. A rule across the whole
    width would be the only full-bleed shape on a page that has none, and it
    would read as the app breaking rather than as the app saying something.
    """
    size = max(11, int(round(design.TEXT_SM * shape.zoom)))
    height = size + design.SPACE_5
    room = shape.board.width - design.SPACE_8
    message = page.banner or getattr(page, "loop_status", "")
    clear_label = t("practice.loop_clear") if getattr(page, "loop_status", "") else ""
    clear_width = _width(clear_label, size) + 20 if clear_label else 0
    words = _elide(message, size, "medium", room - design.SPACE_6 * 2 - clear_width)
    width = min(room, _width(words, size, "medium") + design.SPACE_6 * 2 + clear_width)
    floor = shape.foot - int(lane_gap(page, strings) * LANE_FOOT / 2)
    strip = pygame.Rect(shape.board.centerx - width // 2,
                        floor - height // 2, width, height)
    if strip.width <= 0 or strip.top < shape.head:
        return
    pygame.draw.rect(surface, look.banner_bg if page.banner else look.panel_right, strip,
                     border_radius=design.RADIUS_SM)
    if not page.banner:
        pygame.draw.rect(surface, look.hairline_loud, strip, 1,
                         border_radius=design.RADIUS_SM)
    _text(surface, words,
          (strip.centerx - clear_width // 2,
           strip.y + (height - _rise(size, "medium", "H")) // 2),
          look.banner_text if page.banner else look.ink_2,
          size, weight="medium", align="center")
    page.mark("banner", strip)
    if clear_label:
        button = pygame.Rect(strip.right - clear_width - 3, strip.top + 3,
                             clear_width, strip.height - 6)
        pygame.draw.rect(surface, look.panel_right, button, border_radius=3)
        pygame.draw.rect(surface, look.hairline_loud, button, 1, border_radius=3)
        _text(surface, clear_label,
              (button.centerx, button.centery - _rise(size, "regular", "H") // 2),
              look.ink_2, size, align="center")
        page.mark("loop_clear", button)


# ── The footer ───────────────────────────────────────────────────────────
# One line of controls on the card's floor: two buttons, a dial, four words
# with a dot over each, and what the microphone is hearing. Everything on it
# is either round or type. There is no bar, no tray and no divider except the
# single hairline that separates the dial from the toggles, which is there
# because those two groups do genuinely different things.
#
# A narrow window takes pieces off this line rather than shrinking them to
# fit. The order is the order of what the player can do without: the restart button
# is also a key, the dial is also two keys, the hint under it repeats them,
# the meter is a comfort rather than a control, and the keycaps are written
# down on the help page too. The pause button and the toggles never go.

class _Foot(NamedTuple):
    restart: bool
    knob: bool
    pages: bool
    meter: bool
    keys: bool
    toggles: list


def _foot(surface: pygame.Surface, page: Page, look, shape: _Shape) -> None:
    z = shape.zoom
    floor = shape.card.bottom
    y_label = floor - int(round(FOOT_LABEL * z))
    y_value = floor - int(round(FOOT_VALUE * z))
    y_hint = floor - int(round(FOOT_HINT * z))
    y_mid = floor - int(round(FOOT_MID * z))

    label = max(10, int(round(PLAY_LABEL * z)))
    value = max(14, int(round(PLAY_VALUE * z)))
    word = max(10, int(round(PLAY_WORD * z)))
    small = max(10, int(round(PLAY_SMALL * z)))
    radii = (max(8, int(round(RESTART_R * z))),
             max(12, int(round(PAUSE_R * z))),
             max(12, int(round(KNOB_R * z))))

    tempo = (t("hifi.tempo"), t("hud.percent", percent=page.tempo_percent),
             t("key.pages"))
    toggles = [(t(name), t(key), bool(on), action)
               for name, key, on, action in page.toggles]
    hints = ((t("key.escape"), t("play.menu"), "menu"),
             (t("key.h"), t("play.help"), "help"))

    keep = _fits(shape, z, radii, label, value, word, small, tempo, toggles,
                 hints)

    x = shape.left
    if keep.restart:
        _restart(surface, (x + radii[0], y_mid), radii[0], look.hairline_loud,
                 look.ink_3)
        page.mark("restart", pygame.Rect(x, y_mid - radii[0], radii[0] * 2,
                                         radii[0] * 2))
        x += radii[0] * 2 + int(round(GAP_RESTART * z))

    # The one filled shape on the page that is not a note: pause while it is
    # running, and the triangle while it is not, so the button says what it
    # will do rather than what is happening.
    centre = (x + radii[1], y_mid)
    _disc(surface, centre, radii[1], look.ink_1,
          triangle=look.panel_right if page.paused else None,
          bars=None if page.paused else look.panel_right)
    hit = pygame.Rect(x, y_mid - radii[1], radii[1] * 2, radii[1] * 2)
    # One button, one action: it toggles, so it is "play" whichever glyph it
    # is wearing. Two names for it meant the screen answered one of them and
    # the other sat on top swallowing the click.
    page.mark("play", hit)
    x += radii[1] * 2 + int(round(GAP_PAUSE * z))

    if keep.knob:
        _knob(surface, (x + radii[2], y_mid), radii[2], _dial(page), look)
        page.mark("tempo", pygame.Rect(x, y_mid - radii[2], radii[2] * 2,
                                       radii[2] * 2))
        x += radii[2] * 2 + int(round(GAP_KNOB * z))

    _text(surface, tempo[0], (x, y_label), look.ink_3, label, weight="medium",
          tracking=PLAY_TRACK, upper=True)
    _text(surface, tempo[1], (x, y_value), look.ink_1, value, weight="light",
          glyph="0")
    width = max(_width(tempo[0], label, "medium", PLAY_TRACK, True),
                _width(tempo[1], value, "light"))
    if keep.pages:
        _text(surface, tempo[2], (x, y_hint), look.ink_3, small)
        width = max(width, _width(tempo[2], small))
    if not keep.knob:
        page.mark("tempo", pygame.Rect(x, y_label, width, y_hint - y_label))
    x += width + int(round(GAP_TEMPO * z))

    if keep.toggles:
        divider = pygame.Rect(x, floor - int(round(DIV_HEAD * z)), 1,
                              int(round((DIV_HEAD - DIV_FOOT) * z)))
        surface.fill(look.hairline, divider)
        x += 1 + int(round(GAP_DIV * z))

    dot = max(4, int(round(DOT_R * z)))
    dot_y = y_label + _rise(label, "medium", "H") // 2
    for text_label, key, on, name in keep.toggles:
        column = max(_width(text_label, word), _width(key, small) if keep.keys
                     else 0)
        if on:
            _disc(surface, (x + dot, dot_y), dot, look.hit_zone)
        else:
            # The same outer diameter as the filled one. A ring drawn a pixel
            # inside it made the row of four dots step up and down, which on
            # a page with one row of dots on it is the row.
            _outline(surface, (x + dot, dot_y), dot, look.signal_cold, 2)
        _text(surface, text_label, (x, y_value), look.ink_1 if on else
              look.ink_3, word)
        if keep.keys:
            _text(surface, key, (x, y_hint), look.ink_3, small)
        page.mark(name, pygame.Rect(x, dot_y - dot, column,
                                    y_hint + small - dot_y + dot))
        x += column + int(round(GAP_TOGGLE * z))

    right = shape.right
    if keep.meter:
        _text(surface, t("hifi.input"), (right, y_label), look.ink_3, label,
              weight="medium", align="right", tracking=PLAY_TRACK, upper=True)
        meter = pygame.Rect(right - int(round(METER_W * z)), y_value,
                            int(round(METER_W * z)),
                            max(8, int(round(METER_H * z))))
        shapes.meter(surface, meter, page.signal, on=look.ink_1,
                     off=look.track_empty, bars=METER_BARS, rising=False)
        page.mark("input", meter)

    # "Esc Menue · H Hilfe". The dot is in the drawing and it is doing work:
    # without it the four words are one phrase at one weight, and the player reads
    # "Menue H" as a thing. It is punctuation, not a word, so it is a literal
    # here the same way the score line's separator is.
    cursor = right - _hints_width(small, hints)
    for index, (key, name, mark) in enumerate(hints):
        if index:
            _text(surface, HINT_DOT, (cursor, y_hint), look.ink_3, small)
            cursor += _width(HINT_DOT, small) + HINT_SPLIT
        key_w = _width(key, small)
        _text(surface, key, (cursor, y_hint), look.ink_3, small)
        _text(surface, name, (cursor + key_w + 6, y_hint), look.ink_3, small)
        reach = key_w + 6 + _width(name, small)
        page.mark(mark, pygame.Rect(cursor, y_hint - 6, reach, small + 12))
        cursor += reach + HINT_SPLIT


def _hints_width(small: int, hints) -> int:
    """The two key hints and the dot between them, as one line."""
    total = sum(_width(key, small) + 6 + _width(name, small)
                for key, name, _ in hints)
    if len(hints) > 1:
        total += (len(hints) - 1) * (_width(HINT_DOT, small) + HINT_SPLIT * 2)
    return total


def _dial(page: Page) -> float:
    """Map the complete supported tempo range onto the dial's sweep."""
    minimum = getattr(page, "tempo_min_percent", 25)
    return max(0.0, min(1.0, (page.tempo_percent - minimum) / (100 - minimum)))


def _fits(shape: _Shape, z: float, radii, label: int, value: int, word: int,
          small: int, tempo, toggles, hints) -> _Foot:
    """What of the footer this width can hold, dropped in order of need."""
    room = shape.right - shape.left
    keep = _Foot(True, True, True, True, True, list(toggles))
    order = ("restart", "knob", "pages", "meter", "keys")
    step = 0
    while True:
        need = _foot_width(z, radii, label, value, word, small, tempo, keep,
                           hints)
        if need <= room:
            return keep
        if step < len(order):
            keep = keep._replace(**{order[step]: False})
            step += 1
        elif keep.toggles:
            # All the way to none if it comes to that. One toggle drawn where
            # the key hints are is worse than no toggle: both are live, both
            # are clickable, and the click lands on whichever was recorded
            # last rather than on the one the player was pointing at.
            keep = keep._replace(toggles=keep.toggles[:-1])
        else:
            return keep


def _foot_width(z: float, radii, label: int, value: int, word: int,
                small: int, tempo, keep: _Foot, hints) -> int:
    total = radii[1] * 2 + int(round(GAP_PAUSE * z))
    if keep.restart:
        total += radii[0] * 2 + int(round(GAP_RESTART * z))
    if keep.knob:
        total += radii[2] * 2 + int(round(GAP_KNOB * z))
    column = max(_width(tempo[0], label, "medium", PLAY_TRACK, True),
                 _width(tempo[1], value, "light"))
    if keep.pages:
        column = max(column, _width(tempo[2], small))
    total += column + int(round(GAP_TEMPO * z))
    if keep.toggles:
        total += 1 + int(round(GAP_DIV * z))
    for text_label, key, _on, _name in keep.toggles:
        total += int(round(GAP_TOGGLE * z)) + max(
            _width(text_label, word), _width(key, small) if keep.keys else 0)
    right = _hints_width(small, hints)
    if keep.meter:
        right = max(right, int(round(METER_W * z)),
                    _width(t("hifi.input"), label, "medium", PLAY_TRACK, True))
    # The drawing leaves a hand's width between the last toggle and the right
    # hand group, and that void is the composition rather than slack: the
    # footer reads as two clusters at the ends of one line, not as a strip.
    # It comes down with everything else rather than being a fixed 48 px --
    # at 640 that would be the whole difference between four toggles and
    # three, and the void is not worth a toggle.
    return total + right + int(round(design.SPACE_12 * z))


# ── Temporary harness ────────────────────────────────────────────────────
# menu.py builds the Page itself; this exists only so this module can be
# rendered on its own while the other two painters are still being written.
# The wiring agent deletes it.

def _demo_page(screen, surface: pygame.Surface) -> Page:
    return screen._page(surface)


def _demo(screen, surface: pygame.Surface) -> Page:
    page = _demo_page(screen, surface)
    library(surface, page)
    return page
