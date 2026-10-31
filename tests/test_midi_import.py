"""Reading a Standard MIDI File and turning it into something playable.

Every fixture here is real MIDI bytes built by hand, not a stand-in, because
the parser's whole job is coping with what files actually contain: running
status, note-on with velocity zero meaning note-off, tempo changes mid-piece,
tracks that never send a note-off.
"""

import struct

import pytest

from pickhero import instruments
from pickhero.tabs.midi_import import (
    MidiError,
    assign_fingering,
    choose_transpose,
    describe,
    load_midi_file,
    read_midi,
    score_track,
    select_track,
)

BASS = instruments.BASS_UKULELE
GUITAR = instruments.GUITAR


# ── Building MIDI by hand ────────────────────────────────────────────────

def vlq(value: int) -> bytes:
    """A variable-length quantity, the way the format writes delta times."""
    out = bytearray([value & 0x7F])
    value >>= 7
    while value:
        out.insert(0, (value & 0x7F) | 0x80)
        value >>= 7
    return bytes(out)


def track_chunk(events: bytes) -> bytes:
    body = events + vlq(0) + b"\xff\x2f\x00"      # end of track
    return b"MTrk" + struct.pack(">I", len(body)) + body


def midi_file(tracks: list[bytes], ticks_per_quarter: int = 480,
              fmt: int = 1) -> bytes:
    head = b"MThd" + struct.pack(">IHHH", 6, fmt, len(tracks), ticks_per_quarter)
    return head + b"".join(tracks)


def note(pitch: int, start_delta: int, length: int, channel: int = 0,
         velocity: int = 90) -> bytes:
    return (vlq(start_delta) + bytes([0x90 | channel, pitch, velocity])
            + vlq(length) + bytes([0x80 | channel, pitch, 0]))


def tempo_event(bpm: float) -> bytes:
    micros = int(60_000_000 / bpm)
    return vlq(0) + b"\xff\x51\x03" + micros.to_bytes(3, "big")


def track_name(name: str) -> bytes:
    raw = name.encode("latin-1")
    return vlq(0) + b"\xff\x03" + vlq(len(raw)) + raw


def simple_file(tmp_path, pitches, ticks_per_quarter=480, name="Bass",
                bpm=120.0, channel=0):
    events = track_name(name) + tempo_event(bpm)
    for pitch in pitches:
        events += note(pitch, 0, ticks_per_quarter, channel=channel)
    path = tmp_path / "probe.mid"
    path.write_bytes(midi_file([track_chunk(events)], ticks_per_quarter))
    return path


# ── Parsing ──────────────────────────────────────────────────────────────

