"""The visual system: palette, scales, and the drawing primitives built on them.

One place for every number that decides how the app looks, so a change lands
everywhere at once instead of in nine hand-tuned blits.

Two decisions here are not taste, they are corrections to the mockup and both
come from the same problem. A string's colour is its identity, and hit or miss
feedback also wants colour. If a hit turns the note green, the note loses the
one thing telling the player which string to use, exactly at the moment they
look at it. So the fill always stays the string colour and feedback is carried
by the ring around it. And for the same reason no string is red: red means
missed, and a permanently red string would read as a permanent mistake.
"""

from __future__ import annotations

import math
import pathlib

import pygame

# ── Spacing ──────────────────────────────────────────────────────────────
# A 4 px base. Everything that positions something uses one of these.
SPACE_1, SPACE_2, SPACE_3, SPACE_4 = 4, 8, 12, 16
SPACE_5, SPACE_6, SPACE_8, SPACE_12 = 20, 24, 32, 48

# ── Radii ────────────────────────────────────────────────────────────────
RADIUS_SM, RADIUS_MD, RADIUS_LG, RADIUS_PILL = 4, 8, 14, 999

# ── Type ─────────────────────────────────────────────────────────────────
# The three looks are three typefaces as much as three palettes, and until
# these were bundled they all rendered in Segoe UI -- which is the single
# biggest reason the app did not look like the drawings it was built from.
# All four are SIL Open Font Licence; the licences sit beside them.
def _font_dir() -> pathlib.Path:
    """Where the bundled faces live, frozen or not.

    PyInstaller's one-file build unpacks data to a temporary folder and tells
    you about it through sys._MEIPASS; __file__ there points inside the
    archive, so resolving from it finds nothing and every look silently falls
    back to Segoe UI. That is the exact failure this whole change was for, so
    it is handled here rather than discovered in a release.
    """
    import sys
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return pathlib.Path(base) / "pickhero" / "assets" / "fonts"
    return pathlib.Path(__file__).resolve().parent.parent / "assets" / "fonts"


FONT_DIR = _font_dir()

# family -> (regular, bold). A face named here is loaded from the folder
# above; anything else falls through to the platform stack.
# family -> weight -> file. Explicit, because the suffix game it replaced
# resolved "bold" to the semibold cut on Geist and to the medium cut on
# Hanken, so two of the four weights the drawings use could not be reached at
# all and HankenGrotesk-Bold.ttf shipped without ever being drawn.
BUNDLED: dict[str, dict[str, str]] = {
    "geist": {
        "light": "Geist-Regular.ttf",
        "regular": "Geist-Regular.ttf",
        "medium": "Geist-Medium.ttf",
        "semibold": "Geist-SemiBold.ttf",
        "bold": "Geist-Bold.ttf",
    },
    "chakra": {
        "light": "ChakraPetch-Regular.ttf",
        "regular": "ChakraPetch-Regular.ttf",
        "medium": "ChakraPetch-Regular.ttf",
        "semibold": "ChakraPetch-Bold.ttf",
        "bold": "ChakraPetch-Bold.ttf",
    },
    "chakra-italic": {
        "light": "ChakraPetch-Italic.ttf",
        "regular": "ChakraPetch-Italic.ttf",
        "medium": "ChakraPetch-Italic.ttf",
        "semibold": "ChakraPetch-BoldItalic.ttf",
        "bold": "ChakraPetch-BoldItalic.ttf",
    },
    "hanken": {
        "light": "HankenGroteskLight-Regular.ttf",
        "regular": "HankenGrotesk-Regular.ttf",
        "medium": "HankenGroteskMedium-Regular.ttf",
        "semibold": "HankenGroteskMedium-Regular.ttf",
        "bold": "HankenGrotesk-Bold.ttf",
    },
    "mono": {
        "light": "JetBrainsMono-Regular.ttf",
        "regular": "JetBrainsMono-Regular.ttf",
        "medium": "JetBrainsMono-Regular.ttf",
        "semibold": "JetBrainsMono-Bold.ttf",
        "bold": "JetBrainsMono-Bold.ttf",
    },
}

# What `bold=True` means when no weight is named. Kept so the hundreds of
# existing bold= call sites keep working while new code names a weight.
DEFAULT_BOLD = "semibold"
DEFAULT_REGULAR = "regular"

# Segoe UI ships with every supported Windows. Inter and Roboto matched
# nothing on a stock machine and DejaVu Sans silently resolved to Arial, so
# the middle of the old stack was decoration; what actually catches a machine
# without Segoe UI is Arial, and after that pygame's own bundled face.
FONT_STACK = ("Segoe UI", "Arial", "Tahoma", "Verdana")
MONO_STACK = ("Consolas", "DejaVu Sans Mono", "Courier New")

