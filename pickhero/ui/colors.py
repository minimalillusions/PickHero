"""The three looks.

A Look is one complete appearance: a palette, a typeface, and which stage
geometry the playing screen draws. There is no dark/light axis on top of it.
Each look ships the palette it was designed with and is never inverted -- six
combinations were the alternative, and the two nobody drew are exactly the two
that would have looked wrong.

    STUDIO   dark, calm, colour only on the strings
    STAGE    dark violet, a rhythm-game highway
    HIFI     warm light, like a good amplifier

Eight of the old theme's thirty-five fields were read by nothing at all:
lane_bg_even, lane_bg_odd, lane_line, note_text, note_border,
feedback_streak, signal_hot, signal_warm. They were still required arguments,
so three looks would have meant inventing twenty-four colours that never reach
a pixel. They are gone.

Nothing caches a Look. get_look() is called fresh inside every render() at
60 fps, so the frame after a switch is already right. That is the whole
propagation story, and it is why there is no invalidation machinery here.
"""

from dataclasses import dataclass

RGB = tuple[int, int, int]
RGBA = tuple[int, int, int, int]

# The only two geometries that exist. `stage` is read by a handful of ifs in
# scrolling.py and by nothing else.
STAGES = ("lanes", "highway")


@dataclass(frozen=True, kw_only=True)
class Look:
    """One complete appearance.

    kw_only, so the groups below are ordered by what they mean rather than by
    which of them happen to carry a default. Every COLOUR is stated by every
    look on purpose: a default that is right for a dark look is silently wrong
    for a light one, which is how the old light theme nearly shipped with a
    dark stage and a dark-tuned warning banner.
    """

    # -- Identity ---------------------------------------------------------
    key: str                     # config value and catalogue suffix
    stage: str = "lanes"         # "lanes" | "highway"

    # -- Type -------------------------------------------------------------
    face_sans: str = ""          # "" means the platform stack in design.py
    face_mono: str = ""
    type_scale: float = 1.0      # cap-height correction, 1.0 until measured
    tracking: float = 0.0        # em, chrome labels only; 0.0 is the fast path

    # -- Stage shape ------------------------------------------------------
    note_shape: str = "card"     # "card" | "circle" | "pill"
    string_style: str = "line"   # "line" | "wire"
    # None means "use the string colour", which is what Studio and Stage do.
    # Hi-fi draws dark discs instead and lets the gutter carry the string
    # colour -- which is what lets its stage be light at all: the palette is
    # equiluminant, so it cannot sit on paper, but a near-black disc can.
    note_fill: RGB | None = None
    note_ink: RGB | None = None

    # -- The page ---------------------------------------------------------
    # The mockups' two panels differ, and by how much is the whole character
    # of the look: Studio drops 5 points from field to sidebar, Hi-fi 9 out
    # of 236 -- a hair, which is the Braun move. Arithmetic on one background
    # cannot produce both, which is why these are stated rather than derived.
    panel_left: RGB              # the sidebar / the now-playing column
    panel_right: RGB             # the list's field
    panel_rule: RGB              # what divides them, 1 or 2 px
    popover: RGB                 # the setup card, floating above the page

    # Four steps of text, measured off the drawings. design.ink() walks from
    # the background towards one end and cannot reach these: its quietest
    # step on Studio is #7E8187 where the drawing says #6B6B74, and on Hi-fi
    # it can only go darker, so the popover was unreachable at any argument.
    ink_1: RGB                   # titles
    ink_2: RGB                   # body
    ink_3: RGB                   # meta, key hints
    ink_4: RGB                   # the quietest thing that is still a word

    # Three weights of rule. None of them is a control boundary -- they are
    # decoration on a large field, and they are deliberately below the 3:1
    # the contrast suite asks of a control, because that is what makes the
    # drawings look expensive rather than wireframed. See tests/test_contrast.
    hairline_quiet: RGB          # between rows
    hairline: RGB                # between sections
    hairline_loud: RGB           # the panel divide, bar lines

    select_fill: RGB             # the selected row's fill
    track_empty: RGB             # an unfilled progress rail

    bg: RGB
    menu_bg: RGB
    menu_item: RGB
    menu_selected: RGB
    menu_selected_bg: RGB
    menu_check: RGB
    hud_text: RGB
    hud_accent: RGB

    # -- The stage --------------------------------------------------------
    # A look may have a light stage only if it also brings a note_fill. The
    # string palette is equiluminant: on paper, amber measures 1.33:1 and the
    # hit ring 1.42:1, so a look that keeps string-coloured notes has to keep
    # a dark stage with them. Hi-fi pays the price and gets the paper.
    stage_bg: RGB
    stage_lane_even: RGB
    stage_lane_odd: RGB
    stage_beat_line: RGB
    stage_bar_line: RGB
    hit_zone: RGB

    # -- Verdicts ---------------------------------------------------------
    feedback_hit: RGB
    feedback_close: RGB
    feedback_miss: RGB
    # Timing is not a verdict on the note: the right note played early is
    # still the right note. It gets a colour of its own, off the green-amber-
    # red scale entirely, so "zu frueh" can never be mistaken for "falsch".
    feedback_early: RGB

    # -- Loop -------------------------------------------------------------
    loop_marker: RGB
    loop_marker_disabled: RGB
    loop_region: RGBA
    loop_region_disabled: RGBA

    # -- Meters -----------------------------------------------------------
    signal_cold: RGB
    tuner_in_tune: RGB
    tuner_close: RGB
    tuner_off: RGB

    # -- Warning strip ----------------------------------------------------
    banner_bg: RGB
    banner_text: RGB


