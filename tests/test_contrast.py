"""Every colour pair the app draws, measured, in every theme.

The light theme shipped with its header buttons, its footer buttons, its song
rows, its search box, its bar lines and its progress trough all drawn in their
own background colour. Measured contrast 1.01 to 1: not dim, not subtle --
mathematically the same colour. One idiom caused all of it, "a raised surface
is the background lightened", which lifts a control off a dark page and sinks
it into a light one.

Nobody saw it because nobody could run the app in light mode and look. So it
is measured here instead, and the measurement is the guard: any new look, or
any drawing code that goes back to deriving a colour by arithmetic on the
background, fails this file.

Thresholds are WCAG 2.1: 4.5:1 for body text, 3:1 for large text and for the
boundary of a user interface component (1.4.11). They are not aspirations --
the numbers below all clear them with room to spare, so a failure here means
something genuinely regressed.
"""

import pytest

from pickhero.ui import design
from pickhero.ui.colors import _LOOKS

LOOKS = sorted(_LOOKS.items())
LOOK_IDS = [name for name, _ in LOOKS]

BODY = 4.5      # text below roughly 18 px
LARGE = 3.0     # large or bold text, and component boundaries
COMPONENT = 3.0


def ratio(a, b) -> float:
    return design.contrast(a, b)


class TestPolarityPrimitives:
    """ink, surface and stroke must work on any page, either way up."""

    @pytest.mark.parametrize("name,look", LOOKS, ids=LOOK_IDS)
    def test_a_control_boundary_is_visible_against_the_page(self, name, look):
        for base in (look.bg, look.menu_bg):
            assert ratio(design.stroke(base, 1), base) >= COMPONENT

    @pytest.mark.parametrize("name,look", LOOKS, ids=LOOK_IDS)
    def test_a_control_boundary_is_visible_against_its_own_fill(self, name, look):
        """This is the one that failed. The border was the last chance to see
        a button whose fill had vanished, and it had vanished too."""
        for base in (look.bg, look.menu_bg):
            assert ratio(design.stroke(base, 1), design.surface(base, 1)) >= COMPONENT

    @pytest.mark.parametrize("name,look", LOOKS, ids=LOOK_IDS)
    def test_title_ink_is_readable_on_every_surface_level(self, name, look):
        for base in (look.bg, look.menu_bg):
            for level in range(len(design.SURFACE_LEVELS)):
                assert ratio(design.ink(base, 1.0), design.surface(base, level)) >= BODY

    @pytest.mark.parametrize("name,look", LOOKS, ids=LOOK_IDS)
    def test_secondary_ink_is_readable(self, name, look):
        for base in (look.bg, look.menu_bg):
            assert ratio(design.ink(base, 0.7), design.surface(base, 1)) >= BODY

    @pytest.mark.parametrize("name,look", LOOKS, ids=LOOK_IDS)
    def test_the_quietest_ink_still_clears_body_text(self, name, look):
        """Keyboard hints are drawn at 13 px, so they are body text and need
        4.5:1. The old shade(hud_text, 0.4) reached 2.42 even in dark mode."""
        for base in (look.bg, look.menu_bg):
            assert ratio(design.ink(base, 0.5), design.surface(base, 1)) >= BODY

    @pytest.mark.parametrize("name,look", LOOKS, ids=LOOK_IDS)
    def test_the_hierarchy_does_not_invert(self, name, look):
        """A subtitle must be quieter than a title and louder than a hint. On
        the light look, shade() multiplied channels down and therefore made
        muted text darker -- so the keyboard hints outshouted the song titles."""
        base = look.menu_bg
        fill = design.surface(base, 1)
        title = ratio(design.ink(base, 1.0), fill)
        sub = ratio(design.ink(base, 0.7), fill)
        hint = ratio(design.ink(base, 0.5), fill)
        assert title > sub > hint

    @pytest.mark.parametrize("name,look", LOOKS, ids=LOOK_IDS)
    def test_a_sunken_well_separates_from_its_surface(self, name, look):
        for base in (look.bg, look.menu_bg):
            assert ratio(design.sunken(base), design.surface(base, 1)) >= 1.25

    def test_is_light_agrees_with_the_themes(self):
        from pickhero.ui.colors import STUDIO, HIFI
        assert design.is_light(HIFI.bg) is True
        assert design.is_light(STUDIO.bg) is False