TEXT_XS, TEXT_SM, TEXT_MD = 13, 15, 18
TEXT_LG, TEXT_XL, TEXT_2XL, TEXT_3XL = 22, 28, 36, 56
# The drawings use display sizes the old scale simply did not reach: Hi-fi's
# "05" is a third of the panel, and Stage's title is set to fill its column.
TEXT_4XL, TEXT_5XL = 84, 150

_font_cache: dict[tuple[str, int, str], pygame.font.Font] = {}
_FONT_CACHE_LIMIT = 96


_quit_hook_installed = False


def font(size: int, bold: bool = False, mono: bool = False,
         face: str | None = None, weight: str | None = None,
         italic: bool = False) -> pygame.font.Font:
    """A cached font. pygame.font.SysFont is slow enough to matter per frame.

    `face` names a bundled family from BUNDLED. Left out, it comes from the
    current look -- which is the whole point: the three looks are three
    typefaces, and while this function ignored the look they were three
    palettes painted on one drawing in Segoe UI.

    The cache has to be tied to pygame's own lifetime. pygame.quit() frees the
    SDL_ttf fonts underneath these objects, and the Python wrappers survive it
    pointing at memory that is gone. Rendering with one afterwards does not
    raise; it takes the process down with an access violation that no except
    clause can catch. So: register with pygame to be told when it quits, and
    check the font module is actually up before trusting anything cached.
    """
    global _quit_hook_installed
    if not pygame.font.get_init():
        _font_cache.clear()
        _quit_hook_installed = False
        pygame.font.init()
    if face is None:
        face = _look_face(mono)
    if italic and (face + "-italic") in BUNDLED:
        face = face + "-italic"
    if weight is None:
        weight = DEFAULT_BOLD if bold else DEFAULT_REGULAR
    key = (face or ("mono" if mono else "sans"), size, weight)
    cached = _font_cache.get(key)
    if cached is not None:
        return cached
    if not _quit_hook_installed:
        # Cleared again on the next pygame.quit(). The registration itself is
        # dropped by that quit, hence the flag rather than a one-time call.
        pygame.register_quit(clear_font_cache)
        _quit_hook_installed = True
    result = _bundled_face(face, size, weight)
    if result is None:
        heavy = weight in ("semibold", "bold")
        for family in (MONO_STACK if mono else FONT_STACK):
            result = _load_face(family, size, heavy)
            if result is not None:
                break
    if result is None:
        result = pygame.font.Font(None, size)
    if len(_font_cache) >= _FONT_CACHE_LIMIT:
        # A resize drag asks for new sizes frame after frame, and every face
        # stayed: 378 of them after one drag. Dropping the lot is cheaper
        # than tracking which are stale. The letterspaced-glyph cache goes
        # with it, because it keys on id(font), and an id is only unique
        # while its object is alive.
        _font_cache.clear()
        _tracked.clear()
    _font_cache[key] = result
    return result


def reading_font(size: int, *, bold: bool = False) -> pygame.font.Font:
    """A legible body face; keep Stage's angular display face for headings."""
    from pickhero.ui.colors import get_look
    if get_look().key == "stage":
        return font(size, face="geist", weight="semibold" if bold else "medium")
    return font(size, bold=bold)


def _look_face(mono: bool) -> str:
    """The current look's face. Falls back to the platform stack."""
    try:
        from pickhero.ui.colors import get_look
        look = get_look()
    except Exception:
        return "mono" if mono else "sans"
    name = look.face_mono if mono else look.face_sans
    return name or ("mono" if mono else "sans")


def _bundled_face(face: str, size: int,
                  weight: str) -> pygame.font.Font | None:
    """One of the shipped families at one weight, or None to fall through."""
    entry = BUNDLED.get(face)
    if entry is None:
        return None
    name = entry.get(weight) or entry.get(DEFAULT_REGULAR)
    if name is None:
        return None
    path = FONT_DIR / name
    if not path.exists():
        return None
    try:
        loaded = pygame.font.Font(str(path), size)
        # Font() accepts a truncated or zero-byte file and only fails on the
        # first render, inside a frame, where nothing catches it. Ask it to
        # draw once here, where falling through to the system face is free.
        loaded.render("Ag", True, (0, 0, 0))
        return loaded
    except Exception:
        return None


# Windows ships several weights of Segoe UI and matches the wrong one.
# match_font("Segoe UI") returns segoeuil.ttf, the LIGHT weight, while
# match_font("Segoe UI", bold=True) correctly returns segoeuib.ttf. So every
# heading in this app has been drawn at the intended weight and every line of
# body text at under half of it: measured ink for a 40 px "H" is 116 lit
# pixels against 238 for the regular face. At TEXT_XS that is the thinnest
# thing the system can produce. Asking for "Segoe UI Regular" does not help,
# it matches nothing; the regular file has to be reached directly.
_LIGHT_SUFFIXES = ("sl", "l", "th")