STUDIO = Look(
    key="studio", face_sans="geist", face_mono="mono",
    # Measured off the drawing, not derived. Field #111112, sidebar #0C0C0D:
    # five points apart, which is all a Studio panel divide is.
    panel_left=(12, 12, 13),
    panel_right=(17, 17, 18),
    panel_rule=(28, 28, 32),
    popover=(24, 24, 27),
    ink_1=(236, 236, 239),
    ink_2=(180, 180, 188),
    # #6B6B74 in the drawing measures 3.58:1 on the field. The hue is kept
    # and the step lifted to clear 4.5, which is two points of lightness and
    # the difference between a hint the player can read and one the player cannot.
    ink_3=(138, 138, 147),
    ink_4=(107, 107, 116),
    hairline_quiet=(27, 27, 32),
    hairline=(33, 33, 37),
    hairline_loud=(49, 49, 56),
    select_fill=(28, 28, 32),
    track_empty=(42, 42, 48),
    bg=(17, 17, 19),
    menu_bg=(17, 17, 19),
    menu_item=(180, 180, 188),
    menu_selected=(236, 236, 239),
    menu_selected_bg=(34, 49, 70),
    menu_check=(103, 210, 131),
    hud_text=(200, 200, 220),
    hud_accent=(61, 191, 226),
    stage_bg=(20, 20, 30),
    stage_lane_even=(30, 30, 42),
    stage_lane_odd=(25, 25, 36),
    stage_beat_line=(60, 60, 80),
    stage_bar_line=(104, 106, 134),
    hit_zone=(255, 255, 255),
    feedback_hit=(40, 235, 170),
    feedback_close=(255, 220, 50),
    feedback_miss=(255, 50, 50),
    feedback_early=(120, 170, 255),
    loop_marker=(0, 200, 255),
    loop_marker_disabled=(0, 80, 110),
    loop_region=(0, 200, 255, 25),
    loop_region_disabled=(0, 80, 110, 15),
    signal_cold=(70, 70, 90),
    tuner_in_tune=(50, 220, 80),
    tuner_close=(220, 200, 40),
    tuner_off=(220, 100, 40),
    banner_bg=(176, 92, 24),
    banner_text=(255, 248, 240),
)

STAGE = Look(
    key="stage", face_sans="chakra", face_mono="mono",
    stage="highway",
    note_shape="pill",
    panel_left=(26, 21, 36),
    panel_right=(13, 8, 24),
    panel_rule=(52, 34, 86),
    popover=(26, 21, 38),
    ink_1=(243, 238, 252),
    ink_2=(186, 178, 204),
    ink_3=(140, 132, 158),
    ink_4=(112, 104, 130),
    hairline_quiet=(30, 19, 50),
    hairline=(44, 30, 72),
    hairline_loud=(74, 52, 118),
    select_fill=(30, 19, 50),
    track_empty=(44, 34, 64),
    bg=(13, 8, 24),
    menu_bg=(13, 8, 24),
    menu_item=(186, 176, 208),
    menu_selected=(243, 238, 252),
    menu_selected_bg=(52, 30, 92),
    menu_check=(101, 226, 135),
    hud_text=(214, 205, 233),
    hud_accent=(0, 215, 243),
    stage_bg=(16, 10, 30),
    stage_lane_even=(30, 19, 55),
    stage_lane_odd=(24, 15, 44),
    stage_beat_line=(66, 46, 108),
    stage_bar_line=(122, 92, 184),
    hit_zone=(243, 238, 252),
    feedback_hit=(101, 226, 135),
    feedback_close=(253, 196, 54),
    feedback_miss=(255, 82, 116),
    feedback_early=(120, 170, 255),
    loop_marker=(0, 215, 243),
    loop_marker_disabled=(28, 74, 96),
    loop_region=(0, 215, 243, 28),
    loop_region_disabled=(28, 74, 96, 16),
    signal_cold=(66, 52, 96),
    tuner_in_tune=(101, 226, 135),
    tuner_close=(253, 196, 54),
    tuner_off=(255, 128, 72),
    banner_bg=(150, 62, 128),
    banner_text=(255, 245, 252),
)

