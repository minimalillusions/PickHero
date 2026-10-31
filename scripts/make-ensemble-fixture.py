"""Write tests/fixtures/ensemble.gp5: a nine-track file for loader tests.

It replaces canon.gp5, a transcription of a copyrighted arrangement that came
with the original project. The tests need what that file had -- several
guitars told apart from string pads by their MIDI programme, a drop-D track,
a four-string bass in E-A-D-G, two percussion tracks, tempo changes, and
enough backing to be worth playing along to -- not its music.

The music here is Pachelbel's progression (1680, public domain) with plain
patterns written for this file. Run it again and the file comes out the same.

Usage: python scripts/make-ensemble-fixture.py
"""

from __future__ import annotations

import pathlib

import guitarpro as gp
from guitarpro import models as m

ROOT = pathlib.Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "tests" / "fixtures" / "ensemble.gp5"

TICKS = 960                       # per quarter
FIRST_TICK = 960                  # pyguitarpro starts the first bar here
BARS = 64
START_BPM = 90
# bar (1-based) -> new tempo. Faster in the middle, so the timestamps come out
# well short of what one constant 90 BPM would give.
TEMPO_CHANGES = {17: 120, 33: 140, 41: 100, 49: 140, 57: 90}

STANDARD = {1: 64, 2: 59, 3: 55, 4: 50, 5: 45, 6: 40}
DROP_D = {1: 64, 2: 59, 3: 55, 4: 50, 5: 45, 6: 38}
BASS = {1: 43, 2: 38, 3: 33, 4: 28}

# D A Bm F#m G D G A, two chords to a bar: (root, third, fifth) as MIDI.
CHORDS = [(50, 54, 57), (45, 49, 52), (47, 50, 54), (42, 45, 49),
          (43, 47, 50), (50, 54, 57), (43, 47, 50), (45, 49, 52)]

# (name, tuning, programme, percussion)
TRACKS = [
    ("Lead Guitar", STANDARD, 30, False),
    ("String Pad", DROP_D, 49, False),
    ("Synth Pad", STANDARD, 51, False),
    ("Rhythm Guitar", STANDARD, 29, False),
    ("Drums", STANDARD, 0, True),
    ("Harmony Guitar", STANDARD, 30, False),
    ("Clean Guitar", STANDARD, 27, False),
    ("Bass", BASS, 33, False),
    ("Percussion", STANDARD, 0, True),
]


def place(pitches: list[int], tuning: dict[int, int], max_fret: int = 24) -> list[tuple[int, int]]:
    """Each pitch on its own string, as (string, fret), frets kept low.

    A beat holds at most one note per string: the file stores the strings of
    a beat as a bit mask, so two notes on one string corrupt every byte after
    them -- exactly what the first version of this script wrote. Taking each
    note's lowest fret in turn is not enough either: in drop D, F#m runs out
    of strings. So every assignment is tried and the lowest frets win.
    """
    from itertools import permutations
    best = None
    for strings in permutations(sorted(tuning), len(pitches)):
        frets = [pitch - tuning[string] for pitch, string in zip(pitches, strings)]
        if all(0 <= fret <= max_fret for fret in frets):
            cost = (max(frets), sum(frets))
            if best is None or cost < best[0]:
                best = (cost, list(zip(strings, frets)))
    if best is None:
        raise ValueError(f"{pitches} cannot be fretted on {tuning}")
    return best[1]


def chords_in_bar(bar: int):
    first = (2 * bar) % len(CHORDS)
    return CHORDS[first], CHORDS[(first + 1) % len(CHORDS)]


def pattern(name: str, bar: int):
    """(duration value, [pitches]) for each beat of one bar; [] is a rest."""
    a, b = chords_in_bar(bar)
    if name == "Lead Guitar":                 # eighth-note arpeggio, an octave up
        up = lambda c: [c[0] + 12, c[1] + 12, c[2] + 12, c[0] + 24]
        return [(8, [p]) for p in up(a) + up(b)]
    if name in ("String Pad", "Synth Pad"):   # one held chord per half bar
        shift = 24 if name == "Synth Pad" else 12      # pads sit above the bass
        return [(2, [p + shift for p in a]), (2, [p + shift for p in b])]
    if name == "Rhythm Guitar":               # root and fifth on every beat
        return [(4, [c[0], c[2]]) for c in (a, a, b, b)]
    if name == "Harmony Guitar":              # thirds, half notes
        return [(2, [a[1] + 12]), (2, [b[1] + 12])]
    if name == "Clean Guitar":                # quarter arpeggio
        return [(4, [a[0] + 12]), (4, [a[2] + 12]), (4, [b[0] + 12]), (4, [b[2] + 12])]
    if name == "Bass":                        # the ground: one root per half bar
        return [(2, [a[0] - 12]), (2, [b[0] - 12])]
    if name == "Drums":                       # kick, snare, hats in eighths
        return [(8, [36, 42] if i in (0, 4) else [38, 42] if i in (2, 6) else [42])
                for i in range(8)]
    if name == "Percussion":                  # tambourine on two and four
        return [(4, []), (4, [54]), (4, []), (4, [54])]
    raise ValueError(name)


def build() -> m.Song:
    song = m.Song()
    song.title = "Ensemble (test file)"
    song.artist = "Johann Pachelbel (progression), arranged for tests"
    song.tempo = START_BPM
    song.tracks, song.measureHeaders = [], []
    for index in range(BARS):
        header = m.MeasureHeader(number=index + 1)
        header.start = FIRST_TICK + index * 4 * TICKS
        header.timeSignature = m.TimeSignature(numerator=4, denominator=m.Duration(value=4))
        song.measureHeaders.append(header)

    for number, (name, tuning, programme, percussion) in enumerate(TRACKS, 1):
        track = m.Track(song, number)
        track.name = name
        track.isPercussionTrack = percussion
        track.strings = [m.GuitarString(number=s, value=0 if percussion else tuning[s])
                         for s in sorted(tuning)]
        channel = 9 if percussion else (number - 1 if number - 1 < 9 else number)
        track.channel = m.MidiChannel(channel=channel, effectChannel=channel, instrument=programme)
        track.measures = []
        for bar, header in enumerate(song.measureHeaders):
            measure = m.Measure(track, header)
            voice = m.Voice(measure)
            voice.beats = []
            tick = header.start
            for position, (value, pitches) in enumerate(pattern(name, bar)):
                beat = m.Beat(voice)
                beat.start = tick
                beat.duration = m.Duration(value=value)
                beat.status = m.BeatStatus.normal if pitches else m.BeatStatus.rest
                beat.notes = []
                spots = ([(slot, pitch) for slot, pitch in enumerate(pitches, 1)] if percussion
                         else place(pitches, tuning))
                for string, fret in spots:
                    note = m.Note(beat)
                    note.string, note.value = string, fret
                    note.velocity = 95
                    note.type = m.NoteType.normal
                    beat.notes.append(note)
                if number == 1 and position == 0 and (bar + 1) in TEMPO_CHANGES:
                    change = m.MixTableChange()
                    change.tempo = m.MixTableItem(value=TEMPO_CHANGES[bar + 1], duration=0)
                    beat.effect.mixTableChange = change
                voice.beats.append(beat)
                tick += 4 * TICKS // value
            measure.voices = [voice, m.Voice(measure)]
            track.measures.append(measure)
        song.tracks.append(track)
    return song


def main() -> None:
    gp.write(build(), str(OUTPUT), version=(5, 1, 0))
    print("geschrieben:", OUTPUT)


if __name__ == "__main__":
    main()
