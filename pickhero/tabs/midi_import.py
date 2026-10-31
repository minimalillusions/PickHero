"""Turn a Standard MIDI File into something playable on a fretted instrument.

Why MIDI and not Guitar Pro: there is no licence-free source of Guitar Pro
files. Every archive that has them in quantity hosts unlicensed transcriptions
of copyrighted songs and forbids automated access in its terms. MIDI is the
opposite -- the Mutopia Project, Wikimedia Commons, classtab.org and the
public-domain parts of MuseScore all publish it freely, and it carries exactly
what this app needs: which pitch, when, and for how long.

What it does not carry is which string and which fret. A MIDI file says "G2",
not "third string, open", and on a fretted instrument the same pitch is
usually playable in three or four places. So the fingering is chosen here,
with a cost function that prefers staying where the hand already is.

Hand-rolled parser, no new dependency. The format is small: a header chunk,
then track chunks of delta-timed events.
"""

from __future__ import annotations

import struct
from bisect import bisect_right
from dataclasses import dataclass, field, replace
from pathlib import Path

from pickhero.tabs.timeline import (
    BeatInfo,
    MeasureInfo,
    NoteEvent,
    SongMetadata,
    Timeline,
)
from pickhero.tabs.import_safety import (
    ImportLimitError, MAX_DURATION_MS, MAX_MEASURES, MAX_NOTES, MAX_TRACKS, MAX_WORK,
    bounded_file, signature, tempo,
)
from pickhero.tabs.tempo import TempoMap
from pickhero.validation import text

# Meta and channel event types we care about.
_META = 0xFF
_SYSEX = 0xF0
_SYSEX_END = 0xF7
_NOTE_OFF = 0x80
_NOTE_ON = 0x90
_PROGRAM_CHANGE = 0xC0

_META_TRACK_NAME = 0x03
_META_END_OF_TRACK = 0x2F
_META_TEMPO = 0x51
_META_TIME_SIGNATURE = 0x58

# General MIDI programme numbers. Percussion is channel 9 whatever the
# programme says.
_DRUM_CHANNEL = 9


class MidiError(ValueError):
    """The file is not a Standard MIDI File we can read."""


@dataclass
class _Event:
    tick: int
    kind: str          # "note" | "tempo" | "time_signature"
    pitch: int = 0
    duration: int = 0
    velocity: int = 0
    channel: int = 0
    value: float = 0.0
    numerator: int = 4
    denominator: int = 4


@dataclass
class MidiTrack:
    index: int
    name: str
    channel: int
    programme: int
    notes: list[_Event]
    is_percussion: bool
    meta: list[_Event] = field(default_factory=list)
    end_tick: int = 0

    @property
    def note_count(self) -> int:
        return len(self.notes)

    @property
    def pitch_range(self) -> tuple[int, int]:
        if not self.notes:
            return (0, 0)
        pitches = [n.pitch for n in self.notes]
        return (min(pitches), max(pitches))


# ── Parsing ──────────────────────────────────────────────────────────────

class _Reader:
    """A cursor over the bytes, which refuses to walk off the end."""

    def __init__(self, data: bytes):
        self._data = data
        self._pos = 0

    def __len__(self) -> int:
        return len(self._data) - self._pos

    def byte(self) -> int:
        if self._pos >= len(self._data):
            raise MidiError("Datei endet mitten in einem Ereignis")
        value = self._data[self._pos]
        self._pos += 1
        return value

    def take(self, count: int) -> bytes:
        if count < 0 or self._pos + count > len(self._data):
            raise MidiError("Datei endet mitten in einem Datenblock")
        chunk = self._data[self._pos:self._pos + count]
        self._pos += count
        return chunk

    def variable(self) -> int:
        """A MIDI variable-length quantity: seven bits per byte, high bit set
        on every byte but the last."""
        value = 0
        for _ in range(4):
            byte = self.byte()
            value = (value << 7) | (byte & 0x7F)
            if not byte & 0x80:
                return value
        raise MidiError("Zeitangabe ist laenger als das Format erlaubt")