class TestContrastHelper:
    def test_black_on_white_is_the_maximum(self):
        assert design.contrast((0, 0, 0), (255, 255, 255)) == pytest.approx(21.0, abs=0.01)

    def test_a_colour_against_itself_is_one(self):
        assert design.contrast((123, 45, 67), (123, 45, 67)) == pytest.approx(1.0)

    def test_it_is_symmetric(self):
        a, b = (12, 200, 90), (240, 30, 60)
        assert design.contrast(a, b) == pytest.approx(design.contrast(b, a))


class TestStringPalette:
    """The note cards and everything drawn on them."""

    def test_the_fret_number_is_readable_on_every_string(self):
        """One dark ink on all six. White would fail on five of them."""
        for number in range(1, 7):
            assert ratio(design.FRET_INK, design.string_colour(number)) >= BODY

    def test_neighbouring_lanes_are_clearly_different(self):
        """Measured as perceptual distance, not contrast ratio: the palette is
        deliberately equiluminant so that no string looks brighter and
        therefore more important, which means a luminance ratio calls them all
        identical while the eye has no trouble at all.

        Neighbours are what matters. Reaching one lane too far is the mistake
        the colours exist to prevent, and every neighbouring pair sits between
        62 and 99 -- unmistakable.
        """
        for a in range(1, 6):
            found = design.delta_e(design.string_colour(a),
                                   design.string_colour(a + 1))
            assert found >= 50.0, f"Saite {a} und {a + 1}: dE {found:.1f}"

    def test_no_two_lanes_are_the_same_colour(self):
        """Six equiluminant hues is a crowded wheel, and the weakest pair is
        violet against blue at 24. They are strings 4 and 6 -- two lanes apart,
        with a third between them, and only ever both on screen for a six
        string guitar. Distinguishable rather than unmistakable is the right
        trade there; making it wider costs a neighbouring pair somewhere else.
        """
        for a in range(1, 7):
            for b in range(a + 1, 7):
                found = design.delta_e(design.string_colour(a),
                                       design.string_colour(b))
                assert found >= 22.0, f"Saite {a} und {b}: dE {found:.1f}"

    @pytest.mark.parametrize("name,look", LOOKS, ids=LOOK_IDS)
    def test_no_string_can_be_mistaken_for_a_verdict(self, name, look):
        """No string is red, because red means missed and a permanently red
        string would read as a permanent mistake. The same holds for the other
        two verdicts, and it is the constraint that stops the palette simply
        spreading out to fill the wheel.

        Measured against each look's own verdicts rather than the module
        constants: those are no longer what gets drawn, and a test that
        guards a value nobody paints is not guarding anything. Doing that
        found Studio's hit green sitting dE 14.9 from string 5's green -- a
        hit ring the same colour as the card it goes round -- and Hi-fi's
        amber at dE 38.8 from string 2 while also being the dimmest colour
        on the result panel at 3.22:1. Both were moved.

        The floor is 35 rather than 40 because that is where the palette
        actually sits once those two are fixed: the tightest remaining pair
        is Stage's amber against the orange second string at 36.1. Pushing
        that one past 40 means a near-pure yellow, which costs the amber its
        meaning for a gain nobody can see -- and the verdict is carried by
        the ring's thickness as well as its colour.
        """
        for number in range(1, 7):
            for verdict, label in ((look.feedback_hit, "Treffer"),
                                   (look.feedback_close, "knapp"),
                                   (look.feedback_miss, "Fehler")):
                found = design.delta_e(design.string_colour(number), verdict)
                assert found >= 35.0, f"Saite {number} gegen {label}: dE {found:.1f}"

    def test_there_is_exactly_one_string_palette(self):
        """There were two, and the legend explaining the colours used the
        other one -- so it taught the wrong colour for every string."""
        from pickhero.ui import colors
        assert not hasattr(colors, "STRING_COLORS") or \
            colors.STRING_COLORS is design.STRING_PALETTE