def _regular_weight(path: str) -> str:
    """Swap a light or thin font file for the regular one beside it."""
    import os
    folder, name = os.path.split(path)
    stem, ext = os.path.splitext(name)
    for suffix in _LIGHT_SUFFIXES:
        if stem.endswith(suffix):
            candidate = os.path.join(folder, stem[:-len(suffix)] + ext)
            if os.path.exists(candidate):
                return candidate
    return path


def _load_face(family: str, size: int, bold: bool) -> pygame.font.Font | None:
    """One family, at the weight actually asked for."""
    try:
        path = pygame.font.match_font(family, bold=bold)
    except Exception:
        path = None
    if path:
        try:
            if not bold:
                path = _regular_weight(path)
            return pygame.font.Font(path, size)
        except Exception:
            pass
    try:
        return pygame.font.SysFont(family, size, bold=bold)
    except Exception:
        return None


def clear_font_cache() -> None:
    """Drop every cached surface. Registered with pygame so a quit cannot
    leave dangling SDL handles behind in a cache.

    Named for fonts because that is what it started as; it now clears the
    sprite cache too, for the same reason. pygame.quit() frees the surfaces
    underneath these objects and rendering with one afterwards is an access
    violation, not an exception.
    """
    global _quit_hook_installed
    _font_cache.clear()
    _sprites.clear()
    _tracked.clear()
    from pickhero.ui.shapes import clear_aa_cache
    clear_aa_cache()
    _quit_hook_installed = False


clear_caches = clear_font_cache


# ── String colours ───────────────────────────────────────────────────────
# Ordered so a 4-string instrument uses the first four and a 6-string one all
# six, with no two adjacent strings sharing a hue.
#
# Equiluminant on purpose: no string may look brighter, and therefore more
# important, than the one next to it. That makes contrast ratio the wrong
# measure here and delta_e the right one. Neighbouring lanes sit 50 to 100
# units apart; the weakest pair in the set is violet against blue at 24, and
# those are strings 4 and 6, two lanes apart with a third between them.
#
# The three verdict colours are holes in this wheel, not just other colours:
# a green string would read as a permanent hit the way a red one would read
# as a permanent mistake. Green, amber and red therefore block roughly 240
# degrees of hue between them, which is why the fifth string is a lime rather
# than the teal it used to be.
#
# Colour is deliberately the SECOND cue here, not the first. An evenly
# saturated palette has nearly equal luminance by construction, so these
# cannot be told apart by brightness and would not survive severe colour
# vision deficiency on their own. What identifies a string is its fixed row
# and the permanent name in the left gutter; the colour only makes the
# association quick for people who can use it. Any future change that makes
# colour load-bearing has to add a second cue with it.
STRING_PALETTE: dict[int, tuple[int, int, int]] = {
    1: (232, 108, 190),  # magenta
    2: (253, 133, 40),   # orange
    3: (86, 190, 232),   # sky
    4: (152, 122, 238),  # violet
    5: (116, 254, 74),   # lime
    6: (110, 150, 246),  # blue
}


def string_colour(string: int) -> tuple[int, int, int]:
    return STRING_PALETTE.get(string, (176, 182, 196))


# One dark ink for the fret number on every card. Measured across the whole
# palette it clears 4.5:1 on all six strings, worst case violet. White ink
# would fail on five of the six, which is why the fret number needed an
# outline to survive at all.
FRET_INK = (16, 18, 26)


def on_accent(accent: tuple[int, int, int]) -> tuple[int, int, int]:
    """Text drawn on top of an accent fill: the Spielen button's label."""
    return _INK_ON_LIGHT if is_light(accent) else _INK_ON_DARK


# Difficulty, level 1 to 5. The same five colours were written out twice in
# menu.py and both copies were tuned against a dark page; on the light one
# every pastel fell to between 1.35 and 2.53 to 1, under the 3:1 a non-text
# indicator needs. These are darker and clear it in both themes.
DIFFICULTY_SCALE = (
    (30, 140, 100),   # 1 Anfang
    (94, 138, 40),    # 2 Leicht
    (158, 118, 12),   # 3 Mittel
    (182, 92, 22),    # 4 Schwer
    (192, 46, 72),    # 5 Profi
)
# The same ramp lifted for a dark page. One set cannot serve both: a colour
# dark enough to read on white is nearly invisible on near-black, and the
# glyph is the first thing the eye lands on when choosing what to play.
DIFFICULTY_SCALE_DARK = (
    (96, 208, 156),
    (154, 202, 96),
    (232, 190, 80),
    (238, 146, 78),
    (238, 104, 128),
)


def difficulty_colour(level: int,
                      base: tuple[int, int, int] | None = None) -> tuple[int, int, int]:
    """Colour for difficulty 1 to 5, on the page it will be drawn on."""
    index = max(1, min(len(DIFFICULTY_SCALE), int(level))) - 1
    if base is None:
        from pickhero.ui.colors import get_look
        base = get_look().menu_bg
    scale = DIFFICULTY_SCALE if is_light(base) else DIFFICULTY_SCALE_DARK
    return scale[index]