def _parse_track(data: bytes, index: int, budget: list[int] | None = None) -> MidiTrack:
    """One track chunk into absolute-time note events."""
    reader = _Reader(data)
    tick = 0
    status = 0
    name = ""
    programme = -1
    channel = -1
    sounding: dict[tuple[int, int], tuple[int, int]] = {}
    notes: list[_Event] = []
    extra: list[_Event] = []
    budget = budget if budget is not None else [MAX_WORK, MAX_NOTES]

    while len(reader) > 0:
        budget[0] -= 1
        if budget[0] < 0:
            raise MidiError("Zu viele MIDI-Ereignisse")
        tick += reader.variable()
        if tick > MAX_DURATION_MS * 32767:
            raise MidiError("MIDI-Zeitangabe ueberschreitet das Importlimit")
        byte = reader.byte()
        if byte & 0x80:
            status = byte
        else:
            # Running status: the previous status byte is reused and this byte
            # is already the first data byte.
            if not 0x80 <= status < 0xF0:
                raise MidiError("Datenbyte ohne vorangehenden Status")
            reader._pos -= 1

        if status == _META:
            meta_type = reader.byte()
            payload = reader.take(reader.variable())
            if meta_type == _META_END_OF_TRACK:
                break
            if meta_type == _META_TRACK_NAME and not name:
                if len(payload) > 4096:
                    raise MidiError("MIDI-Spurname ist zu lang")
                name = payload.decode("latin-1", "replace").strip()
                if not text(name):
                    raise MidiError("Ungueltiger MIDI-Spurname")
            elif meta_type == _META_TEMPO and len(payload) == 3:
                micros = (payload[0] << 16) | (payload[1] << 8) | payload[2]
                if not micros:
                    raise MidiError("Ungueltiges MIDI-Tempo")
                extra.append(_Event(tick, "tempo", value=tempo(60_000_000.0 / micros)))
            elif meta_type == _META_TIME_SIGNATURE and len(payload) >= 2:
                num, den = signature(payload[0], 2 ** payload[1])
                extra.append(_Event(tick, "time_signature",
                                    numerator=num, denominator=den))
            continue

        if status in (_SYSEX, _SYSEX_END):
            reader.take(reader.variable())
            continue

        kind = status & 0xF0
        chan = status & 0x0F
        if kind in (_NOTE_ON, _NOTE_OFF):
            pitch = reader.byte() & 0x7F
            velocity = reader.byte() & 0x7F
            if channel < 0:
                channel = chan
            key = (chan, pitch)
            # A note-on with velocity zero is a note-off. Every sequencer
            # writes it that way to keep running status going.
            if kind == _NOTE_ON and velocity > 0:
                budget[1] -= 1
                if budget[1] < 0:
                    raise MidiError("Zu viele MIDI-Noten")
                sounding[key] = (tick, velocity)
            else:
                started = sounding.pop(key, None)
                if started is not None:
                    notes.append(_Event(started[0], "note", pitch=pitch,
                                        duration=max(1, tick - started[0]),
                                        velocity=started[1], channel=chan))
        elif kind == _PROGRAM_CHANGE:
            value = reader.byte() & 0x7F
            extra.append(_Event(tick, "program", channel=chan, value=value))
            if programme < 0:
                programme = value
            if channel < 0:
                channel = chan
        elif kind in (0xA0, 0xB0, 0xE0):
            reader.take(2)
        elif kind == 0xD0:
            reader.take(1)
        else:
            raise MidiError(f"Unbekannter Ereignistyp {status:#04x}")

    # Anything still held at the end of the track gets a nominal length rather
    # than being dropped: a missing note-off is common in hand-written files.
    for (chan, pitch), (start, velocity) in sounding.items():
        notes.append(_Event(start, "note", pitch=pitch, duration=max(1, tick - start),
                            velocity=velocity, channel=chan))

    notes.sort(key=lambda e: (e.tick, e.pitch))
    track = MidiTrack(index=index, name=name, channel=max(channel, 0),
                      programme=max(programme, 0), notes=notes,
                      is_percussion=channel == _DRUM_CHANNEL,
                      meta=extra, end_tick=tick)
    return track