class TestVerdictRings:
    """Hit, close and miss are carried by a ring around the card."""

    @pytest.mark.parametrize("name,look", LOOKS, ids=LOOK_IDS)
    def test_every_verdict_reads_against_the_lane(self, name, look):
        """The ring is drawn on the lane, not on the card, because
        pygame.draw.rect with width= paints INSIDE the rect -- over the card's
        own fill. Measured there, the hit ring was 1.07:1 on the amber string."""
        for lane in (look.stage_lane_even, look.stage_lane_odd):
            for verdict in (look.feedback_hit, look.feedback_close,
                            look.feedback_miss):
                assert ratio(verdict, lane) >= COMPONENT

    @pytest.mark.parametrize("name,look", LOOKS, ids=LOOK_IDS)
    def test_the_three_verdicts_are_distinguishable_from_each_other(self, name, look):
        """Also by hue, for the same reason. Amber against red is the pair
        that protanopia and deuteranopia collapse, so close and miss are the
        two that most need real separation."""
        for a, b, label in ((look.feedback_hit, look.feedback_miss, "Treffer/Fehler"),
                            (look.feedback_close, look.feedback_miss, "knapp/Fehler"),
                            (look.feedback_hit, look.feedback_close, "Treffer/knapp")):
            found = design.delta_e(a, b)
            assert found >= 35.0, f"{label}: dE {found:.1f}"

    @pytest.mark.parametrize("name,look", LOOKS, ids=LOOK_IDS)
    def test_timing_is_not_on_the_verdict_scale(self, name, look):
        """Early and late are not degrees of wrong.

        The right note played early is the right note, so its colour sits off
        the green-amber-red scale entirely. If it drifted towards any of the
        three, "zu frueh" would start to read as a mistake -- which is the one
        thing this app must never tell them.
        """
        for verdict, label in ((look.feedback_hit, "Treffer"),
                               (look.feedback_close, "knapp"),
                               (look.feedback_miss, "Fehler")):
            found = design.delta_e(look.feedback_early, verdict)
            assert found >= 40.0, f"Timing gegen {label}: dE {found:.1f}"


class TestTheFourStepsOfInk:
    """Four named text colours per look, measured against the page.

    design.ink() walks from the background towards one end on a curve, and
    the drawings do not sit on that curve: its quietest step on Studio is
    #7E8187 where the drawing says #6B6B74, and on Hi-fi it can only go
    darker than the page, so the popover -- which is lighter -- could not be
    produced at any argument. The steps are stated now, and stated means
    measurable.
    """

    @pytest.mark.parametrize("name,look", LOOKS, ids=LOOK_IDS)
    def test_the_three_reading_steps_clear_body_text(self, name, look):
        """Titles, body and meta all end up in sentences the player has to read.

        The drawings put ink_3 at 3.58:1 (Studio) and 2.50:1 (Hi-fi). The hue
        is kept and the step moved until it clears -- about two points of
        lightness, and the difference between a hint the player can read and one the player
        cannot.
        """
        for step, colour in (("ink_1", look.ink_1), ("ink_2", look.ink_2),
                             ("ink_3", look.ink_3)):
            for page, label in ((look.panel_right, "field"),
                                (look.panel_left, "panel"),
                                (look.popover, "popover")):
                found = ratio(colour, page)
                assert found >= BODY, f"{name} {step} on the {label}: {found:.2f}:1"

    @pytest.mark.parametrize("name,look", LOOKS, ids=LOOK_IDS)
    def test_the_quietest_step_is_large_text_only(self, name, look):
        """ink_4 is for display numerals and tracked labels set large. It is
        not allowed to be used for a sentence, and the number says so: it
        clears the large-text bar and not the body one."""
        found = ratio(look.ink_4, look.panel_right)
        assert found >= LARGE, f"{name} ink_4: {found:.2f}:1"

    @pytest.mark.parametrize("name,look", LOOKS, ids=LOOK_IDS)
    def test_the_steps_are_actually_four_distinct_steps(self, name, look):
        """A scale whose steps collapse is one colour with four names."""
        steps = [look.ink_1, look.ink_2, look.ink_3, look.ink_4]
        for a, b in zip(steps, steps[1:]):
            assert ratio(a, look.panel_right) > ratio(b, look.panel_right), (
                f"{name}: {a} is not louder than {b}")


