"""Instrument profiles.

An Instrument bundles everything that differs between a guitar and a bass
ukulele: string count, tuning, fret count, and the audio settings that follow
from the instrument's frequency range.

String numbering follows pyguitarpro: string 1 is the highest-pitched string.

The names are shown to the player, so they live in the translation catalogue
rather than in this file. A profile carries the key, not the text: name and
short_name read it on access, which is why switching language changes the
header pill without rebuilding the profiles.

This module must stay free of pygame -- the detector imports it -- which is
why i18n lives at pickhero/i18n.py and not under ui/.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import math

from pickhero.audio.note_utils import midi_to_freq
from pickhero.i18n import t


@dataclass(frozen=True, eq=False)
class Instrument:
    """A stringed instrument the app can follow."""

    key: str
    # Catalogue keys, not text. See the module docstring.
    name_key: str
    short_name_key: str
    tuning: dict[int, int]        # string number -> MIDI note of the open string
    max_fret: int
    pitch_method: str = "yin"     # "yin" (aubio) or "harmonic" (in-repo, numpy only)
    buf_size: int = 2048
    hop_size: int = 512
    sample_rate: int = 44100
    confidence_threshold: float = 0.8
    yin_tolerance: float = 0.15   # aubio's own default; only used by pitch_method="yin"
    onset_method: str = "default"
    onset_threshold: float = 0.3
    # The onset window is separate from the pitch window on purpose. Pitch at
    # 41 Hz needs a long window; a long window smears the attack and costs
    # timing accuracy, so the strike detector gets a short one.
    onset_buf_size: int = 1024
    # What the onset detector itself lags by, on top of the analysis window
    # that is already compensated per detection. Measured, not derived.
    onset_latency_ms: float = 0.0
    # The shortest gap between two reports of the same note. A single pluck
    # fires the onset detector twice, so without a floor here one note played
    # is announced twice. Set from the instrument's own decay, not guessed:
    # too long and genuine repeated notes merge.
    min_note_gap_ms: float = 90.0
    # A strike must be accompanied by the signal actually getting louder.
    # An onset detector fires on spectral change, and a decaying string keeps
    # changing spectrally long after it was plucked, so it fires again and
    # again inside one note. A real pluck puts energy into the string; a
    # phantom is the same energy running out. `strike_rise_db` is how much
    # louder the signal must get, and `strike_rise_window_ms` is how far back
    # the quiet reference is taken from. Zero disables the whole mechanism,
    # which is what the guitar profile does: this is measured on a bass
    # ukulele and nowhere else.
    strike_rise_db: float = 0.0
    strike_rise_window_ms: float = 160.0
    # Optional short attack evidence, accepted only with two independent
    # periodicity confirmations of the voted pitch. Measured for steel bass.
    transient_rise_db: float = 0.0
    # How far from the written time a note may be played and still count.
    # This is real timing tolerance for the player, not compensation for the
    # pipeline: the detector's own delay is handled separately by
    # NoteMatcher's retire_delay_ms.
    timing_window_ms: float = 100.0
    noise_gate_db: float = -60.0
    harmonics: int = 8            # only used by the harmonic detector

    @property
    def string_count(self) -> int:
        return len(self.tuning)

    @property
    def name(self) -> str:
        """The full name, e.g. "Bass-Ukulele (4 Saiten, E-A-D-G)"."""
        return t(self.name_key, count=self.string_count)

    @property
    def short_name(self) -> str:
        """Just the instrument, for places with no room for the tuning.

        The song browser used to cut the full name at " (", which only worked
        because every name happened to have a bracket in it. A translator has
        no reason to keep that bracket, so the short form is its own key.
        """
        return t(self.short_name_key, count=self.string_count)

    @property
    def midi_min(self) -> int:
        """MIDI note of the lowest open string."""
        return min(self.tuning.values())

    @property
    def midi_max(self) -> int:
        """MIDI note of the highest string at the highest fret."""
        return max(self.tuning.values()) + self.max_fret

    @property
    def freq_min(self) -> float:
        """Lowest frequency worth searching for, one semitone below the low string."""
        return midi_to_freq(self.midi_min - 1)

    @property
    def freq_max(self) -> float:
        """Highest frequency worth searching for, one semitone above the top note."""
        return midi_to_freq(self.midi_max + 1)

    def strings_low_to_high(self) -> list[int]:
        """String numbers ordered thickest (lowest) first, e.g. [6, 5, ..., 1]."""
        return sorted(self.tuning, key=lambda s: self.tuning[s])

    def describe(self) -> str:
        """One line naming the instrument, its strings and its range.

        The note names are deliberately not translated: German writes B as H,
        but the tabs, the tuner and every Guitar Pro file the player learns from use
        the scientific names, and the app has to agree with their material.
        """
        from pickhero.audio.note_utils import midi_to_name
        notes = " ".join(midi_to_name(self.tuning[s]) for s in self.strings_low_to_high())
        return t("instrument.describe", name=self.short_name,
                 count=self.string_count, notes=notes, frets=self.max_fret)


# Standard 6-string guitar, E2-A2-D3-G3-B3-E4. These values reproduce the
# app's original hardcoded behaviour exactly, so the guitar path is unchanged.
GUITAR = Instrument(
    key="guitar",
    name_key="instrument.guitar.name",
    short_name_key="instrument.guitar.short",
    tuning={1: 64, 2: 59, 3: 55, 4: 50, 5: 45, 6: 40},
    max_fret=24,
    pitch_method="yin",
    buf_size=2048,
    confidence_threshold=0.8,
    onset_method="default",
    onset_threshold=0.3,
    noise_gate_db=-60.0,
)

# Bass ukulele with rubber/polyurethane strings, tuned exactly like a 4-string
# bass guitar: E1-A1-D2-G2 = MIDI 28/33/38/43 = 41.2/55.0/73.4/98.0 Hz.
#
# The settings below are not guesses. They were measured against a real
# recording of the instrument through a condenser microphone:
#   - buf_size 2048 cannot represent E1 at all (aubio's YIN searches lags up to
#     buf_size/2, so its floor is 44100/1024 = 43.07 Hz, above the 41.20 Hz
#     string). Measured detection rate on the low E at 2048: 0 percent.
#   - The instrument's strongest partial is the 3rd to 5th harmonic, not the
#     fundamental, so plain YIN locks onto a harmonic. Measured note accuracy:
#     aubio YIN 60 percent, harmonic sum 87 percent, at a twelfth of the CPU.
BASS_UKULELE = Instrument(
    key="bass_ukulele",
    name_key="instrument.bass_ukulele.name",
    short_name_key="instrument.bass_ukulele.short",
    tuning={1: 43, 2: 38, 3: 33, 4: 28},
    max_fret=16,
    pitch_method="harmonic",
    buf_size=4096,
    hop_size=512,
    # Measured on a real recording, per frame above the noise gate: at 0.15
    # the open G reads an octave low in a quarter of its frames, because the
    # string's inharmonic partials make the subharmonic hypothesis look
    # plausible. Those frames all score below 0.15 confidence while the
    # correct ones sit near 0.43, so the threshold separates them cleanly.
    # Raising it used to cost whole strikes; it no longer does, because a
    # strike now reports the frames it did collect instead of discarding them.
    confidence_threshold=0.25,
    # Measured against a real recording of the instrument, 12 open-string
    # notes end to end through the matcher: specflux 12/12, energy 11/12,
    # hfc 11/12. The shipped "default" (high frequency content) is close to
    # blind to a rubber string's soft attack.
    onset_method="specflux",
    onset_threshold=0.15,
    onset_buf_size=1024,
    # Measured on a real recording against known note times: after the
    # per-detection window correction, strikes still land a median 38 ms late
    # with a spread of roughly +-100 ms.
    onset_latency_ms=38.0,
    # Rubber strings ring long and the second onset lands about 120 ms after
    # the first. 160 ms is the shortest floor that removes every phantom on
    # the reference recording; it merges identical sixteenth notes above
    # roughly 115 BPM, which is far beyond anything this instrument is for.
    min_note_gap_ms=160.0,
    # Measured end to end on a 70 s recording of this instrument, 35 plucks,
    # sweeping one decibel at a time. Every value from 13 to 25 dB gives the
    # identical, perfect result there: 35 strikes for 35 plucks, no phantoms,
    # none missed. Below 13 phantoms return one at a time (12 dB: 1, 10 dB: 4,
    # 9 dB: 5). At 26 dB real plucks start being lost (26 dB: 2, 28 dB: 5).
    #
    # 18 was chosen first, as the middle of that plateau pushed one step
    # toward the phantom side. It was wrong, and the reason is worth keeping:
    # that recording contains no note played while another string rings. It
    # is single plucks, then eighths on one string, then a run on one string.
    #
    # On the alternating-strings exercise, which is the whole point of the
    # exercise, the picture changes completely. The open A is still at
    # -28.3 dB two seconds after it is plucked, and the open E -- the
    # quietest string on the instrument -- attacks at only -13.2 dB. The rise
    # actually available is 15.1 dB. At 18 the gate discarded EVERY open E
    # that followed an open A, deterministically, 2.9 dB short every time: a
    # note-perfect run scored 9 of 16, and all seven misses were the E.
    # At 14 the same run scores 16 of 16, and the reference recording is
    # still 35 for 35. The four decibels of headroom bought nothing and cost
    # seven notes.
    #
    # The structural fix is to take the rise per frequency band rather than
    # broadband, so a ringing neighbour cannot raise the bar for a quiet
    # string. Until then this number carries that whole argument.
    strike_rise_db=14.0,
    # Same sweep: 116 to 209 ms is flat and perfect. Shorter and the first
    # pluck of the chromatic run (60.802 s, struck while the previous note
    # was still audible) loses its quiet reference and is missed; longer and
    # the reference reaches back past the previous note's own attack, so a
    # phantom in its decay clears the bar (221 ms: 1, 250 ms: 3, 300 ms: 6).
    # 160 is the middle.
    strike_rise_window_ms=160.0,
    # Deliberately off, and the number is here to record why. Measured alone
    # on the same recording, the best it can do without losing a real pluck
    # is 200 ms, which removes 5 phantoms of 27; pushed to 300 ms it removes
    # 10 but loses a real note. Measured ON TOP of the rise gate it removes
    # nothing whatsoever -- the gate has already taken every phantom -- and
    # at 450 ms it starts eating notes off the test fixture. It is pure cost.
    timing_window_ms=150.0,
    noise_gate_db=-45.0,
    harmonics=8,
)

BASS_GUITAR = Instrument(
    key="bass_guitar",
    name_key="instrument.bass_guitar.name",
    short_name_key="instrument.bass_guitar.short",
    tuning={1: 43, 2: 38, 3: 33, 4: 28},
    max_fret=24,
    pitch_method="harmonic",
    buf_size=4096,
    hop_size=512,
    confidence_threshold=0.25,
    onset_method="specflux",
    onset_threshold=0.15,
    onset_buf_size=1024,
    onset_latency_ms=38.0,
    min_note_gap_ms=140.0,
    # Keep the long-window rejection of decaying notes. A weaker but abrupt
    # attack may additionally qualify with independent pitch corroboration;
    # see the original-recording development/validation expansion report.
    strike_rise_db=14.0,
    strike_rise_window_ms=160.0,
    transient_rise_db=9.0,
    timing_window_ms=150.0,
    noise_gate_db=-45.0,
)

PROFILES: dict[str, Instrument] = {
    i.key: i for i in (GUITAR, BASS_UKULELE, BASS_GUITAR)
}

DEFAULT_KEY = GUITAR.key


def get(key: str | None) -> Instrument:
    """Look up a profile by key, falling back to guitar for unknown keys."""
    if not key:
        return PROFILES[DEFAULT_KEY]
    return PROFILES.get(key, PROFILES[DEFAULT_KEY])


def with_tuning(instrument: Instrument, tuning: dict[int, int]) -> Instrument:
    """Adapt a selected instrument to a track without replacing its settings.

    Lower strings need a longer pitch window to retain the same number of
    periods as the measured profile. The onset window and its latency remain
    unchanged; this affects the session, never the stored instrument settings.
    """
    if not tuning or tuning == instrument.tuning:
        return instrument
    ratio = max(1.0, midi_to_freq(instrument.midi_min) / midi_to_freq(min(tuning.values())))
    # Keep complete hops: the onset-voting horizon is expressed in hops,
    # and rounding to a partial hop would silently truncate that extra time.
    window = int(math.ceil(instrument.buf_size * ratio / instrument.hop_size)) * instrument.hop_size
    return replace(instrument, tuning=dict(tuning), buf_size=window)


def from_tuning(tuning: dict[int, int], max_fret: int = 24) -> Instrument:
    """Build an ad-hoc profile from a tab's own tuning.

    Used when a loaded Guitar Pro track has a tuning that matches none of the
    built-in profiles. Picks the harmonic detector for anything whose lowest
    string is below the guitar's low E, since that is where YIN starts failing.
    """
    if not tuning:
        return PROFILES[DEFAULT_KEY]
    low = min(tuning.values())
    for prof in PROFILES.values():
        if prof.tuning == tuning:
            return prof
    deep = low < GUITAR.midi_min
    return Instrument(
        key="custom",
        name_key="instrument.custom.name",
        short_name_key="instrument.custom.short",
        tuning=dict(tuning),
        max_fret=max_fret,
        pitch_method="harmonic" if deep else "yin",
        buf_size=4096 if deep else 2048,
        confidence_threshold=0.15 if deep else 0.8,
        onset_method="specflux" if deep else "default",
        onset_threshold=0.15 if deep else 0.3,
        onset_buf_size=1024,
        # Anything deep enough to need the harmonic detector also rings long
        # enough for the onset detector to re-fire inside one note, so it gets
        # the same strike gate as the bass ukulele. A guitar-range custom
        # tuning does not: the number was never measured there.
        strike_rise_db=14.0 if deep else 0.0,
        strike_rise_window_ms=160.0,
        noise_gate_db=-45.0 if deep else -60.0,
    )