# ── Feedback ─────────────────────────────────────────────────────────────
# There are no verdict constants in this module. There were four -- HIT,
# CLOSE, MISS and OFFBEAT -- and they were right for exactly one page: a dark
# one. The moment a look put the app on paper they measured 1.01:1 to 1.59:1
# in nine places, which is a number drawn and then not visible, including the
# accuracy on the result screen.
#
# A constant cannot know what it will be drawn on, so the verdict colours
# live on the Look instead, as feedback_hit, feedback_close, feedback_miss
# and feedback_early. Deleting these rather than leaving them for reference
# is the point: a colour that does not exist cannot be reached for by
# accident, and reaching for one was the whole defect.
#
# The verdict is still never carried by colour alone. It is encoded three
# ways at once -- the ring colour, the ring thickness (a hit is thicker), and
# whether the card is darkened (a miss is) -- and any one of the three is
# enough to read it.
#
# feedback_early is deliberately off the green-amber-red scale on every look.
# Those three say how well the player played the note; timing is a different skill,
# learned later and mostly by ear. A note found two hundred milliseconds
# early used to look exactly like a note never played, which told them the
# opposite of the truth.


def mix(a: tuple[int, int, int], b: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    t = max(0.0, min(1.0, t))
    return (round(a[0] + (b[0] - a[0]) * t),
            round(a[1] + (b[1] - a[1]) * t),
            round(a[2] + (b[2] - a[2]) * t))


def shade(colour: tuple[int, int, int], factor: float) -> tuple[int, int, int]:
    """Darken (factor < 1) or lighten (factor > 1) without leaving the gamut.

    For anything that has to stay legible, reach for ink(), surface() or
    stroke() instead. Those know which way the page runs; this does not, and
    lightening a light background is how the whole interface once went blank.
    """
    if factor <= 1.0:
        return (round(colour[0] * factor), round(colour[1] * factor),
                round(colour[2] * factor))
    return mix(colour, (255, 255, 255), factor - 1.0)


def with_alpha(colour: tuple[int, int, int], alpha: int) -> tuple[int, int, int, int]:
    return (colour[0], colour[1], colour[2], max(0, min(255, alpha)))


# ── Polarity ─────────────────────────────────────────────────────────────
# Every raised surface in this app used to be drawn as "the background, a
# little lighter". On a dark page that lifts a control off the background. On
# a light one it moves toward the background instead, and measured on the
# light theme it arrived at a contrast ratio of 1.01 to 1: a button drawn in
# its own background colour. The whole header and footer of the browser
# disappeared that way, and so did the song rows, the search box, the bar
# lines on the playing screen and the progress trough.
#
# So no drawing code decides a colour by arithmetic on the background any
# more. It asks for ink, a surface or a stroke, and these three decide which
# direction is "away from the page" first.

_INK_ON_LIGHT = (14, 16, 24)
_INK_ON_DARK = (242, 246, 255)

# How far a surface sits from the page. Index is an elevation step.
SURFACE_LEVELS = (0.0, 0.055, 0.10, 0.16)
# How far a hairline sits from the surface it sits on. Index is emphasis.
STROKE_LEVELS = (0.30, 0.52, 0.72)


def luminance(colour: tuple[int, int, int]) -> float:
    """Relative luminance per sRGB, the quantity WCAG contrast is built on."""
    out = []
    for channel in colour[:3]:
        v = channel / 255.0
        out.append(v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4)
    return 0.2126 * out[0] + 0.7152 * out[1] + 0.0722 * out[2]


def contrast(a: tuple[int, int, int], b: tuple[int, int, int]) -> float:
    """WCAG contrast ratio between two colours, from 1.0 to 21.0."""
    la, lb = luminance(a), luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def is_light(base: tuple[int, int, int]) -> bool:
    """Whether a page reads as light. The threshold is the luminance at which
    black and white text contrast equally against it."""
    return luminance(base) > 0.179


def ink(base: tuple[int, int, int], strength: float = 1.0) -> tuple[int, int, int]:
    """Text colour for a given surface.

    `strength` runs 0 to 1: 1.0 is a title, about 0.7 a subtitle, about 0.5 a
    keyboard hint. It never darkens a dark theme or lightens a light one,
    which is what `shade(hud_text, 0.4)` did — on the light theme that made
    muted text *darker* than the titles and inverted the whole hierarchy.
    """
    target = _INK_ON_LIGHT if is_light(base) else _INK_ON_DARK
    return mix(base, target, 0.42 + 0.58 * max(0.0, min(1.0, strength)))


def readable(colour: tuple[int, int, int], base: tuple[int, int, int],
             target: float = 4.5) -> tuple[int, int, int]:
    """`colour` moved just far enough to be legible on `base`, keeping its hue.

    The string palette is the app's one piece of identity colour, so a string
    may not simply be replaced by black or white when the page is the wrong
    brightness -- then the player loses which string it was, which is the thing the
    colour is for. This walks the colour towards the page's own ink and stops
    at the first step that clears `target`, so it darkens a pastel on paper
    and lightens it on a dark stage, and does nothing at all when the colour
    already reads.

    Written for the ghost note: a note that went by unplayed was drawn in the
    raw string colour, which measures 1.37:1 on Hi-fi's paper. The note the player
    missed was the one the player could not see.
    """
    if contrast(colour, base) >= target:
        return colour
    end = _INK_ON_LIGHT if is_light(base) else _INK_ON_DARK
    lo, hi = 0.0, 1.0
    for _ in range(12):
        mid = (lo + hi) / 2
        if contrast(mix(colour, end, mid), base) >= target:
            hi = mid
        else:
            lo = mid
    return mix(colour, end, hi)


def surface_colour(base: tuple[int, int, int], level: int = 1) -> tuple[int, int, int]:
    """A raised fill on `base`. Level 0 is the page itself.

    On a light page a fill alone can never carry a control -- even twenty
    percent toward black only reaches 1.58 to 1 -- so a control is this fill
    plus a stroke() border, and the border is what makes it visible.
    """
    level = max(0, min(len(SURFACE_LEVELS) - 1, level))
    target = _INK_ON_LIGHT if is_light(base) else _INK_ON_DARK
    return mix(base, target, SURFACE_LEVELS[level])


# The drawing primitives all take a parameter called `surface`, so inside this
# module the token is reached by its full name. `surface` stays as the public
# alias because that is what reads best at the call sites.
surface = surface_colour


def sunken(base: tuple[int, int, int]) -> tuple[int, int, int]:
    """A recessed well: a progress trough, a level meter's track.

    A well goes the other way from a raised surface -- darker on a light page
    -- so that the accent fill inside it reads as something added rather than
    something taken away.
    """
    target = _INK_ON_LIGHT if is_light(base) else _INK_ON_DARK
    return mix(base, target, 0.22 if is_light(base) else 0.24)


def delta_e(a: tuple[int, int, int], b: tuple[int, int, int]) -> float:
    """Perceptual distance between two colours, CIE76, in Lab.

    Contrast ratio is the wrong measure for the string palette and the three
    verdicts. Both were built deliberately equiluminant -- no string may look
    brighter and therefore more important than its neighbour -- so they are
    told apart by hue, and hue is exactly what a luminance ratio discards.
    Roughly: under 10 is a shade of the same colour, over 25 is unmistakably
    a different one.
    """
    def to_lab(colour):
        rgb = []
        for channel in colour[:3]:
            v = channel / 255.0
            rgb.append(v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4)
        r, g, bl = rgb
        x = (0.4124 * r + 0.3576 * g + 0.1805 * bl) / 0.95047
        y = (0.2126 * r + 0.7152 * g + 0.0722 * bl)
        z = (0.0193 * r + 0.1192 * g + 0.9505 * bl) / 1.08883

        def f(t):
            return t ** (1 / 3) if t > 0.008856 else (7.787 * t + 16 / 116)

        fx, fy, fz = f(x), f(y), f(z)
        return (116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz))

    la, aa, ba = to_lab(a)
    lb, ab, bb = to_lab(b)
    return math.sqrt((la - lb) ** 2 + (aa - ab) ** 2 + (ba - bb) ** 2)