class TestHairlinesAreDecorationNotControls:
    """The drawings' rules are 1.10:1 to 1.35:1, and that is deliberate.

    WCAG 1.4.11 asks 3:1 of "a user interface component or graphical object
    required to understand the content". A rule between two library rows is
    neither: the rows are already separated by their own spacing and by their
    text, and a 3:1 rule on a dark field is a bright scratch across the page.
    Drawing them at 3:1 is most of why the first attempt looked wireframed
    rather than expensive.

    So the suite splits rather than loosens. A hairline gets this exemption
    only while it is decoration -- and the test below is what stops it ever
    becoming the only thing carrying a boundary.
    """

    @pytest.mark.parametrize("name,look", LOOKS, ids=LOOK_IDS)
    def test_a_hairline_is_visible_at_all(self, name, look):
        """Below about 1.05 it is the background and the drawing loses it."""
        for step, colour in (("quiet", look.hairline_quiet),
                             ("normal", look.hairline),
                             ("loud", look.hairline_loud)):
            found = ratio(colour, look.panel_right)
            assert found >= 1.05, f"{name} hairline {step}: {found:.2f}:1"

    @pytest.mark.parametrize("name,look", LOOKS, ids=LOOK_IDS)
    def test_a_hairline_stays_quieter_than_the_quietest_word(self, name, look):
        """This is the whole exemption. A rule louder than the text beside it
        is no longer decoration -- it has become the loudest thing in the row,
        and at that point it must meet 3:1 like any other component."""
        quietest = ratio(look.ink_4, look.panel_right)
        for step, colour in (("quiet", look.hairline_quiet),
                             ("normal", look.hairline),
                             ("loud", look.hairline_loud)):
            found = ratio(colour, look.panel_right)
            assert found < quietest, (
                f"{name}: the {step} hairline at {found:.2f}:1 is louder than "
                f"ink_4 at {quietest:.2f}:1, so it is not decoration any more")

    @pytest.mark.parametrize("name,look", LOOKS, ids=LOOK_IDS)
    def test_the_three_weights_are_ordered(self, name, look):
        quiet = ratio(look.hairline_quiet, look.panel_right)
        mid = ratio(look.hairline, look.panel_right)
        loud = ratio(look.hairline_loud, look.panel_right)
        assert quiet <= mid <= loud, (quiet, mid, loud)

    @pytest.mark.parametrize("name,look", LOOKS, ids=LOOK_IDS)
    def test_the_two_panels_are_told_apart(self, name, look):
        """Hi-fi separates them by nine points out of 236, which is the whole
        Braun move -- but nine points of nothing is one panel."""
        assert look.panel_left != look.panel_right
        gap = abs(design.luminance(look.panel_left)
                  - design.luminance(look.panel_right))
        assert gap > 0.0015, f"{name}: the panels are the same colour"


