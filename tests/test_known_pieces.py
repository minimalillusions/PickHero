"""Known pieces must be the pieces, note for note, and playable in first position.

The oracle for each melody is its published source, copied here in the
source's own key and spelling: Pachelbel's Canon from the LilyPond score in
Wikipedia's article (D major: the cello's ground and the first violin's
entry), When the Saints from the ABC score in the
German article (C major), the Mountain King theme from the first score
excerpt in Wikipedia's article (B minor). The test transposes the source, not
the other way round, so a wrong note in music.py cannot be "fixed" by quietly
editing the oracle to match.

A famous tune with one wrong note is worse than an original piece: it is the
one kind of mistake a beginner will hear at once and blame on themselves.
"""

import pytest

from pickhero.i18n import t
from pickhero.instruments import BASS_GUITAR, BASS_UKULELE, GUITAR
from pickhero.music import PIECES, VERSIONS, build_music, music_song

# Named here, not derived from Piece.known: a list built from the flag would
# quietly shrink if the flag were lost, and every test below would pass by
# testing less.
KNOWN_SLUGS = ("canon", "saints", "boogie", "mountain_king")
KNOWN = [p for p in PIECES if p.slug in KNOWN_SLUGS]
PITCH = {"C": 0, "C#": 1, "D": 2, "D#": 3, "E": 4, "E#": 5, "F": 5, "F#": 6,
         "G": 7, "G#": 8, "A": 9, "A#": 10, "B": 11}


def piece(slug):
    return next(p for p in PIECES if p.slug == slug)


def struck(slug, version, instrument=BASS_UKULELE):
    """(beat from the start, midi pitch) of every note actually played."""
    exercise = build_music(piece(slug), version, instrument)
    out = []
    for index, bar in enumerate(exercise.bars):
        beat = 0.0
        for step in bar:
            if step.string is not None:
                out.append((index * 4 + beat, instrument.tuning[step.string] + step.fret))
            beat += 4 / step.value
    return out


def classes(names, shift):
    return [(PITCH[name] + shift) % 12 for name in names.split()]


# The Canon as the score writes it: the cello's two-bar ground, and the line
# the first violin enters with. The cello's own steps, note to note, are its
# shape: every leap a fourth.
CANON_GROUND_IN_D = "D A B F#  G D G A"
CANON_VIOLIN_IN_D = "F# E D C#  B A B C#  D C# B A  G F# G E"
CANON_GROUND_STEPS = (-5, 2, -5, 1, -5, 5, 2)
SAINTS_IN_C = ("C E F G  C E F G  C E F G E C E D  "
               "E E D C  E G G F  E F G E C D C")
MOUNTAIN_KING_IN_B_MINOR = ("B C# D E F# D F#  E# C# E# E C E  "
                            "B C# D E F# D F# B  A F# D F# A")


def test_the_canon_is_pachelbels_ground_and_first_violin():
    """Source in D major; here in F, a minor third up, ending home on F."""
    ground = classes(CANON_GROUND_IN_D, 3)
    violin = classes(CANON_VIOLIN_IN_D, 3)
    home = classes("D", 3)
    assert [p % 12 for _, p in struck("canon", "start")] == ground * 2 + home
    assert [p % 12 for _, p in struck("canon", "groove")] == ground * 2 + violin + ground + home


def test_the_canon_ground_keeps_the_cellos_shape_on_every_instrument():
    """Every leap a fourth, as the cello plays it: no note in another octave.

    In D or C one note of the ground would have to jump an octave to fit the
    first three frets above the lowest string. A bass line is recognised by
    its shape, so the shape is what this checks.
    """
    for instrument in (BASS_UKULELE, GUITAR, BASS_GUITAR):
        for version in VERSIONS:
            pitches = [p for _, p in struck("canon", version, instrument)][:8]
            steps = tuple(b - a for a, b in zip(pitches, pitches[1:]))
            assert steps == CANON_GROUND_STEPS, (instrument.key, version)


def test_the_canon_start_is_its_ground_at_half_speed():
    start = struck("canon", "start")
    groove = struck("canon", "groove")
    assert [(2 * beat, p) for beat, p in groove[:16]] == start[:16]


def test_when_the_saints_has_all_three_rising_calls():
    """Three times C-E-F-G before the answer.

    The first summary of the source dropped two bars and with them one call.
    Counting against the words caught it: "Oh when the saints go marching in"
    twice is sixteen syllables, and what came back had twelve notes.
    """
    once = classes(SAINTS_IN_C, 0)
    assert [p % 12 for _, p in struck("saints", "start")] == once
    assert [p % 12 for _, p in struck("saints", "groove")] == once + once