def chrome(base: tuple[int, int, int]) -> tuple[int, int, int]:
    """A framing band that is not content: a header, a footer, a toolbar.

    Chrome recedes in both themes -- it goes darker than the page whichever
    way the page runs -- so the eye reads the content area as the lit one. The
    band is separated from the content by a stroke() rule, not by this fill
    alone; on a dark theme the fill difference alone measured 1.04 to 1.
    """
    return mix(base, (0, 0, 0), 0.20 if is_light(base) else 0.35)


def scrim(base: tuple[int, int, int]) -> tuple[int, int, int]:
    """The veil drawn behind a modal panel or the count-in."""
    return mix(base, (0, 0, 0), 0.62)


def stroke(base: tuple[int, int, int], level: int = 1) -> tuple[int, int, int]:
    """A hairline on `base`: borders, dividers, the off state of an indicator.

    Level 1 clears the 3:1 that a control boundary needs, in both themes.
    """
    level = max(0, min(len(STROKE_LEVELS) - 1, level))
    target = _INK_ON_LIGHT if is_light(base) else _INK_ON_DARK
    return mix(base, target, STROKE_LEVELS[level])


# ── Drawing primitives ───────────────────────────────────────────────────

def card(
    surface: pygame.Surface,
    rect: pygame.Rect,
    fill: tuple[int, int, int],
    *,
    radius: int = RADIUS_LG,
    ring: tuple[int, int, int] | None = None,
    ring_width: int = 3,
    glow: float = 0.0,
) -> None:
    """A rounded card, optionally ringed and glowing.

    `glow` in 0..1 draws a soft halo outside the card in the ring colour. It is
    what makes a hit legible without recolouring the note.

    The ring is drawn AROUND the card, not on it. pygame.draw.rect with a
    width paints inward, over the fill, so the verdict used to be a border
    made of the card's own colour blended with the string's: measured, the
    green hit ring sat at 1.07 to 1 on the amber string. A ring has to be on
    the lane to be a ring.
    """
    # Clamped rather than trusted: a caller that derives glow from an age can
    # produce a huge value when the clock jumps backwards, and the halo then
    # swallows the screen.
    glow = max(0.0, min(1.0, glow))
    if glow > 0.0 and ring is not None:
        from pickhero.ui.shapes import glow as shape_glow
        shape_glow(surface, rect.inflate(ring_width * 2, ring_width * 2), ring,
                   spread=12, alpha=round(155 * glow), radius=radius + ring_width)

    from pickhero.ui.shapes import rounded_rect
    if ring is not None:
        outer = rect.inflate(ring_width * 2, ring_width * 2)
        rounded_rect(surface, ring, outer, width=ring_width,
                     border_radius=radius + ring_width)
    rounded_rect(surface, fill, rect, border_radius=radius)