def read_midi(path: str | Path) -> tuple[list[MidiTrack], int, list[_Event]]:
    """(tracks, ticks_per_quarter, meta events) from a Standard MIDI File."""
    try:
        raw = bounded_file(path)
    except ImportLimitError as exc:
        raise MidiError(str(exc)) from exc
    if len(raw) < 14 or raw[:4] != b"MThd":
        raise MidiError("Keine MIDI-Datei: der Kopf fehlt")
    header_len = struct.unpack(">I", raw[4:8])[0]
    if header_len < 6 or 8 + header_len > len(raw):
        raise MidiError("MIDI-Kopf ist zu kurz")
    fmt, count, division = struct.unpack(">HHH", raw[8:14])
    if fmt not in (0, 1):
        raise MidiError(f"MIDI-Format {fmt} wird nicht unterstuetzt")
    if count > MAX_TRACKS:
        raise MidiError("Zu viele MIDI-Spuren")
    if division & 0x8000:
        raise MidiError("SMPTE-Zeitbasis wird nicht unterstuetzt")
    if not division:
        raise MidiError("MIDI-Zeitbasis darf nicht null sein")
    ticks_per_quarter = division

    pos = 8 + header_len
    tracks: list[MidiTrack] = []
    meta: list[_Event] = []
    budget = [MAX_WORK, MAX_NOTES]
    while pos < len(raw):
        if pos + 8 > len(raw):
            raise MidiError("Abgeschnittener MIDI-Datenblock")
        kind = raw[pos:pos + 4]
        length = struct.unpack(">I", raw[pos + 4:pos + 8])[0]
        if pos + 8 + length > len(raw):
            raise MidiError("Abgeschnittener MIDI-Datenblock")
        chunk = raw[pos + 8:pos + 8 + length]
        pos += 8 + length
        if kind != b"MTrk":
            continue           # Unknown chunk types are skipped by the spec.
        if len(tracks) >= MAX_TRACKS:
            raise MidiError("Zu viele MIDI-Spuren")
        try:
            track = _parse_track(chunk, len(tracks), budget)
        except ImportLimitError as exc:
            raise MidiError(str(exc)) from exc
        meta.extend(getattr(track, "meta", []))
        tracks.append(track)

    if not tracks:
        raise MidiError("Die Datei enthaelt keine Spuren")
    meta.sort(key=lambda e: e.tick)
    timing = _tempo_map(meta, ticks_per_quarter)
    try:
        timing.tick_to_ms(max(track.end_tick for track in tracks))
    except ImportLimitError as exc:
        raise MidiError(str(exc)) from exc
    return tracks, ticks_per_quarter, meta


def _tempo_map(meta, ticks_per_quarter):
    result = TempoMap(120, ticks_per_quarter)
    for event in meta:
        if event.kind == "tempo":
            result.add_change(event.tick, event.value)
    return result


def extract_midi_backing(path: str | Path, exclude_track_indices=None,
                         instrument=None):
    """Preserve original MIDI channels, pitches and tempo in accompaniment."""
    from pickhero.audio.midi_playback import (
        BackingTrack, MidiEvent, NOTE_OFF, NOTE_ON, PROGRAM_CHANGE,
    )
    if instrument is None:
        from pickhero.instruments import GUITAR
        instrument = GUITAR
    tracks, resolution, meta = read_midi(path)
    timing = _tempo_map(meta, resolution)
    if exclude_track_indices is None:
        selected = select_track(tracks, instrument)
        exclude_track_indices = {selected.index} if selected else set()
    events = []
    for track in tracks:
        if track.index in exclude_track_indices:
            continue
        for event in getattr(track, "meta", []):
            if event.kind == "program":
                events.append(MidiEvent(timing.tick_to_ms(event.tick), event.channel,
                                        PROGRAM_CHANGE, int(event.value)))
        for note in track.notes:
            events.append(MidiEvent(timing.tick_to_ms(note.tick), note.channel,
                                    NOTE_ON, note.pitch, note.velocity))
            events.append(MidiEvent(timing.tick_to_ms(note.tick + note.duration),
                                    note.channel, NOTE_OFF, note.pitch, 0))
    return BackingTrack(events)


# ── Choosing what to play ────────────────────────────────────────────────