def test_the_mountain_king_theme_is_griegs():
    """Source in B minor; here in E minor, a fourth up."""
    once = classes(MOUNTAIN_KING_IN_B_MINOR, 5)
    assert [p % 12 for _, p in struck("mountain_king", "start")] == once
    assert [p % 12 for _, p in struck("mountain_king", "groove")] == once + once


def test_the_mountain_king_starts_on_the_lowest_open_string():
    for instrument in (BASS_UKULELE, GUITAR, BASS_GUITAR):
        first = struck("mountain_king", "start", instrument)[0][1]
        assert first == min(instrument.tuning.values()), instrument.key


def test_the_boogie_rocks_root_fifth_sixth_fifth():
    roots = {"I": 9, "IV": 2, "V": 4}          # A, D, E
    form = ["I", "I", "I", "I", "IV", "IV", "I", "I", "V", "IV", "I"]
    notes = struck("boogie", "groove")
    for bar, chord in enumerate(form):
        figure = [p % 12 for beat, p in notes if bar * 4 <= beat < bar * 4 + 4]
        root = roots[chord]
        assert figure == [root, (root + 7) % 12, (root + 9) % 12, (root + 7) % 12], (bar, chord)
    assert notes[-1][1] % 12 == 9, "a blues in A ends on A"


@pytest.mark.parametrize("slug", ["saints", "mountain_king"])
def test_start_is_the_same_tune_at_half_speed(slug):
    """Doubled values, so every strike sits at twice the beat of its groove twin."""
    start = struck(slug, "start")
    groove = struck(slug, "groove")[:len(start)]
    assert [(2 * beat, p) for beat, p in groove] == start


def test_a_dot_is_a_rest_that_keeps_the_strike_where_the_score_puts_it():
    """A dotted quarter written as a quarter plus an eighth rest strikes at
    exactly the same moment, and that moment is what gets scored.

    When the Saints, bar 6 of the groove: G F-dot, so nothing on beat 3.5.
    """
    bar_six = [beat - 20 for beat, _ in notes_in("saints", "groove", 20, 24)]
    assert bar_six == [0.0, 1.0, 2.0, 2.5]


def notes_in(slug, version, start, end):
    return [(b, p) for b, p in struck(slug, version) if start <= b < end]


@pytest.mark.parametrize("p", KNOWN, ids=lambda p: p.slug)
def test_start_stays_in_first_position_on_her_instrument(p):
    frets = [step.fret for bar in build_music(p, "start", BASS_UKULELE).bars
             for step in bar if step.string is not None]
    assert max(frets) <= 3


@pytest.mark.parametrize("p", KNOWN, ids=lambda p: p.slug)
def test_groove_reaches_the_fourth_fret_at_most_once(p):
    frets = [step.fret for bar in build_music(p, "groove", BASS_UKULELE).bars
             for step in bar if step.string is not None]
    assert max(frets) <= 4 and frets.count(4) <= 1


@pytest.mark.parametrize("p", KNOWN, ids=lambda p: p.slug)
@pytest.mark.parametrize("instrument", [BASS_UKULELE, GUITAR, BASS_GUITAR],
                         ids=lambda i: i.key)
def test_every_known_piece_builds_for_every_instrument(p, instrument):
    for version in VERSIONS:
        exercise = build_music(p, version, instrument)
        assert all(sum(4 / s.value for s in bar) == 4 for bar in exercise.bars)


@pytest.mark.parametrize("p", KNOWN, ids=lambda p: p.slug)
def test_a_known_piece_names_its_composer(p):
    song = music_song(p, "start", BASS_UKULELE)
    assert song.artist == t(f"music.{p.slug}.composer")
    assert song.artist != t("music.artist")
    assert song.album == t("music.album_known")


def test_exactly_these_pieces_are_marked_as_somebody_elses():
    assert {p.slug for p in PIECES if p.known} == set(KNOWN_SLUGS)
    assert len(KNOWN) == len(KNOWN_SLUGS)


def test_an_original_still_calls_itself_a_practice_piece():
    original = next(p for p in PIECES if not p.known)
    assert music_song(original, "start", BASS_UKULELE).artist == t("music.artist")


@pytest.mark.parametrize("language", ["de", "en"])
@pytest.mark.parametrize("p", KNOWN, ids=lambda p: p.slug)
def test_every_known_piece_has_its_text(p, language, monkeypatch):
    from pickhero import i18n
    monkeypatch.setattr(i18n, "_current", language)
    for key in ("title", "composer", "start", "groove"):
        text = t(f"music.{p.slug}.{key}")
        assert text and not text.startswith("music."), (p.slug, key)
    assert not t("music.album_known").startswith("music.")