_sprites: dict = {}
# A resize drag produces a new card height per frame, so the cache is bounded.
_SPRITE_LIMIT = 240


def sprite(key: tuple, size: tuple[int, int], paint) -> pygame.Surface:
    """A cached transparent surface, built once by `paint(surf)`.

    Every one of these was being allocated and cleared on every frame it was
    used. Allocating a 1280 by 520 SRCALPHA surface costs the better part of a
    millisecond on its own, which is a twentieth of the whole frame budget for
    a shape that never changes.
    """
    found = _sprites.get(key)
    if found is None:
        if len(_sprites) > _SPRITE_LIMIT:
            _sprites.clear()
        found = pygame.Surface(size, pygame.SRCALPHA)
        paint(found)
        _sprites[key] = found
    return found


def _halo_sprite(size, radius: int, colour, alpha: int) -> pygame.Surface:
    """A cached rounded halo.

    This used to allocate a fresh SRCALPHA surface per halo per note per
    frame. Allocating and clearing one is the single most expensive thing in
    the draw loop, and a passage with six glowing notes did it eighteen times
    a frame.
    """
    key = ("halo", size, radius, colour, alpha)
    sprite = _sprites.get(key)
    if sprite is None:
        if len(_sprites) > _SPRITE_LIMIT:
            _sprites.clear()
        sprite = pygame.Surface(size, pygame.SRCALPHA)
        pygame.draw.rect(sprite, with_alpha(colour, alpha), sprite.get_rect(),
                         border_radius=radius)
        _sprites[key] = sprite
    return sprite


def ghost(
    surface: pygame.Surface,
    rect: pygame.Rect,
    colour: tuple[int, int, int],
    *,
    radius: int = RADIUS_LG,
    alpha: int = 64,
    circle: bool = False,
) -> None:
    """An outline-only note, for one that has passed without being played.

    There is exactly one ghost style on purpose. The mockup had two that looked
    almost identical and meant different things, which is worse than having
    none.

    `circle` follows the look's note shape. A look that draws its notes as
    discs drew its ghosts as rounded rectangles, which was the only place on
    that stage where a note changed shape -- and it changed it at the exact
    moment the note stopped being playable, which reads as a different kind
    of thing rather than the same thing, spent.
    """
    from pickhero.ui.shapes import ellipse, rounded_rect
    if circle:
        r = min(rect.width, rect.height) // 2 - 1
        if r > 0:
            ellipse(surface, with_alpha(colour, alpha),
                    pygame.Rect(rect.centerx - r, rect.centery - r, r * 2, r * 2), width=2)
    else:
        rounded_rect(surface, with_alpha(colour, alpha), rect,
                     width=2, border_radius=radius)


# Tracked strings get their own cache. They are built glyph by glyph, which
# is far too slow per frame, and keying them into the shared sprite cache
# would evict the note bodies and halos every time a label moved: that cache
# holds 240 entries and the chrome alone asks for more than half of it.
_tracked: dict[tuple, pygame.Surface] = {}
_TRACKED_LIMIT = 160