def score_track(track: MidiTrack, instrument) -> float:
    """How well this track suits the instrument. Higher is better.

    A MIDI file of a song has a melody, a bass, some chords and often drums.
    On a four-string bass ukulele the bass line is the part to learn, and it
    is recognisable without reading the name: the lowest sustained voice with
    few simultaneous notes.
    """
    if track.is_percussion or not track.notes:
        return -1.0

    low, high = track.pitch_range
    centre = (low + high) / 2.0
    target = (instrument.midi_min + instrument.midi_max) / 2.0
    span = max(instrument.midi_max - instrument.midi_min, 1)

    # How much of it lands in range without transposing.
    inside = sum(1 for n in track.notes
                 if instrument.midi_min <= n.pitch <= instrument.midi_max)
    in_range = inside / len(track.notes)

    # How far the whole part would have to move to fit.
    distance = abs(centre - target) / span

    # How monophonic it is. A chord part is not what a beginner plays.
    starts: dict[int, int] = {}
    for note in track.notes:
        starts[note.tick] = starts.get(note.tick, 0) + 1
    single = sum(1 for count in starts.values() if count == 1) / max(len(starts), 1)

    # Enough notes to be a part rather than a stray marker.
    substance = min(len(track.notes) / 32.0, 1.0)

    name = (track.name or "").lower()
    named = 0.0
    wants_bass = instrument.midi_min < 40
    for word, bass_part in (("bass", True), ("melody", False), ("melodie", False),
                            ("lead", False), ("guitar", False), ("gitarre", False),
                            ("voice", False), ("gesang", False)):
        if word in name:
            named = 0.25 if bass_part == wants_bass else -0.15
            break

    return (1.6 * in_range - 1.0 * distance + 0.6 * single
            + 0.5 * substance + named)


def select_track(tracks: list[MidiTrack], instrument) -> MidiTrack | None:
    playable = [t for t in tracks if t.notes and not t.is_percussion]
    if not playable:
        return None
    return max(playable, key=lambda t: score_track(t, instrument))


def choose_transpose(pitches: list[int], instrument) -> int:
    """Octaves to shift so the part sits in the instrument's range.

    Whole octaves only. Moving a part by anything else changes the key, which
    would put it out of tune with any recording the player plays along to.
    """
    if not pitches:
        return 0
    best_shift, best_score = 0, -1
    for octaves in range(-4, 5):
        shift = octaves * 12
        inside = sum(1 for p in pitches
                     if instrument.midi_min <= p + shift <= instrument.midi_max)
        # Ties go to the smaller move.
        score = inside * 1000 - abs(octaves)
        if score > best_score:
            best_shift, best_score = shift, score
    return best_shift


def assign_fingering(midi_note: int, instrument, previous_fret: int | None,
                     max_fret: int) -> tuple[int, int] | None:
    """(string, fret) for a pitch, or None if it cannot be played.

    Of the three or four places a pitch exists on a fretboard, the playable
    one is whichever keeps the hand where it already is. Open strings win
    outright because they need no hand at all.
    """
    options = []
    for string, open_note in instrument.tuning.items():
        fret = midi_note - open_note
        if 0 <= fret <= max_fret:
            cost = 0.0 if fret == 0 else 1.0 + fret * 0.08
            if previous_fret is not None and fret > 0:
                cost += abs(fret - previous_fret) * 0.35
            options.append((cost, string, fret))
    if not options:
        return None
    _, string, fret = min(options)
    return string, fret


# ── The conversion ───────────────────────────────────────────────────────