class TestTheNoteShePlayedAndTheOneSheMissed:
    """A note that went by unplayed is still a note the player has to read.

    It was drawn in design.shade(string_colour, 0.9) -- the raw string colour,
    barely darkened. On a dark stage that is 4.58:1. On Hi-fi's paper it is
    1.37:1, so the note the player missed was the only one on screen the player could not
    see, on the look where the player most needed to find it.
    """

    @pytest.mark.parametrize("name,look", LOOKS, ids=LOOK_IDS)
    def test_a_missed_note_can_be_read_on_every_stage(self, name, look):
        for number in range(1, 7):
            spent = design.readable(design.string_colour(number), look.stage_bg)
            found = ratio(spent, look.stage_bg)
            assert found >= BODY, f"Saite {number} auf {name}: {found:.2f}:1"

    @pytest.mark.parametrize("name,look", LOOKS, ids=LOOK_IDS)
    def test_it_still_says_which_string_it_was(self, name, look):
        """Legible is not enough: the ghost may not become six grey shapes.

        Pushing every pastel to black would clear the contrast test and
        destroy the one thing the colour is for, so the strings must stay
        apart from each other after the correction.
        """
        spent = [design.readable(design.string_colour(n), look.stage_bg)
                 for n in range(1, 7)]
        for i, a in enumerate(spent):
            for j, b in enumerate(spent[i + 1:], start=i + 1):
                found = design.delta_e(a, b)
                assert found >= 15.0, (
                    f"{name}: Saite {i + 1} und {j + 1} nach der Korrektur "
                    f"nur noch dE {found:.1f} auseinander")

    def test_readable_leaves_a_colour_that_already_works_alone(self):
        """It is a correction, not a filter: a colour that reads is returned
        untouched, so nothing drifts on the looks that were already right."""
        on_dark = (255, 255, 255)
        assert design.readable(on_dark, (10, 10, 12)) == on_dark


class TestNumbersOffTheStage:
    """Every number the app colours by how well the player did.

    These all used design.HIT and design.CLOSE, which are tuned for a dark
    page. On Hi-fi's paper they measured 1.01:1 to 1.59:1 -- nine places where
    the number was drawn and could not be seen at all, including the one on
    the result screen that is the whole point of finishing a song. They read
    from the look now, and this is the guard that keeps them there.
    """

    @pytest.mark.parametrize("name,look", LOOKS, ids=LOOK_IDS)
    def test_the_percentage_in_the_library_is_visible(self, name, look):
        row = design.surface(look.menu_bg, 1)
        for colour in (look.feedback_hit, look.feedback_close):
            assert ratio(colour, row) >= LARGE

    @pytest.mark.parametrize("name,look", LOOKS, ids=LOOK_IDS)
    def test_the_result_screen_is_visible(self, name, look):
        panel = design.surface(look.bg, 2)
        for colour in (look.feedback_hit, look.feedback_close,
                       look.feedback_early):
            assert ratio(colour, panel) >= LARGE

    @pytest.mark.parametrize("name,look", LOOKS, ids=LOOK_IDS)
    def test_the_streak_reads_against_the_hud(self, name, look):
        assert ratio(look.feedback_hit, look.bg) >= LARGE

    @pytest.mark.parametrize("name,look", LOOKS, ids=LOOK_IDS)
    def test_the_timing_chip_can_be_read(self, name, look):
        """The chip is a solid block of the timing colour with the word on
        top, so what matters is the word against the chip, and the chip
        against the stage it sits on."""
        assert ratio(design.on_accent(look.feedback_early),
                     look.feedback_early) >= BODY
        assert ratio(look.feedback_early, look.stage_bg) >= COMPONENT

    @pytest.mark.parametrize("name,look", LOOKS, ids=LOOK_IDS)
    def test_the_input_meter_shows_when_it_is_too_loud(self, name, look):
        """The bar is nearly full when it clips, so its length carries the
        message and the colour is the second cue -- but on paper the red was
        1.87:1 against the trough, which is no cue at all."""
        trough = design.sunken(look.bg)
        assert ratio(look.feedback_miss, trough) >= 2.4
        assert ratio(look.feedback_hit, trough) >= 2.4