def printable(value: str) -> str:
    """What pygame can draw: no NUL, no control codes, no lone surrogates.

    Font.render raises on a NUL and on an unpaired surrogate, and nothing
    guards the frame, so one such character ended the app. File metadata is
    cleaned on import now, but a Windows file name may carry a lone surrogate
    and a hand-made language pack may carry anything. Line breaks and tabs
    become spaces, the rest of the control ranges go, a lone surrogate
    becomes the replacement character.
    """
    if value.isprintable():
        return value
    out = []
    for ch in value:
        code = ord(ch)
        if ch in "\t\n\r\x0b\x0c":
            out.append(" ")
        elif code < 0x20 or 0x7F <= code < 0xA0:
            continue
        elif 0xD800 <= code <= 0xDFFF:
            out.append("�")
        else:
            out.append(ch)
    return "".join(out)


def text(
    surface: pygame.Surface,
    value: str,
    pos: tuple[int, int],
    colour: tuple[int, int, int],
    size: int = TEXT_MD,
    *,
    bold: bool = False,
    mono: bool = False,
    align: str = "left",
    outline: tuple[int, int, int] | None = None,
    face: str | None = None,
    weight: str | None = None,
    italic: bool = False,
    tracking: float = 0.0,
    upper: bool = False,
) -> pygame.Rect:
    """Draw text and return the rect it occupied.

    `align` is left, center or right and refers to `pos[0]`.

    `tracking` is extra letterspacing in ems, which the drawings use for
    every small-caps label -- eight distinct values from +0.01 to +0.27. It
    is what makes a label read as a label rather than as small body text, and
    pygame has no setting for it, so a tracked string is composed glyph by
    glyph and cached.

    `upper` uppercases at the draw site. The catalogue stays as written: a
    German noun is capitalised in the middle of a sentence and uppercasing in
    the catalogue would make the string unusable anywhere else, besides being
    wrong for languages that case differently.
    """
    if upper:
        value = value.upper()
    value = printable(value)
    face_obj = font(size, bold=bold, mono=mono, face=face, weight=weight,
                    italic=italic)
    if tracking:
        surf = _tracked_surface(value, face_obj, colour, size, tracking)
    else:
        surf = face_obj.render(value, True, colour)
    x, y = pos
    if align == "center":
        x -= surf.get_width() // 2
    elif align == "right":
        x -= surf.get_width()
    if outline is not None:
        shadow = (_tracked_surface(value, face_obj, outline, size, tracking)
                  if tracking else face_obj.render(value, True, outline))
        for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1), (-1, -1), (1, 1)):
            surface.blit(shadow, (x + dx, y + dy))
    surface.blit(surf, (x, y))
    return pygame.Rect(x, y, surf.get_width(), surf.get_height())


def _tracked_surface(value: str, face_obj: pygame.font.Font,
                     colour: tuple[int, int, int], size: int,
                     tracking: float) -> pygame.Surface:
    """One string, letterspaced, cached by everything that changes it."""
    key = (value, id(face_obj), colour, size, round(tracking, 4))
    hit = _tracked.get(key)
    if hit is not None:
        return hit
    extra = tracking * size
    widths = [face_obj.size(ch)[0] for ch in value]
    total = int(sum(widths) + extra * max(0, len(value) - 1)) + 1
    height = face_obj.get_height()
    layer = pygame.Surface((max(1, total), height), pygame.SRCALPHA)
    cursor = 0.0
    for ch, w in zip(value, widths):
        # A glyph with no width -- a soft hyphen, a zero-width space -- makes
        # Font.render raise "Text has zero width". Drawn whole, the string
        # renders them as nothing; drawn letter by letter, each needs skipping.
        if ch != " " and w > 0:
            layer.blit(face_obj.render(ch, True, colour), (int(cursor), 0))
        cursor += w + extra
    if len(_tracked) >= _TRACKED_LIMIT:
        _tracked.clear()
    _tracked[key] = layer
    return layer


def elide(value: str, fits, *, strip: bool = True) -> str:
    """The longest prefix of `value` that fits with an ellipsis after it.

    `fits(candidate)` says whether a string fits. Found by bisection: the
    helpers this replaces dropped one character at a time and re-measured the
    whole remainder at every step -- quadratic in the length of the string,
    for every label, every frame.
    """
    if fits(value):
        return value
    lo, hi = 0, len(value)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if fits(value[:mid] + "…"):
            lo = mid
        else:
            hi = mid - 1
    cut = value[:lo].rstrip() if strip else value[:lo]
    return (cut + "…") if cut else ""


def text_width(value: str, size: int = TEXT_MD, *, bold: bool = False,
               mono: bool = False, face: str | None = None,
               weight: str | None = None, italic: bool = False,
               tracking: float = 0.0, upper: bool = False) -> int:
    """What `text` would occupy, without drawing it.

    Layout code needs this to place a column before it knows what goes in it,
    and measuring by drawing to a scratch surface is both slow and the kind
    of thing that quietly stops matching the real call.
    """
    if upper:
        value = value.upper()
    value = printable(value)
    face_obj = font(size, bold=bold, mono=mono, face=face, weight=weight,
                    italic=italic)
    if not tracking:
        return face_obj.size(value)[0]
    extra = tracking * size
    return int(sum(face_obj.size(ch)[0] for ch in value)
               + extra * max(0, len(value) - 1)) + 1