def load_midi_file(path: str | Path, instrument=None,
                   track_index: int | None = None,
                   max_fret: int | None = None) -> Timeline:
    """Read a MIDI file and return a playable Timeline.

    The part is collapsed to one note at a time. That is not a simplification
    for convenience: pitch detection here is monophonic, so a chord cannot be
    scored, and showing the player something the app cannot hear would be a
    lie about what it knows.
    """
    if instrument is None:
        from pickhero import instruments as _inst
        instrument = _inst.GUITAR
    if max_fret is None:
        max_fret = instrument.max_fret

    tracks, ticks_per_quarter, meta = read_midi(path)
    if track_index is not None and 0 <= track_index < len(tracks):
        track = tracks[track_index]
    else:
        track = select_track(tracks, instrument)
    if track is None or not track.notes:
        return Timeline([], SongMetadata(title=Path(path).stem))

    tempo_map = _tempo_map(meta, ticks_per_quarter)
    tick_to_ms = tempo_map.tick_to_ms

    # One voice: where several notes start together, keep the one that suits
    # the instrument -- the lowest for a bass, the highest for a guitar.
    wants_low = instrument.midi_min < 40
    by_tick: dict[int, list[_Event]] = {}
    for note in track.notes:
        by_tick.setdefault(note.tick, []).append(note)
    voice = [min(group, key=lambda n: n.pitch) if wants_low
             else max(group, key=lambda n: n.pitch)
             for _, group in sorted(by_tick.items())]

    shift = choose_transpose([n.pitch for n in voice], instrument)

    events: list[NoteEvent] = []
    previous_fret: int | None = None
    skipped = 0
    for index, note in enumerate(voice):
        pitch = note.pitch + shift
        placed = assign_fingering(pitch, instrument, previous_fret, max_fret)
        if placed is None:
            skipped += 1
            continue
        string, fret = placed
        previous_fret = fret if fret > 0 else previous_fret
        start = tick_to_ms(note.tick)
        # A note may not run into the next one: overlapping cards on the same
        # lane are unreadable, and the matcher scores onsets anyway.
        end = tick_to_ms(note.tick + note.duration)
        if index + 1 < len(voice):
            end = min(end, tick_to_ms(voice[index + 1].tick))
        events.append(NoteEvent(
            timestamp_ms=start,
            duration_ms=max(0.0, end - start),
            midi_note=pitch,
            string=string,
            fret=fret,
        ))

    metadata = SongMetadata(
        title=(track.name or Path(path).stem).strip() or Path(path).stem,
        artist="",
        track_name=track.name,
        tempo=tempo_map.tempo_at_tick(0),
        tuning=dict(instrument.tuning),
        track_index=track.index,
    )
    measures, pulses = _midi_measures(events, meta, tempo_map, ticks_per_quarter)
    if measures:
        # Each note in the bar it starts in. Without this every note said
        # measure 0, and everything that works per bar -- the weakest
        # section, a loop, a passage -- saw a MIDI song as one long bar.
        starts = [measure.start_ms for measure in measures]
        events = [replace(event, measure=max(0, bisect_right(starts, event.timestamp_ms) - 1))
                  for event in events]
    return Timeline(events, metadata, measures=measures, beats=pulses,
                    tempo_changes=tempo_map.changes_ms)


def _midi_measures(events, meta, tempo_map, ticks_per_quarter):
    """Respect signature changes and place pulses through the same tempo map."""
    if not events:
        return [], []
    signatures = {0: (4, 4)}
    for event in meta:
        if event.kind == "time_signature":
            signatures[event.tick] = signature(event.numerator, event.denominator)
    changes = sorted(signatures)
    change_index = 0
    tick = 0.0
    end_ms = max(event.end_ms for event in events)
    measures, pulses = [], []
    while tempo_map.tick_to_ms(tick) < end_ms:
        if len(measures) >= MAX_MEASURES or len(pulses) >= MAX_WORK:
            raise MidiError("Zu viele MIDI-Takte")
        while change_index + 1 < len(changes) and changes[change_index + 1] <= tick:
            change_index += 1
        num, den = signatures[changes[change_index]]
        pulse_ticks = ticks_per_quarter * 4 / den
        end_tick = tick + num * pulse_ticks
        if change_index + 1 < len(changes):
            end_tick = min(end_tick, changes[change_index + 1])
        index = len(measures)
        measures.append(MeasureInfo(index, tempo_map.tick_to_ms(tick),
                                    tempo_map.tick_to_ms(end_tick)))
        for beat in range(num):
            at = tick + beat * pulse_ticks
            if at >= end_tick:
                break
            pulses.append(BeatInfo(tempo_map.tick_to_ms(at), index, beat))
        tick = end_tick
    return measures, pulses



def describe(path: str | Path, instrument=None) -> list[dict]:
    """What is in this file, for a picker: one row per track."""
    if instrument is None:
        from pickhero import instruments as _inst
        instrument = _inst.GUITAR
    tracks, _, _ = read_midi(path)
    rows = []
    for track in tracks:
        low, high = track.pitch_range
        rows.append({
            "index": track.index,
            "name": track.name,
            "notes": track.note_count,
            "lowest": low,
            "highest": high,
            "is_percussion": track.is_percussion,
            "programme": track.programme,
            "score": round(score_track(track, instrument), 3),
        })
    return rows