class TestStage:
    """The playing field.

    A look either paints its notes in the string palette, and then its stage
    has to be dark enough to carry them, or it brings its own note fill and
    may put the stage on paper. Hi-fi takes the second road: near-black discs
    on a light page, with the string colour moved to the gutter letter.
    """

    @pytest.mark.parametrize("name,look", LOOKS, ids=LOOK_IDS)
    def test_the_note_reads_against_its_lane(self, name, look):
        for lane in (look.stage_lane_even, look.stage_lane_odd):
            if look.note_fill is not None:
                found = ratio(look.note_fill, lane)
                assert found >= COMPONENT, f"{name}: Note {found:.2f}"
                assert ratio(look.note_ink, look.note_fill) >= 4.5, \
                    f"{name}: Bundzahl auf der Note"
                continue
            for number in range(1, 7):
                found = ratio(design.string_colour(number), lane)
                assert found >= COMPONENT, \
                    f"Saite {number} auf {name}: {found:.2f}"

    @pytest.mark.parametrize("name,look", LOOKS, ids=LOOK_IDS)
    def test_bar_lines_are_visible(self, name, look):
        """A beginner counts bars off these. In light mode they measured
        1.14:1 and were simply not there."""
        assert ratio(look.stage_bar_line, look.stage_lane_even) >= COMPONENT

    @pytest.mark.parametrize("name,look", LOOKS, ids=LOOK_IDS)
    def test_stage_text_is_readable(self, name, look):
        assert ratio(design.ink(look.stage_bg, 1.0), look.stage_bg) >= BODY
        assert ratio(design.ink(look.stage_bg, 0.6), look.stage_bg) >= BODY


class TestAccent:
    @pytest.mark.parametrize("name,look", LOOKS, ids=LOOK_IDS)
    def test_text_on_the_accent_is_readable(self, name, look):
        """The Spielen button's label. On the light accent the old (12,16,26)
        measured 3.36 and its 'Enter' hint 2.22."""
        assert ratio(design.on_accent(look.hud_accent), look.hud_accent) >= BODY

    @pytest.mark.parametrize("name,look", LOOKS, ids=LOOK_IDS)
    def test_the_accent_reads_against_the_page(self, name, look):
        for base in (look.bg, look.menu_bg):
            assert ratio(look.hud_accent, base) >= COMPONENT


class TestDifficultyScale:
    """The first thing the eye lands on in a browser row."""

    @pytest.mark.parametrize("name,look", LOOKS, ids=LOOK_IDS)
    def test_every_level_reads_against_the_page(self, name, look):
        for level in range(1, 6):
            found = ratio(design.difficulty_colour(level, look.menu_bg), look.menu_bg)
            assert found >= COMPONENT, f"Stufe {level} auf {name}: {found:.2f}"

    @pytest.mark.parametrize("name,look", LOOKS, ids=LOOK_IDS)
    def test_a_lit_bar_is_louder_than_an_unlit_one(self, name, look):
        """The unlit bars were hardcoded to (52,56,68). On the light theme
        that is 9.85:1 while the lit ones sit at 2 to 4, so a level-1 piece
        read as a level-5 piece -- the glyph said the opposite of the truth."""
        unlit = design.stroke(look.menu_bg, 0)
        for level in range(1, 6):
            lit = design.difficulty_colour(level, look.menu_bg)
            assert ratio(lit, look.menu_bg) >= ratio(unlit, look.menu_bg), \
                f"Stufe {level} auf {name}: unbeleuchtet lauter als beleuchtet"


