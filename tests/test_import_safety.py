"""Adversarial score bytes and musical timing through the public import APIs."""

import struct
from pathlib import Path
import zipfile

import pytest
from defusedxml.common import DefusedXmlException

from pickhero.tabs import import_safety as limits
from pickhero.tabs.loader import extract_backing_track, list_tracks, load_gp_file
from pickhero.tabs.midi_import import MidiError, read_midi
from pickhero.tabs.tempo import TempoMap
from pickhero.tabs.timeline import BeatInfo, MeasureInfo, NoteEvent, SongMetadata, Timeline
from tests.test_midi_import import midi_file, note, tempo_event, track_chunk


def gpif(*, repeats=1, bpm="120", time="4/4", tuning="40 45 50 55 59 64",
         extra_tempo="", rhythm="<NoteValue>Quarter</NoteValue>"):
    refs = " ".join(["0"] * repeats)
    return f'''<GPIF>
      <MasterTrack><Automations><Automation><Type>Tempo</Type><Bar>0</Bar>
      <Value>{bpm} 2</Value></Automation>{extra_tempo}</Automations></MasterTrack>
      <Tracks><Track id="0"><Name>Guitar</Name><MIDI><Program>24</Program></MIDI>
      <Properties><Property name="Tuning"><Pitches>{tuning}</Pitches>
      </Property></Properties></Track></Tracks>
      <Rhythms><Rhythm id="0">{rhythm}</Rhythm></Rhythms>
      <Notes><Note id="0"><Properties><Property name="Fret"><Fret>0</Fret></Property>
      <Property name="String"><String>0</String></Property></Properties></Note></Notes>
      <Beats><Beat id="0"><Rhythm ref="0"/><Notes>{refs}</Notes></Beat></Beats>
      <Voices><Voice id="0"><Beats>{refs}</Beats></Voice></Voices>
      <Bars><Bar id="0"><Voices>{refs}</Voices></Bar></Bars>
      <MasterBars>{f'<MasterBar><Time>{time}</Time><Bars>0</Bars></MasterBar>' * repeats}</MasterBars>
      </GPIF>'''


def write_gp(tmp_path, xml):
    path = tmp_path / "sample.gp7"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("Content/score.gpif", xml)
    return path


@pytest.mark.parametrize("operation", [load_gp_file, list_tracks, extract_backing_track])
@pytest.mark.parametrize("declaration", [
    '<!DOCTYPE GPIF>',
    '<!DOCTYPE GPIF [<!ENTITY a "expanded">]>',
    '<!DOCTYPE GPIF [<!ENTITY a SYSTEM "file:///should-not-be-read">]>',
])
def test_all_gp7_paths_forbid_dtd_and_entities(tmp_path, operation, declaration):
    path = write_gp(tmp_path, declaration + gpif())
    with pytest.raises(DefusedXmlException):
        operation(path)


@pytest.mark.parametrize("operation", [load_gp_file, list_tracks, extract_backing_track])
def test_compressed_xml_is_rejected_before_expansion(tmp_path, monkeypatch, operation):
    monkeypatch.setattr(limits, "MAX_XML_BYTES", 4096)
    path = write_gp(tmp_path, gpif().replace("</GPIF>", " " * 8192 + "</GPIF>"))
    assert path.stat().st_size < 4096
    with pytest.raises(limits.ImportLimitError, match="XML"):
        operation(path)


@pytest.mark.parametrize("operation", [load_gp_file, list_tracks, extract_backing_track])
def test_reference_bomb_is_rejected_before_note_allocation(tmp_path, monkeypatch, operation):
    import pickhero.tabs.loader as loader
    monkeypatch.setattr(limits, "MAX_NOTES", 1000)
    def forbidden_note(*args, **kwargs):
        raise AssertionError("Reference preflight must run before note allocation")
    monkeypatch.setattr(loader, "NoteEvent", forbidden_note)
    with pytest.raises(limits.ImportLimitError, match="reference"):
        operation(write_gp(tmp_path, gpif(repeats=8)))


@pytest.mark.parametrize("xml", ["<a>" * 70 + "</a>" * 70,
                                "<GPIF>" + "<x/>" * 51 + "</GPIF>"])
def test_xml_complexity_is_bounded_during_parsing(tmp_path, monkeypatch, xml):
    monkeypatch.setattr(limits, "MAX_XML_ELEMENTS", 50)
    with pytest.raises(limits.ImportLimitError, match="depth/element"):
        load_gp_file(write_gp(tmp_path, xml))