class TestParsing:
    def test_a_plain_file_is_read(self, tmp_path):
        path = simple_file(tmp_path, [40, 45, 50, 55])
        tracks, tpq, meta = read_midi(path)
        assert tpq == 480
        assert len(tracks) == 1
        assert [n.pitch for n in tracks[0].notes] == [40, 45, 50, 55]
        assert tracks[0].name == "Bass"

    def test_running_status_is_followed(self, tmp_path):
        """A sequencer omits the status byte when it repeats. A parser that
        does not handle it reads garbage from the second note on."""
        events = (vlq(0) + bytes([0x90, 40, 90])
                  + vlq(480) + bytes([40, 0])        # note-off via running status
                  + vlq(0) + bytes([45, 90])         # note-on, still running
                  + vlq(480) + bytes([45, 0]))
        path = tmp_path / "running.mid"
        path.write_bytes(midi_file([track_chunk(events)]))
        tracks, _, _ = read_midi(path)
        assert [n.pitch for n in tracks[0].notes] == [40, 45]

    def test_note_on_with_zero_velocity_ends_the_note(self, tmp_path):
        events = (vlq(0) + bytes([0x90, 40, 90])
                  + vlq(240) + bytes([0x90, 40, 0]))
        path = tmp_path / "zero.mid"
        path.write_bytes(midi_file([track_chunk(events)]))
        tracks, _, _ = read_midi(path)
        assert len(tracks[0].notes) == 1
        assert tracks[0].notes[0].duration == 240

    def test_a_note_never_switched_off_still_appears(self, tmp_path):
        """Common in hand-written files. Dropping it loses a note the player
        can see in every other program."""
        events = vlq(0) + bytes([0x90, 40, 90]) + vlq(960) + bytes([0x90, 45, 90])
        path = tmp_path / "hanging.mid"
        path.write_bytes(midi_file([track_chunk(events)]))
        tracks, _, _ = read_midi(path)
        assert {n.pitch for n in tracks[0].notes} == {40, 45}

    def test_tempo_changes_are_collected(self, tmp_path):
        events = tempo_event(120) + note(40, 0, 480) + tempo_event(60) + note(45, 0, 480)
        path = tmp_path / "tempo.mid"
        path.write_bytes(midi_file([track_chunk(events)]))
        _, _, meta = read_midi(path)
        assert [round(e.value) for e in meta if e.kind == "tempo"] == [120, 60]

    def test_unknown_chunks_are_skipped(self, tmp_path):
        """The spec requires it: a file may carry chunks we have never heard
        of and must still be readable."""
        junk = b"XYZW" + struct.pack(">I", 4) + b"\x00\x01\x02\x03"
        body = track_chunk(track_name("A") + note(40, 0, 480))
        path = tmp_path / "junk.mid"
        path.write_bytes(midi_file([]) [:14] + junk + body)
        tracks, _, _ = read_midi(path)
        assert len(tracks) == 1

    def test_a_file_that_is_not_midi_is_refused(self, tmp_path):
        path = tmp_path / "nope.mid"
        path.write_bytes(b"das ist keine MIDI-Datei")
        with pytest.raises(MidiError):
            read_midi(path)

    def test_a_truncated_file_is_refused(self, tmp_path):
        good = simple_file(tmp_path, [40, 45]).read_bytes()
        path = tmp_path / "kurz.mid"
        path.write_bytes(good[:len(good) // 2])
        with pytest.raises(MidiError):
            read_midi(path)

    def test_smpte_timing_is_refused_clearly(self, tmp_path):
        path = tmp_path / "smpte.mid"
        path.write_bytes(b"MThd" + struct.pack(">IHHH", 6, 1, 1, 0xE728)
                         + track_chunk(note(40, 0, 100)))
        with pytest.raises(MidiError, match="SMPTE"):
            read_midi(path)


# ── Choosing the part ────────────────────────────────────────────────────

class TestTrackChoice:
    def test_percussion_is_never_chosen(self, tmp_path):
        drums = track_name("Drums") + b"".join(
            note(38, 0, 120, channel=9) for _ in range(40))
        bass = track_name("Bass") + b"".join(
            note(40 + i % 5, 0, 480) for i in range(20))
        path = tmp_path / "band.mid"
        path.write_bytes(midi_file([track_chunk(drums), track_chunk(bass)]))
        tracks, _, _ = read_midi(path)
        assert select_track(tracks, BASS).name == "Bass"

    def test_the_low_part_is_chosen_for_a_bass_ukulele(self, tmp_path):
        high = track_name("Melodie") + b"".join(note(72 + i % 7, 0, 240)
                                                for i in range(24))
        low = track_name("Bass") + b"".join(note(33 + i % 5, 0, 480)
                                            for i in range(24))
        path = tmp_path / "zwei.mid"
        path.write_bytes(midi_file([track_chunk(high), track_chunk(low)]))
        tracks, _, _ = read_midi(path)
        assert select_track(tracks, BASS).name == "Bass"

    def test_a_chord_part_loses_to_a_single_line(self, tmp_path):
        chords = track_name("Akkorde")
        for _ in range(16):
            chords += (vlq(0) + bytes([0x90, 40, 90]) + vlq(0) + bytes([0x90, 44, 90])
                       + vlq(0) + bytes([0x90, 47, 90]) + vlq(480)
                       + bytes([0x80, 40, 0]) + vlq(0) + bytes([0x80, 44, 0])
                       + vlq(0) + bytes([0x80, 47, 0]))
        line = track_name("Linie") + b"".join(note(40 + i % 6, 0, 480)
                                              for i in range(16))
        path = tmp_path / "akkorde.mid"
        path.write_bytes(midi_file([track_chunk(chords), track_chunk(line)]))
        tracks, _, _ = read_midi(path)
        assert select_track(tracks, BASS).name == "Linie"

    def test_an_empty_set_of_tracks_chooses_nothing(self):
        assert select_track([], BASS) is None


class TestTranspose:
    def test_a_part_already_in_range_is_not_moved(self):
        assert choose_transpose([28, 33, 38, 43], BASS) == 0

    def test_a_part_above_the_range_comes_down(self):
        """The smallest move that fits, not the biggest. Two octaves down also
        fits here, and moving further than needed puts the part lower than the
        arranger wrote it for no gain."""
        shift = choose_transpose([52, 57, 62, 67], BASS)
        assert shift == -12
        assert all(BASS.midi_min <= p + shift <= BASS.midi_max
                   for p in (52, 57, 62, 67))

    def test_a_part_far_above_the_range_comes_down_further(self):
        shift = choose_transpose([76, 81, 86], BASS)
        assert all(BASS.midi_min <= p + shift <= BASS.midi_max
                   for p in (76, 81, 86))

    def test_only_whole_octaves_are_used(self):
        """Anything else changes the key, and the player may be playing along to a
        recording."""
        for pitches in ([30], [61], [77], [19]):
            assert choose_transpose(pitches, BASS) % 12 == 0

    def test_an_empty_part_is_not_moved(self):
        assert choose_transpose([], BASS) == 0


class TestFingering:
    def test_an_open_string_is_preferred(self):
        string, fret = assign_fingering(33, BASS, previous_fret=5, max_fret=12)
        assert fret == 0

    def test_the_hand_stays_where_it_is(self):
        """The same pitch exists in several places. The playable one is the
        one nearest the hand."""
        near = assign_fingering(40, BASS, previous_fret=2, max_fret=16)
        far = assign_fingering(40, BASS, previous_fret=14, max_fret=16)
        assert near is not None and far is not None
        assert near[1] <= far[1]

    def test_a_pitch_off_the_fretboard_cannot_be_played(self):
        assert assign_fingering(120, BASS, None, max_fret=12) is None
        assert assign_fingering(10, BASS, None, max_fret=12) is None

    def test_the_fret_limit_is_respected(self):
        for midi in range(BASS.midi_min, BASS.midi_max + 1):
            placed = assign_fingering(midi, BASS, None, max_fret=5)
            if placed is not None:
                assert 0 <= placed[1] <= 5


# ── End to end ───────────────────────────────────────────────────────────

class TestConversion:
    def test_a_file_becomes_a_playable_timeline(self, tmp_path):
        path = simple_file(tmp_path, [28, 33, 38, 43])
        timeline = load_midi_file(path, instrument=BASS)
        assert len(timeline) == 4
        assert [n.midi_note for n in timeline.notes] == [28, 33, 38, 43]
        assert all(n.fret == 0 for n in timeline.notes)
        assert [n.string for n in timeline.notes] == [4, 3, 2, 1]

    def test_timing_follows_the_tempo(self, tmp_path):
        path = simple_file(tmp_path, [28, 33], bpm=120.0)
        timeline = load_midi_file(path, instrument=BASS)
        # A quarter note at 120 BPM is 500 ms.
        assert timeline.notes[1].timestamp_ms == pytest.approx(500.0, abs=1.0)

    def test_a_tempo_change_mid_piece_is_followed(self, tmp_path):
        events = (track_name("Bass") + tempo_event(120) + note(28, 0, 480)
                  + tempo_event(60) + note(33, 0, 480) + note(38, 0, 480))
        path = tmp_path / "wechsel.mid"
        path.write_bytes(midi_file([track_chunk(events)]))
        timeline = load_midi_file(path, instrument=BASS)
        starts = [n.timestamp_ms for n in timeline.notes]
        assert starts[1] == pytest.approx(500.0, abs=2.0)      # still 120 BPM
        assert starts[2] == pytest.approx(1500.0, abs=2.0)     # now 60 BPM

    def test_a_chord_becomes_one_note(self, tmp_path):
        """Pitch detection here is monophonic. Drawing a chord the app cannot
        hear would be a lie about what it knows."""
        events = (track_name("Akkord") + vlq(0) + bytes([0x90, 28, 90])
                  + vlq(0) + bytes([0x90, 33, 90]) + vlq(0) + bytes([0x90, 38, 90])
                  + vlq(480) + bytes([0x80, 28, 0]) + vlq(0) + bytes([0x80, 33, 0])
                  + vlq(0) + bytes([0x80, 38, 0]))
        path = tmp_path / "akkord.mid"
        path.write_bytes(midi_file([track_chunk(events)]))
        timeline = load_midi_file(path, instrument=BASS)
        assert len(timeline) == 1
        assert timeline.notes[0].midi_note == 28     # the lowest, for a bass

    def test_notes_do_not_overlap_on_a_lane(self, tmp_path):
        """A held note running into the next one draws two cards on top of
        each other and neither can be read."""
        events = track_name("Lang") + b"".join(
            vlq(0) + bytes([0x90, 28 + i, 90]) + vlq(240) + bytes([0x80, 28 + i, 0])
            if False else note(28, 0, 960) for i in range(3))
        path = tmp_path / "lang.mid"
        path.write_bytes(midi_file([track_chunk(events)]))
        timeline = load_midi_file(path, instrument=BASS)
        notes = sorted(timeline.notes, key=lambda n: n.timestamp_ms)
        for a, b in zip(notes, notes[1:]):
            assert a.end_ms <= b.timestamp_ms + 0.001

    def test_a_part_out_of_range_is_transposed_in(self, tmp_path):
        path = simple_file(tmp_path, [64, 69, 74])     # a guitar melody
        timeline = load_midi_file(path, instrument=BASS)
        assert timeline.notes
        for event in timeline.notes:
            assert BASS.midi_min <= event.midi_note <= BASS.midi_max

    def test_the_tuning_is_recorded_so_the_player_screen_agrees(self, tmp_path):
        path = simple_file(tmp_path, [28, 33])
        timeline = load_midi_file(path, instrument=BASS)
        assert timeline.metadata.tuning == dict(BASS.tuning)

    def test_bar_lines_are_produced(self, tmp_path):
        path = simple_file(tmp_path, [28] * 8)
        timeline = load_midi_file(path, instrument=BASS)
        assert len(timeline.measures) >= 2

    def test_a_file_with_no_playable_track_gives_an_empty_timeline(self, tmp_path):
        drums = track_name("Drums") + b"".join(note(38, 0, 120, channel=9)
                                               for _ in range(8))
        path = tmp_path / "nurdrums.mid"
        path.write_bytes(midi_file([track_chunk(drums)]))
        timeline = load_midi_file(path, instrument=BASS)
        assert len(timeline) == 0

    def test_the_guitar_takes_the_high_part(self, tmp_path):
        high = track_name("Melodie") + b"".join(note(64 + i % 7, 0, 240)
                                                for i in range(24))
        low = track_name("Bass") + b"".join(note(33 + i % 5, 0, 480)
                                            for i in range(24))
        path = tmp_path / "beide.mid"
        path.write_bytes(midi_file([track_chunk(high), track_chunk(low)]))
        assert select_track(read_midi(path)[0], GUITAR).name == "Melodie"


class TestDescribe:
    def test_it_lists_every_track(self, tmp_path):
        a = track_name("Eins") + note(40, 0, 480)
        b = track_name("Zwei") + note(45, 0, 480)
        path = tmp_path / "zwei.mid"
        path.write_bytes(midi_file([track_chunk(a), track_chunk(b)]))
        rows = describe(path, BASS)
        assert [r["name"] for r in rows] == ["Eins", "Zwei"]
        assert all("score" in r for r in rows)