class TestBanner:
    @pytest.mark.parametrize("name,look", LOOKS, ids=LOOK_IDS)
    def test_the_warning_strip_is_readable(self, name, look):
        """'Ich hoere dich gerade nicht' measured 3.89 in both themes."""
        assert ratio(look.banner_text, look.banner_bg) >= BODY

    @pytest.mark.parametrize("name,look", LOOKS, ids=LOOK_IDS)
    def test_the_strip_reads_as_a_strip(self, name, look):
        assert ratio(look.banner_bg, look.bg) >= COMPONENT


class TestTheThreeLooks:
    """Three looks replace a dark/light switch, and nothing is inverted.

    Each look ships the palette it was drawn with. The alternative was two
    axes and six combinations, two of which nobody designed -- and those two
    are exactly the ones that would have looked wrong.
    """

    def test_there_are_three_and_they_are_named(self):
        from pickhero.ui.colors import _LOOKS, looks
        assert looks() == ("studio", "stage", "hifi")
        assert set(_LOOKS) == set(looks())

    def test_every_look_names_itself_in_both_languages(self):
        from pickhero import i18n
        from pickhero.ui.colors import look_name_key, looks
        for language in ("de", "en"):
            i18n.set_language(language)
            for key in looks():
                name = i18n.t(look_name_key(key))
                assert name and not name.startswith("look."), (
                    f"{language}: {key} has no name")
        i18n.set_language("de")

    def test_cycling_visits_each_look_once_and_returns(self):
        from pickhero.ui.colors import cycle_look, get_look, looks, set_look
        set_look("studio")
        seen = [get_look().key]
        for _ in range(len(looks())):
            seen.append(cycle_look())
        assert seen[:-1] == list(looks())
        assert seen[-1] == seen[0], "cycling does not come back round"
        set_look("studio")

    def test_an_unknown_look_falls_back_rather_than_crashing(self):
        from pickhero.ui.colors import get_look, set_look
        set_look("a-look-that-never-shipped")
        assert get_look().key == "studio"
        set_look("studio")

    def test_every_look_states_every_colour_itself(self):
        # A default that is right for a dark look is silently wrong for a
        # light one. That is how the old light theme nearly shipped with a
        # dark stage: seven fields defaulted to the dark values.
        from dataclasses import MISSING, fields
        from pickhero.ui.colors import Look
        NON_COLOUR = {"key", "stage", "face_sans", "face_mono", "type_scale",
                      "tracking", "note_shape", "string_style",
                      # Both default to None, which means "use the string
                      # palette" -- an absence, not an inherited colour.
                      "note_fill", "note_ink"}
        for field in fields(Look):
            if field.name in NON_COLOUR:
                continue
            assert field.default is MISSING and field.default_factory is MISSING, (
                f"{field.name} carries a default, so a look can inherit a "
                f"colour it never chose")

    def test_no_look_carries_a_field_nothing_reads(self):
        # Eight of the old theme's thirty-five were dead but still required,
        # so three looks meant inventing twenty-four unused colours.
        from dataclasses import fields
        from pickhero.ui.colors import Look
        dead = {"lane_bg_even", "lane_bg_odd", "lane_line", "note_text",
                "note_border", "feedback_streak", "signal_hot", "signal_warm"}
        assert dead.isdisjoint({f.name for f in fields(Look)})

    def test_a_light_stage_is_only_allowed_with_its_own_note_fill(self):
        # Not taste: the string palette is equiluminant, and on paper amber
        # measures 1.33:1. A look may have a light stage, but only if it
        # stops painting notes in the string palette -- which is exactly the
        # trade Hi-fi makes, and why its discs are near-black.
        from pickhero.ui import design
        from pickhero.ui.colors import _LOOKS
        for key, look in _LOOKS.items():
            if design.is_light(look.stage_bg):
                assert look.note_fill is not None, (
                    f"{key} put the stage on paper while still painting "
                    f"notes in the string palette")

    def test_an_old_settings_file_finds_its_successor(self, tmp_path,
                                                      monkeypatch):
        # Exercised through the real load(), not by repeating its arithmetic:
        # a test that restates the mapping passes even when load() never runs
        # it.
        import json

        from pickhero import config as config_module

        for legacy, expected in (("dark", "studio"), ("light", "hifi"),
                                 ("nonsense", "studio")):
            path = tmp_path / f"{legacy}.json"
            path.write_text(json.dumps({"theme": legacy, "instrument": "guitar"}),
                            encoding="utf-8")
            monkeypatch.setattr(config_module, "CONFIG_FILE", path)
            assert config_module.Config.load().look == expected, legacy

    def test_a_file_that_already_knows_about_looks_is_left_alone(self, tmp_path,
                                                                monkeypatch):
        import json

        from pickhero import config as config_module
        path = tmp_path / "new.json"
        path.write_text(json.dumps({"look": "stage", "instrument": "guitar"}),
                        encoding="utf-8")
        monkeypatch.setattr(config_module, "CONFIG_FILE", path)
        assert config_module.Config.load().look == "stage"


