"""What a screen hands its look, and what a look hands back.

The six drawings are three library pages and three play pages, not one page
in three palettes. menu.py and scrolling.py drew exactly one layout each,
parameterised by colour -- so every look was a repaint of the wrong shape, and
no amount of palette work was ever going to fix that.

This is the seam. The screen keeps the state, the keys and the data; the look
decides where things go and what shape they are. A `Page` is the whole of what
a painter is allowed to see, so a painter cannot reach into a screen and a
screen cannot reach into a painter.

Painters live in `paint_studio.py`, `paint_stage.py` and `paint_hifi.py`, one
per look, and are picked by `painter(look.key)`. Each exposes:

    library(surface, page) -> None
    play(surface, page) -> None

Everything a painter draws that can be clicked goes into `page.hit`, which the
screen reads back when a click arrives. That is the only direction information
travels back, and it keeps hit-testing honest: a control that was not drawn
this frame is not in the map, so it cannot be clicked.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

import pygame


@dataclass
class Page:
    """One frame's worth of everything a painter may look at.

    Built fresh each frame by the screen. Painters never hold onto it: it is
    a view of state that is about to change, and the one thing worse than a
    stale layout is a layout that is stale only sometimes.
    """

    # -- The canvas -------------------------------------------------------
    width: int
    height: int

    # -- What is on it ----------------------------------------------------
    rows: list = field(default_factory=list)       # SongInfo, already filtered
    selected: int = 0
    scroll_px: int = 0
    # Keyboard navigation follows the selection; wheel browsing deliberately
    # leaves the selected song where it was, even when it is off screen.
    reveal_selection: bool = True
    # (label, value, count, active) -- the filter chips / sidebar entries.
    filters: list[tuple[str, Any, int, bool]] = field(default_factory=list)
    # The piece the player was in the middle of, and its record. None if there is none.
    carry: tuple[Any, Any] | None = None
    # accuracy percent per row index, None where never played.
    scores: dict[int, float | None] = field(default_factory=dict)
    # attempts and last-played date per row index.
    history: dict[int, tuple[int, str]] = field(default_factory=dict)

    # -- Chrome state -----------------------------------------------------
    search_active: bool = False
    search_text: str = ""
    setup_open: bool = False
    setup_scroll: int = 0
    setup_maximum: int = 0
    instrument_name: str = ""
    instrument_short: str = ""
    tuning_label: str = ""          # "E A D G"
    device_name: str = ""
    device_ready: bool = True
    counts: tuple[int, int] = (0, 0)   # pieces, played
    sort_label: str = ""
    empty_title: str = ""
    empty_hint: str = ""

    # -- The play screen --------------------------------------------------
    title: str = ""
    subtitle: str = ""
    bar: int = 0
    bars: int = 0
    bpm: int = 0
    elapsed_ms: float = 0.0
    total_ms: float = 0.0
    visible_window_ms: float = 8000.0
    timeline_beats: list = field(default_factory=list)
    timeline_markers: bool = False
    count_in_remaining: int = 0
    count_in_progress: float = 0.0
    tempo_min_percent: int = 25
    loop_status: str = ""
    tempo_percent: int = 100
    paused: bool = False
    accuracy: float | None = None
    streak: int = 0
    found: int = 0
    expected: int = 0
    in_time: int = 0
    timing_measured: int | None = None
    timing_unknown: int = 0
    signal: float = 0.0            # 0..1, for the input meter
    # How many lanes, and what each one is called. Without these a painter
    # has to guess, and the one that guessed drew six guitar strings down the
    # side of a four string bass ukulele -- labelled E B G D A E, none of
    # which the player has.
    strings: int = 4
    # (string number, letter, name with octave), highest string first.
    string_labels: list[tuple[int, str, str]] = field(default_factory=list)
    # (label key, key-hint key, is on, action name). The action is in here
    # so a painter can mark a toggle without knowing what the screen calls
    # it -- it draws four switches and hands back where it put them.
    toggles: list[tuple[str, str, bool, str]] = field(default_factory=list)
    banner: str | None = None

    # -- Back from the painter --------------------------------------------
    # Several rects per name on purpose. Hi-fi draws the setup control twice
    # -- the pill at the top and the key hint at the foot -- and with one
    # rect per name the second silently replaced the first, so the pill was
    # drawn, looked clickable, and was not.
    hit: dict[str, list[pygame.Rect]] = field(default_factory=dict)
    row_hit: list[tuple[pygame.Rect, int]] = field(default_factory=list)
    # The painter owns the row rhythm, so it reports it back rather than the
    # screen assuming one: Studio's rows are 67 px, Stage's 83 and Hi-fi's 67,
    # and a screen that guessed would scroll by the wrong amount on two of
    # the three looks.
    hit_row_h: int = 0
    hit_band: int = 0

    def clamp_scroll(self, row_height: int, band_height: int,
                     row_pitch: int | None = None) -> int:
        """Bound the list using its actual layout, optionally revealing selection.

        Painters call this after measuring their list, including any header or
        continuation card. Row pitch may include gaps; the selected row's own
        height determines whether all of its content is visible.
        """
        row_height = max(1, int(row_height))
        pitch = max(1, int(row_pitch if row_pitch is not None else row_height))
        band = max(0, int(band_height))
        self.hit_row_h = pitch
        self.hit_band = band
        total = (len(self.rows) - 1) * pitch + row_height if self.rows else 0
        at = max(0, int(round(self.scroll_px)))
        if self.reveal_selection and self.rows and band:
            index = max(0, min(len(self.rows) - 1, self.selected))
            top = index * pitch
            at = min(at, top)
            at = max(at, top + min(row_height, band) - band)
        self.scroll_px = max(0, min(at, max(0, total - band)))
        return self.scroll_px

    def mark(self, name: str, rect: pygame.Rect) -> pygame.Rect:
        """Record that `name` was drawn at `rect` and can be clicked.

        Appends rather than replaces: one action may be drawn in more than
        one place, and the second one is not a correction of the first.
        """
        self.hit.setdefault(name, []).append(rect)
        return rect

    def hits(self, name: str) -> list[pygame.Rect]:
        return self.hit.get(name, [])

    def at(self, pos: tuple[int, int]) -> str | None:
        """Which action was drawn under `pos`, if any.

        Last drawn wins, because that is what is on top: a popover over a
        row is what the click lands on, and iterating in draw order would
        hand it to whatever was underneath.
        """
        for name, rects in reversed(list(self.hit.items())):
            for rect in rects:
                if rect.collidepoint(pos):
                    return name
        return None

    def mark_row(self, rect: pygame.Rect, index: int) -> pygame.Rect:
        self.row_hit.append((rect, index))
        return rect


Painter = Callable[[pygame.Surface, Page], None]


# Which module draws which look. Names rather than imports, so a module that
# is not there yet -- or was dropped from a build -- costs a fallback instead
# of an ImportError at startup.
PAINTERS = {"studio": "paint_studio", "stage": "paint_stage",
            "hifi": "paint_hifi"}


def painter(key: str):
    """The module that draws for look `key`, or None to use the fallback.

    Imported lazily and by name rather than through a registry the painters
    write into on import: a registry means the look you get depends on what
    has been imported, which is the kind of order dependency that shows up as
    a blank screen in a packaged build and nowhere else.

    Returning None rather than raising is deliberate. A missing painter is a
    packaging mistake, and a packaging mistake that blanks the screen is far
    worse than one that draws the plain version -- the player can still practise.
    """
    import importlib

    name = PAINTERS.get(key)
    if name is None:
        return None
    try:
        return importlib.import_module(f"pickhero.ui.{name}")
    except Exception:
        return None


# ── Geometry shared by all three ─────────────────────────────────────────
# The drawings are a 1280x766 sheet and the window is 1280x720 and resizable.
# Reading a y straight off the sheet puts every row 46 px low, so fractions
# are of the live window and the few things that were measured against the
# sheet say so where they are used.

def frac(total: int, f: float) -> int:
    """`f` of `total`, rounded. The drawings are specified in fractions."""
    return int(round(total * f))


def split(width: int, at: float, minimum: int = 280) -> tuple[int, int]:
    """Two panel widths from one fraction, with a floor on the left.

    Below the floor the left panel is dropped entirely rather than squeezed:
    a 120 px "now playing" column is not a smaller version of the drawing, it
    is a different and worse one.
    """
    left = frac(width, at)
    if left < minimum:
        return 0, width
    return left, width - left