@pytest.mark.parametrize("bpm", ["0", "-1", "nan", "inf", "1e-300", "1001"])
@pytest.mark.parametrize("operation", [load_gp_file, list_tracks, extract_backing_track])
def test_bad_tempo_is_refused_by_every_gp7_path(tmp_path, bpm, operation):
    with pytest.raises(ValueError, match="tempo"):
        operation(write_gp(tmp_path, gpif(bpm=bpm)))


@pytest.mark.parametrize("time", ["4/0", "0/4", "4/3", "1000000/4"])
def test_invalid_time_signature_is_refused(tmp_path, time):
    with pytest.raises(ValueError, match="signature"):
        load_gp_file(write_gp(tmp_path, gpif(time=time)))


def test_huge_tuplet_is_rejected_before_float_conversion(tmp_path):
    rhythm = '<NoteValue>Quarter</NoteValue><PrimaryTuplet num="1" den="' + "9" * 310 + '"/>'
    with pytest.raises(ValueError, match="tuplet"):
        load_gp_file(write_gp(tmp_path, gpif(rhythm=rhythm)))


def test_zip_directory_bound_precedes_zipinfo_allocation(tmp_path, monkeypatch):
    monkeypatch.setattr(limits, "MAX_ZIP_MEMBERS", 2)
    path = tmp_path / "many.gp7"
    with zipfile.ZipFile(path, "w") as archive:
        for index in range(3):
            archive.writestr(str(index), "")
    with pytest.raises(ValueError, match="directory"):
        load_gp_file(path)


def test_file_limit_applies_to_legacy_and_midi(tmp_path, monkeypatch):
    monkeypatch.setattr(limits, "MAX_FILE_BYTES", 16)
    for suffix, operation in [("gp5", load_gp_file), ("mid", read_midi)]:
        path = tmp_path / f"large.{suffix}"
        path.write_bytes(b"a" * 17)
        with pytest.raises(ValueError, match="file limit"):
            operation(path)


def test_legacy_stream_allows_one_optional_eof_byte_but_no_default_loop():
    stream = limits.BoundedGPStream(b"x")
    assert stream.read(1) == b"x"
    assert stream.read(1) == b""
    with pytest.raises(ValueError, match="Truncated"):
        stream.read(1)


@pytest.mark.parametrize("counter", ["measures", "tracks"])
def test_forged_legacy_counts_are_refused_before_allocating_objects(tmp_path, monkeypatch, counter):
    from guitarpro.io import _open
    from guitarpro.gp5 import GP5File
    import guitarpro
    raw = (Path(__file__).parent / "fixtures" / "Slides.gp5").read_bytes()
    stream = limits.BoundedGPStream(raw)
    parser, _ = _open(None, stream, "rb", encoding="cp1252")
    class HeaderReached(Exception):
        pass
    def stop_at_headers(*args):
        raise HeaderReached
    parser.readMeasureHeaders = stop_at_headers
    with pytest.raises(HeaderReached):
        parser.readSong()
    offset = stream.tell() - (8 if counter == "measures" else 4)
    forged = raw[:offset] + struct.pack("<i", 2_147_483_647) + raw[offset + 4:]
    path = tmp_path / "forged.gp5"
    path.write_bytes(forged)
    method = "readMeasureHeader" if counter == "measures" else "readTrack"
    def must_not_allocate(*args, **kwargs):
        raise AssertionError("Forged count reached allocation loop")
    monkeypatch.setattr(GP5File, method, must_not_allocate)
    with pytest.raises((ValueError, guitarpro.GPException), match="Too many legacy"):
        load_gp_file(path)


@pytest.mark.parametrize("field", ["timestamp_ms", "duration_ms"])
@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf"), 1e300])
def test_nonfinite_or_unbounded_note_times_are_refused(field, value):
    data = dict(timestamp_ms=0, duration_ms=500, midi_note=40, string=6, fret=0)
    data[field] = value
    with pytest.raises(ValueError):
        NoteEvent(**data)


def test_tempos_at_tick_zero_override_default_and_durations_integrate_changes():
    timing = TempoMap(120)
    timing.add_change(0, 60)
    timing.add_change(960, 120)
    assert timing.tick_to_ms(960) == 1000
    assert timing.duration_ticks_to_ms(1920, 0) == 1500