def pill(
    surface: pygame.Surface,
    rect: pygame.Rect,
    label: str,
    hint: str = "",
    *,
    fill: tuple[int, int, int] | None = None,
    label_colour: tuple[int, int, int] | None = None,
    hint_colour: tuple[int, int, int] | None = None,
    active: bool = False,
    accent: tuple[int, int, int] | None = None,
) -> None:
    """A labelled control with its keyboard shortcut underneath.

    Single letters in a status bar are only readable to whoever wrote them.

    Every colour here used to default to a literal picked against a dark page.
    pill() draws the entire playing-screen toolbar, so on the light theme the
    whole bottom bar was a row of near-black slabs, and the shortcut letters
    measured 4.36 to 1 against their own fill in the theme they were chosen
    for.
    """
    from pickhero.ui.colors import get_look
    look = get_look()
    base = look.bg
    fill = fill if fill is not None else surface_colour(base)
    accent = accent if accent is not None else look.hud_accent
    body = accent if active else fill
    label_colour = label_colour if label_colour is not None else (
        on_accent(accent) if active else ink(base, 1.0))
    hint_colour = hint_colour if hint_colour is not None else (
        mix(on_accent(accent), accent, 0.30) if active else ink(base, 0.55))

    pygame.draw.rect(surface, body, rect, border_radius=RADIUS_MD)
    if not active:
        pygame.draw.rect(surface, stroke(base, 1), rect, width=1,
                         border_radius=RADIUS_MD)
    cx = rect.centerx
    if hint:
        text(surface, label, (cx, rect.y + 10), label_colour, TEXT_SM,
             bold=True, align="center")
        text(surface, hint, (cx, rect.y + 30), hint_colour, TEXT_XS,
             align="center")
    else:
        face = font(TEXT_SM, bold=True)
        text(surface, label, (cx, rect.centery - face.get_height() // 2),
             label_colour, TEXT_SM, bold=True, align="center")


def toggle_dot(
    surface: pygame.Surface,
    centre: tuple[int, int],
    on: bool,
    *,
    on_colour: tuple[int, int, int] | None = None,
    off_colour: tuple[int, int, int] | None = None,
) -> None:
    """On/off indicator that differs in fill as well as colour.

    The mockup drew both states as a coloured dot, so on and off looked the
    same at a glance.

    Both colours default to the look rather than to a constant, because the
    dot is drawn on whatever page the look chose.
    """
    if on:
        if on_colour is None:
            from pickhero.ui.colors import get_look
            on_colour = get_look().feedback_hit
        pygame.draw.circle(surface, on_colour, centre, 5)
    else:
        if off_colour is None:
            from pickhero.ui.colors import get_look
            off_colour = stroke(get_look().bg, 1)
        pygame.draw.circle(surface, off_colour, centre, 5, width=2)


def banner(
    surface: pygame.Surface,
    rect: pygame.Rect,
    message: str,
    *,
    colour: tuple[int, int, int] | None = None,
    text_colour: tuple[int, int, int] | None = None,
) -> None:
    """A full-width strip for something the player must not miss.

    The colours come from the theme. They used to be literals chosen against
    a dark page, and the text measured 3.89 to 1 on its own strip -- under
    4.5 -- in both themes. The strip that says "I cannot hear you" was the
    hardest thing on screen to read.
    """
    from pickhero.ui.colors import get_look
    look = get_look()
    pygame.draw.rect(surface, colour or look.banner_bg, rect)
    text(surface, message, (rect.centerx, rect.centery - 11),
         text_colour or look.banner_text, TEXT_MD, bold=True, align="center")


def pulse(now: float, period: float = 1.0) -> float:
    """A 0..1 triangle wave, for anything that should breathe rather than blink."""
    phase = (now % period) / period
    return 1.0 - abs(phase * 2.0 - 1.0)


def ease_out(t: float) -> float:
    return 1.0 - (1.0 - max(0.0, min(1.0, t))) ** 3


def circle_ring(
    surface: pygame.Surface,
    centre: tuple[int, int],
    radius: int,
    colour: tuple[int, int, int],
    width: int = 3,
    alpha: int = 255,
) -> None:
    if alpha >= 255:
        pygame.draw.circle(surface, colour, centre, radius, width=width)
        return
    size = radius * 2 + width * 2

    def paint(layer):
        pygame.draw.circle(layer, with_alpha(colour, alpha),
                           (size // 2, size // 2), radius, width=width)

    layer = sprite(("ring", size, radius, width, colour, alpha),
                   (size, size), paint)
    surface.blit(layer, (centre[0] - size // 2, centre[1] - size // 2))
