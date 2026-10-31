"""Tempo changes and capos: two silent ways a tab plays back wrong."""

import guitarpro
import pytest

from pickhero.tabs.loader import (
    _GP7_NOTE_VALUES,
    _build_tempo_map,
    _capo,
    _extract_notes,
    _extract_tuning,
    _gp7_capo,
)


def _song(tracks: int = 2, tempo: int = 120) -> guitarpro.Song:
    m = guitarpro.models
    song = m.Song()
    song.tempo = tempo
    song.tracks = []
    song.measureHeaders = []
    header = m.MeasureHeader(number=1)
    header.start = 960
    header.timeSignature = m.TimeSignature(
        numerator=4, denominator=m.Duration(value=4))
    song.measureHeaders.append(header)
    for i in range(tracks):
        track = m.Track(song, i + 1)
        track.strings = [m.GuitarString(number=n, value=v) for n, v in
                         enumerate((64, 59, 55, 50, 45, 40), start=1)]
        measure = m.Measure(track, header)
        voice = m.Voice(measure)
        voice.beats = []
        measure.voices = [voice] + list(measure.voices[1:])
        track.measures = [measure]
        song.tracks.append(track)
    return song


def _put_tempo_change(track, tick: int, bpm: int) -> None:
    m = guitarpro.models
    voice = track.measures[0].voices[0]
    beat = m.Beat(voice)
    beat.start = tick
    beat.effect.mixTableChange = m.MixTableChange()
    beat.effect.mixTableChange.tempo = m.MixTableItem(value=bpm)
    voice.beats.append(beat)


class TestTempoChanges:
    def test_a_change_on_the_first_track_is_read(self):
        song = _song()
        _put_tempo_change(song.tracks[0], 1920, 60)
        assert _build_tempo_map(song).tempo_at_tick(1920) == 60

    def test_a_change_on_a_later_track_is_read_too(self):
        """Guitar Pro writes the change on whichever track was selected. Only
        looking at track 0 played the whole piece at its opening tempo."""
        song = _song()
        _put_tempo_change(song.tracks[1], 1920, 60)
        assert _build_tempo_map(song).tempo_at_tick(1920) == 60

    def test_the_same_change_on_two_tracks_is_counted_once(self):
        song = _song()
        _put_tempo_change(song.tracks[0], 1920, 60)
        _put_tempo_change(song.tracks[1], 1920, 60)
        tempo_map = _build_tempo_map(song)
        assert tempo_map.tempo_at_tick(1920) == 60
        # Doubling the entry would double the elapsed time at that tick.
        single = _song()
        _put_tempo_change(single.tracks[0], 1920, 60)
        assert tempo_map.tick_to_ms(3840) == pytest.approx(
            _build_tempo_map(single).tick_to_ms(3840))


class TestCapo:
    def test_no_capo_leaves_pitches_alone(self):
        song = _song(tracks=1)
        song.tracks[0].offset = 0
        assert _capo(song.tracks[0]) == 0
        assert _extract_tuning(song.tracks[0])[1] == song.tracks[0].strings[0].value

    def test_a_capo_raises_the_tuning(self):
        """Fret numbers in the tab are relative to the capo, so what sounds is
        three semitones above what pyguitarpro's realValue reports."""
        song = _song(tracks=1)
        song.tracks[0].offset = 3
        open_string = song.tracks[0].strings[0].value
        assert _extract_tuning(song.tracks[0])[1] == open_string + 3

    def test_a_capo_raises_the_notes(self):
        song = _song(tracks=1)
        track = song.tracks[0]
        track.offset = 2
        voice = track.measures[0].voices[0]
        beat = guitarpro.models.Beat(voice)
        beat.start = 960
        beat.status = guitarpro.models.BeatStatus.normal
        note = guitarpro.models.Note(beat)
        note.string = 1
        note.value = 0
        note.type = guitarpro.models.NoteType.normal
        beat.notes.append(note)
        voice.beats.append(beat)

        without = note.realValue
        extracted = _extract_notes(track, _build_tempo_map(song))
        assert extracted[0].midi_note == without + 2

    def test_a_garbled_capo_value_is_ignored(self):
        song = _song(tracks=1)
        song.tracks[0].offset = None
        assert _capo(song.tracks[0]) == 0


class TestGp7Rhythms:
    @pytest.mark.parametrize("name,quarters", [
        ("Long", 16.0), ("DoubleWhole", 8.0), ("Whole", 4.0),
        ("Quarter", 1.0), ("128th", 0.03125), ("256th", 0.015625),
    ])
    def test_every_note_value_guitar_pro_writes_is_known(self, name, quarters):
        """An unknown value fell back to a quarter note, which moved every
        later beat in the bar to the wrong millisecond."""
        assert _GP7_NOTE_VALUES[name] == quarters


class TestGp7Capo:
    def test_capo_fret_is_read(self):
        import xml.etree.ElementTree as ET
        track = ET.fromstring(
            '<Track><Properties>'
            '<Property name="CapoFret"><Fret>4</Fret></Property>'
            '</Properties></Track>')
        assert _gp7_capo(track) == 4

    def test_no_capo_property_means_no_capo(self):
        import xml.etree.ElementTree as ET
        assert _gp7_capo(ET.fromstring("<Track/>")) == 0