class TestTheNoteSheIsPlayingRightNow:
    """The note at the playhead, which is the one glyph the player is reading.

    Hi-fi marks it by colouring it rather than by ringing it, because that is
    what its drawing does and because a ring would be a second mark for the
    same idea on the one page that cannot afford one. The drawing writes its
    fret number in near-white on the orange, which measures 3.46:1 -- and
    this is not a label in a corner, it is the number the player is looking at at
    the moment the player plays it.
    """

    @pytest.mark.parametrize("name,look", LOOKS, ids=LOOK_IDS)
    def test_the_marked_note_carries_its_number(self, name, look):
        import pygame

        from pickhero.ui import design as d

        ink = look.popover if d.is_light(look.stage_bg) \
            else d.on_accent(look.hit_zone)
        found = ratio(ink, look.hit_zone)
        assert found >= BODY, f"{name}: the fret number reads {found:.2f}:1"

    @pytest.mark.parametrize("name,look", LOOKS, ids=LOOK_IDS)
    def test_the_mark_is_visible_on_the_stage(self, name, look):
        """It is also the playhead line, which has to be findable at a
        glance on a page whose whole discipline is restraint."""
        assert ratio(look.hit_zone, look.stage_bg) >= COMPONENT

    @pytest.mark.parametrize("name,look", LOOKS, ids=LOOK_IDS)
    def test_the_mark_is_not_a_verdict(self, name, look):
        """"This one, now" must not read as "you played that nearly right".

        The floor is 20 rather than the 35 the verdicts hold against each
        other, and the reason is structural: a verdict is only ever a RING
        and the marker is only ever a FILL, which is the house rule that
        keeps the string's colour on the card. Two marks that are never the
        same shape need less colour distance than two that are.

        Hi-fi is the tight one at 24.3 against its amber, and it was measured
        before it was accepted: the only orange that reaches 30 while still
        carrying a white numeral at 4.5 is dE 24 away from the drawing's own
        orange, which trades a confusion nobody can make for one the player would
        see every time the player opened the app.
        """
        for verdict, label in ((look.feedback_hit, "Treffer"),
                               (look.feedback_close, "knapp"),
                               (look.feedback_miss, "Fehler")):
            found = design.delta_e(look.hit_zone, verdict)
            assert found >= 20.0, f"{name}: der Marker gegen {label}: dE {found:.1f}"

    def test_a_verdict_is_a_ring_and_the_marker_is_a_fill(self):
        """Which is what lets the two sit 24 units apart. If the marker ever
        became a ring, or a verdict ever became a fill, the number above
        would have to go back up to 35."""
        import inspect

        from pickhero.ui import scrolling

        source = inspect.getsource(scrolling.PlayingScreen._draw_notes)
        assert "base = look.hit_zone" in source, "the marker stopped being a fill"
        assert "ring = {MatchType.HIT:" in source, "a verdict stopped being a ring"
        assert "fill = look.feedback_" not in source