def test_gp7_seven_and_eight_string_low_notes_are_preserved(tmp_path):
    for tuning, count in [("35 40 45 50 55 59 64", 7), ("30 35 40 45 50 55 59 64", 8)]:
        path = write_gp(tmp_path, gpif(tuning=tuning))
        timeline = load_gp_file(path)
        assert timeline.notes[0].string == count
        assert timeline.notes[0].midi_note == int(tuning.split()[0])
        assert list_tracks(path)[0]["is_guitar"]


def test_gp7_intra_bar_tempo_affects_notes_beats_and_backing(tmp_path):
    change = '<Automation><Type>Tempo</Type><Bar>0</Bar><Position>0.5</Position><Value>60 2</Value></Automation>'
    xml = gpif(extra_tempo=change, rhythm="<NoteValue>Whole</NoteValue>")
    path = write_gp(tmp_path, xml)
    timeline = load_gp_file(path)
    assert timeline.notes[0].duration_ms == 3000
    assert timeline.measures[0].end_ms == 3000
    assert [beat.timestamp_ms for beat in timeline.beats_in_range(0, 3000)] == [0, 500, 1000, 2000]
    assert timeline.beat_duration_at(1500) == 1000
    backing = extract_backing_track(path, exclude_track_indices=set())
    assert max(event.timestamp_ms for event in backing.events) == 3000


def test_timeline_keeps_trailing_silence_and_bar_accents():
    timeline = Timeline([NoteEvent(0, 500, 40, 6, 0)], SongMetadata(tempo=60),
                        measures=[MeasureInfo(0, 0, 3000), MeasureInfo(1, 3000, 6000)])
    assert timeline.duration_ms == 6000
    assert timeline.beats_in_range(2900, 4000) == [BeatInfo(3000, 1, 0)]
    assert timeline.beats_in_range(0, 2000)[0].accent
    assert not timeline.beats_in_range(0, 2000)[1].accent


def test_midi_chunk_length_is_verified_even_after_complete_end_event(tmp_path):
    path = tmp_path / "truncated.mid"
    raw = midi_file([track_chunk(note(40, 0, 480))])
    raw = raw[:18] + struct.pack(">I", 5000) + raw[22:]
    path.write_bytes(raw)
    with pytest.raises(MidiError, match="Datenblock"):
        read_midi(path)


def test_midi_event_budget_counts_events_without_notes(tmp_path, monkeypatch):
    import pickhero.tabs.midi_import as midi
    monkeypatch.setattr(midi, "MAX_WORK", 3)
    path = tmp_path / "events.mid"
    path.write_bytes(midi_file([track_chunk(b"\x00\xb0\x01\x01" * 4)]))
    with pytest.raises(MidiError, match="Ereignisse"):
        read_midi(path)


def test_midi_signature_and_tempo_changes_define_grid(tmp_path):
    signature = b"\x00\xff\x58\x04\x03\x02\x18\x08"  # 3/4
    events = tempo_event(120) + note(40, 0, 1920) + signature + tempo_event(60) + note(45, 0, 1440)
    path = tmp_path / "changes.mid"
    path.write_bytes(midi_file([track_chunk(events)]))
    timeline = load_gp_file(path)
    assert [(m.start_ms, m.end_ms) for m in timeline.measures] == [(0, 2000), (2000, 5000)]
    pulses = timeline.beats_in_range(0, 5000)
    assert [p.timestamp_ms for p in pulses] == [0, 500, 1000, 1500, 2000, 3000, 4000]
    assert [p.timestamp_ms for p in pulses if p.accent] == [0, 2000]


def test_midi_backing_excludes_selected_part_and_follows_tempo(tmp_path):
    path = tmp_path / "band.mid"
    path.write_bytes(midi_file([
        track_chunk(tempo_event(120) + note(40, 0, 480)),
        track_chunk(b"\x00\xc2\x18" + note(64, 0, 480, channel=2)
                    + tempo_event(60) + note(65, 0, 480, channel=2)),
    ]))
    backing = extract_backing_track(path, exclude_track_indices={0})
    notes = [event for event in backing.events if event.event_type == 0x90]
    assert [(e.timestamp_ms, e.channel, e.data1) for e in notes] == [(0, 2, 64), (500, 2, 65)]
    assert max(e.timestamp_ms for e in backing.events) == 1500