HIFI = Look(
    key="hifi", face_sans="hanken", face_mono="mono",
    string_style="wire",
    note_shape="circle",
    note_fill=(29, 28, 26),
    note_ink=(245, 244, 240),
    # Braun, not "the light one". The two panels are nine points apart out of
    # 236 -- #E3E1DB against #ECEBE7 -- and the rule between them is #D7D4CC.
    # What makes this look expensive is that almost nothing is a shape: the
    # separation is a hair, the space is large, and one orange marks one
    # thing. Every value here is sampled from the drawing.
    panel_left=(227, 225, 219),
    panel_right=(236, 235, 231),
    panel_rule=(215, 212, 204),
    popover=(245, 244, 240),
    ink_1=(28, 27, 26),
    ink_2=(74, 72, 68),
    # The drawing's two quietest greys measure 2.50:1 and 3.04:1. Darkened
    # until they clear 4.5 while keeping the warm cast, which is the only
    # part of them anyone can see.
    ink_3=(98, 95, 88),
    ink_4=(124, 120, 112),
    hairline_quiet=(222, 219, 212),
    hairline=(217, 213, 207),
    hairline_loud=(199, 195, 186),
    select_fill=(240, 238, 234),
    track_empty=(213, 210, 202),
    bg=(236, 235, 231),
    menu_bg=(236, 235, 231),
    menu_item=(85, 82, 76),
    menu_selected=(29, 28, 26),
    menu_selected_bg=(216, 206, 194),
    menu_check=(46, 122, 62),
    hud_text=(60, 58, 54),
    hud_accent=(158, 81, 30),
    stage_bg=(236, 235, 231),
    stage_lane_even=(236, 235, 231),
    stage_lane_odd=(236, 235, 231),
    stage_beat_line=(211, 208, 201),
    stage_bar_line=(136, 131, 120),
    # The drawing's orange is (195,107,53) and it writes the active note's
    # fret number on it in near-white, which measures 3.46:1 -- under the bar
    # for the one glyph the player is reading at that exact moment. Nine perceptual
    # units darker carries the white at 4.86 and stands on the paper itself
    # at 4.49, and side by side with the drawing it is the same orange.
    hit_zone=(165, 85, 36),
    feedback_hit=(34, 122, 58),
    feedback_close=(112, 80, 0),
    feedback_miss=(178, 46, 38),
    feedback_early=(38, 82, 158),
    loop_marker=(195, 108, 54),
    loop_marker_disabled=(140, 112, 92),
    loop_region=(195, 108, 54, 30),
    loop_region_disabled=(140, 112, 92, 16),
    signal_cold=(150, 142, 130),
    tuner_in_tune=(46, 122, 62),
    tuner_close=(158, 116, 20),
    tuner_off=(170, 70, 26),
    banner_bg=(146, 68, 8),
    banner_text=(255, 248, 240),
)

_LOOKS = {look.key: look for look in (STUDIO, STAGE, HIFI)}
# The order the key cycles through. A tuple, because with three of them the
# old "is this the dark one? then the light one" no longer works.
_ORDER = ("studio", "stage", "hifi")
_current: Look = STUDIO


def set_look(key: str) -> None:
    """Make `key` the active look. An unknown key falls back to Studio."""
    global _current
    _current = _LOOKS.get(key, STUDIO)


def get_look() -> Look:
    return _current


def look_name_key(key: str) -> str:
    """The catalogue key holding a look's own name."""
    return f"look.{key}"


def cycle_look() -> str:
    """Step to the next look. Returns its key."""
    try:
        index = _ORDER.index(_current.key)
    except ValueError:
        index = -1
    set_look(_ORDER[(index + 1) % len(_ORDER)])
    return _current.key


def looks() -> tuple[str, ...]:
    return _ORDER


# There is one string palette and it lives in design.py. There used to be two:
# the lanes painted the E string magenta from design.STRING_PALETTE while the
# help legend and the tuning checklist painted it purple from this dict, so the
# screen explaining what the colours mean taught the wrong colour for every
# single string. Re-exported rather than duplicated. It is deliberately NOT a
# per-look decision: all three mockups picked the same four hues.
def string_colour(string: int) -> RGB:
    from pickhero.ui.design import string_colour as _palette
    return _palette(string)


def dimmed(color: RGB, factor: float = 0.4) -> RGB:
    """Darken a color by multiplying each channel by factor."""
    return (int(color[0] * factor), int(color[1] * factor), int(color[2] * factor))
