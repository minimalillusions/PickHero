"""Guitar Pro file loader.

Parses GP3/GP4/GP5 files via pyguitarpro, and GP7/GP8 files (ZIP+XML) via
bounded, hardened XML parsing. Builds note timelines for both formats.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import guitarpro

from pickhero.audio.midi_playback import (
    NOTE_ON, NOTE_OFF, PROGRAM_CHANGE, BackingTrack, MidiEvent,
)
from pickhero.tabs.timeline import BeatInfo, MeasureInfo, NoteEvent, SongMetadata, Timeline, MAX_STRINGS
from pickhero.tabs.import_safety import (
    read_gpif, signature, parse_legacy_score,
)
from pickhero.tabs.tempo import TempoMap

# GP tick resolution: 960 ticks = 1 quarter note
TICKS_PER_QUARTER = 960

# MIDI program numbers for guitar instruments (General MIDI)
GUITAR_INSTRUMENT_MIN = 24  # Nylon String Guitar
GUITAR_INSTRUMENT_MAX = 31  # Guitar Harmonics

# General MIDI bass programs. A bass ukulele is tuned exactly like a 4-string
# bass guitar, so a tab's bass track is the one to play on it.
BASS_INSTRUMENT_MIN = 32  # Acoustic Bass
BASS_INSTRUMENT_MAX = 39  # Synth Bass 2


def _parse_legacy(path):
    return parse_legacy_score(path)


def is_guitar_track(track: guitarpro.Track) -> bool:
    """Check if a track is a guitar track.

    Criteria: 6-12 strings, MIDI instrument 24-31, not percussion.
    """
    return (
        6 <= len(track.strings) <= MAX_STRINGS
        and GUITAR_INSTRUMENT_MIN <= track.channel.instrument <= GUITAR_INSTRUMENT_MAX
        and not track.channel.isPercussionChannel
    )


def is_bass_track(track: guitarpro.Track) -> bool:
    """Check if a track is a bass track.

    Criteria: 4 to 6 strings, GM instrument 32-39, not percussion.
    """
    return (
        4 <= len(track.strings) <= 6
        and BASS_INSTRUMENT_MIN <= track.channel.instrument <= BASS_INSTRUMENT_MAX
        and not track.channel.isPercussionChannel
    )


def score_track(info: dict, instrument) -> int:
    """How well a track suits an instrument profile. Higher is better.

    Zero or less means unusable. Tuning is trusted over the General MIDI
    program, because the program is often wrong in files from tab sites while
    the tuning array has to be right for the tab to make sense at all.
    """
    if info.get("is_percussion"):
        return -1

    score = 0
    tuning = info.get("tuning") or {}
    if tuning and tuning == instrument.tuning:
        score += 100
    elif len(tuning) == instrument.string_count:
        score += 40
        gap = abs(min(tuning.values()) - instrument.midi_min)
        if gap <= 2:
            score += 30
        elif gap <= 12:
            score += 10

    prog = info.get("instrument", -1)
    wants_bass = instrument.midi_min < GUITAR_INSTRUMENT_MIN + 16  # below guitar low E
    if wants_bass:
        if BASS_INSTRUMENT_MIN <= prog <= BASS_INSTRUMENT_MAX:
            score += 50
    elif GUITAR_INSTRUMENT_MIN <= prog <= GUITAR_INSTRUMENT_MAX:
        score += 50

    return score


def track_matches_instrument(info: dict, instrument) -> bool:
    """Whether the written strings can be used on the selected instrument.

    A four-string part remains usable on a five-string bass. The reverse is
    not true: even unused low-string lanes should not turn a four-string
    exercise into a five-string one. Tuning distinguishes bass from guitar
    more reliably than the MIDI program, while allowing alternate tunings.
    """
    if info.get("is_percussion"):
        return False
    tuning = info.get("tuning") or {}
    if not tuning or len(tuning) > instrument.string_count:
        return False
    return all(string in instrument.tuning
               and abs(pitch - instrument.tuning[string]) <= 12
               for string, pitch in tuning.items())


def select_track_index(infos: list[dict], instrument) -> int:
    """Pick the track a player of `instrument` should be shown.

    Falls back to the first non-percussion track rather than blindly to track
    0, which in real files is often a vocal line or a synth pad.
    """
    if not infos:
        return 0
    compatible = [info for info in infos if track_matches_instrument(info, instrument)]
    if compatible:
        return max(compatible, key=lambda info: score_track(info, instrument))["index"]
    best_score, best_index = -999, None
    for info in infos:
        s = score_track(info, instrument)
        if s > best_score:
            best_score, best_index = s, info["index"]
    if best_score > 0 and best_index is not None:
        return best_index
    for info in infos:
        if not info.get("is_percussion"):
            return info["index"]
    return 0


def _build_tempo_map(song: guitarpro.Song) -> TempoMap:
    """Build a TempoMap from the tempo changes anywhere in the song.

    A tempo change is written on whichever track the author happened to have
    selected. Reading only track 0 lost every change on a file where that was
    not the first track, and the piece then played at its opening tempo from
    beginning to end while the notes scrolled past at the wrong moments.
    """
    tempo_map = TempoMap(song.tempo)
    changes: dict[int, int] = {}
    for track in song.tracks:
        for measure in track.measures:
            for voice in measure.voices:
                for beat in voice.beats:
                    mtc = beat.effect.mixTableChange
                    if mtc and mtc.tempo:
                        changes.setdefault(beat.start, mtc.tempo.value)
    for tick in sorted(changes):
        tempo_map.add_change(tick, changes[tick])
    return tempo_map


def _capo(track: guitarpro.Track) -> int:
    """Fret the capo sits on, 0 for none.

    Guitar Pro writes fret numbers relative to the capo, so the pitch that
    actually sounds is the capo higher than the tab says. pyguitarpro's
    realValue does not include it.
    """
    try:
        return max(0, int(getattr(track, "offset", 0) or 0))
    except (TypeError, ValueError):
        return 0


def _extract_tuning(track: guitarpro.Track) -> dict[int, int]:
    """Extract string tuning as {string_number: midi_note}, capo included."""
    capo = _capo(track)
    return {i + 1: s.value + capo for i, s in enumerate(track.strings)}


def _extract_notes(track: guitarpro.Track, tempo_map: TempoMap) -> list[NoteEvent]:
    """Extract all playable notes from a track, at the pitch they sound."""
    capo = _capo(track)
    notes = []
    for measure_idx, measure in enumerate(track.measures):
        for voice in measure.voices:
            for beat in voice.beats:
                timestamp_ms = tempo_map.tick_to_ms(beat.start)
                duration_ms = tempo_map.duration_ticks_to_ms(
                    beat.duration.time, beat.start
                )
                for note in beat.notes:
                    # Keep normal (1) and dead (3), skip rest (0) and tie (2)
                    if note.type.value not in (1, 3):
                        continue
                    notes.append(
                        NoteEvent(
                            timestamp_ms=timestamp_ms,
                            duration_ms=duration_ms,
                            midi_note=note.realValue + capo,
                            string=note.string,
                            fret=note.value,
                            measure=measure_idx,
                        )
                    )
    return notes


def _extract_measures(track: guitarpro.Track, tempo_map: TempoMap) -> list[MeasureInfo]:
    """Extract measure time ranges from a track."""
    measures = []
    for idx, measure in enumerate(track.measures):
        # Each measure has a header with start tick. We compute start/end from beats.
        beats_in_measure = []
        for voice in measure.voices:
            for beat in voice.beats:
                beats_in_measure.append(beat.start)
                beats_in_measure.append(beat.start + beat.duration.time)

        if beats_in_measure:
            start_tick = min(beats_in_measure)
            end_tick = max(beats_in_measure)
            start_ms = tempo_map.tick_to_ms(start_tick)
            end_ms = tempo_map.tick_to_ms(end_tick)
        else:
            # Empty measure — use previous end or 0
            if measures:
                start_ms = measures[-1].end_ms
            else:
                start_ms = 0.0
            end_ms = start_ms

        measures.append(MeasureInfo(index=idx, start_ms=start_ms, end_ms=end_ms))
    return measures


def _legacy_beats(track, tempo_map):
    beats = []
    measures = []
    for index, measure in enumerate(track.measures):
        header = measure.header
        num, den = signature(header.timeSignature.numerator,
                             header.timeSignature.denominator.value)
        start = header.start
        step = TICKS_PER_QUARTER * 4 / den
        end = start + num * step
        measures.append(MeasureInfo(index, tempo_map.tick_to_ms(start), tempo_map.tick_to_ms(end)))
        for pulse in range(num):
            beats.append(BeatInfo(tempo_map.tick_to_ms(start + pulse * step), index, pulse))
    return measures, beats


def list_tracks(path: str | Path) -> list[dict]:
    """List all tracks in a GP file with metadata.

    Returns dicts with keys: index, name, strings, instrument, tuning,
    is_percussion, is_guitar, is_bass. `tuning` uses this project's string
    numbering, where string 1 is the highest pitched.

    Handles GP7/GP8 as well. It previously called guitarpro.parse()
    unconditionally, which raises on those, so any track picker built on it
    would have crashed on modern tabs.
    """
    if str(path).lower().endswith((".mid", ".midi")):
        from pickhero.tabs.midi_import import describe
        rows = []
        for row in describe(path):
            rows.append({
                "index": row["index"],
                "name": row["name"] or f"Spur {row['index'] + 1}",
                "strings": 0,
                "instrument": row["programme"],
                "tuning": {},
                "is_percussion": row["is_percussion"],
                "is_guitar": False,
                "is_bass": False,
                "notes": row["notes"],
            })
        return rows

    if _is_gp7_file(path):
        return _gp7_track_infos(path)

    song = _parse_legacy(path)
    tracks = []
    for i, track in enumerate(song.tracks):
        tracks.append(
            {
                "index": i,
                "name": track.name,
                "strings": len(track.strings),
                "instrument": track.channel.instrument,
                "tuning": _extract_tuning(track),
                "is_percussion": track.channel.isPercussionChannel,
                "is_guitar": is_guitar_track(track),
                "is_bass": is_bass_track(track),
            }
        )
    return tracks


def _gp7_tuning_dict(pitches: list[int]) -> dict[int, int]:
    """GPIF stores tuning low string first; this project numbers 1 = highest."""
    n = len(pitches)
    return {n - i: midi for i, midi in enumerate(pitches)}


def _gp7_track_infos(path: str | Path) -> list[dict]:
    """Track table of a GP7/GP8 file, in the same shape as list_tracks."""
    return _gp7_track_infos_from_root(read_gpif(path))


def _gp7_track_infos_from_root(root) -> list[dict]:
    infos: list[dict] = []
    tracks_el = root.find("Tracks")
    if tracks_el is None:
        return infos
    for i, t in enumerate(tracks_el.findall("Track")):
        pitches: list[int] = []
        for prop in t.findall(".//Property"):
            if prop.get("name") == "Tuning":
                pitches = [int(x) for x in prop.findtext("Pitches", "").split() if x]
        pitches = [pitch + _gp7_capo(t) for pitch in pitches]
        try:
            prog = int(t.findtext(".//MIDI/Program", "-1"))
        except ValueError:
            prog = -1
        is_drum = (prog == 1024 or t.findtext(".//InstrumentSet", "") == "drums")
        infos.append({
            "index": i,
            "id": t.get("id", ""),
            "name": t.findtext("Name", "").strip(),
            "strings": len(pitches),
            "instrument": prog,
            "tuning": _gp7_tuning_dict(pitches),
            "is_percussion": is_drum,
            "is_guitar": (6 <= len(pitches) <= MAX_STRINGS
                          and GUITAR_INSTRUMENT_MIN <= prog <= GUITAR_INSTRUMENT_MAX),
            "is_bass": (4 <= len(pitches) <= 6
                        and BASS_INSTRUMENT_MIN <= prog <= BASS_INSTRUMENT_MAX
                        and not is_drum),
        })
    return infos


def _is_gp7_file(path: str | Path) -> bool:
    """Check if a file is GP7/GP8 format (ZIP with Content/score.gpif)."""
    try:
        return zipfile.is_zipfile(str(path))
    except OSError:
        return False


# ── GP7/GP8 loader ──────────────────────────────────────────────────────────

# Rhythm note value → duration in quarter notes
# Guitar Pro writes every one of these. The four at the ends were missing, so
# a breve came out a quarter note long and a 128th four times too long: the
# note cards were the wrong width and, worse, every following beat in the bar
# sat at the wrong millisecond.
_GP7_NOTE_VALUES = {
    "Long": 16.0, "DoubleWhole": 8.0,
    "Whole": 4.0, "Half": 2.0, "Quarter": 1.0, "Eighth": 0.5,
    "16th": 0.25, "32nd": 0.125, "64th": 0.0625,
    "128th": 0.03125, "256th": 0.015625,
}


def _gp7_capo(track_el) -> int:
    """Capo fret from a GPIF Track element, 0 for none."""
    for prop in track_el.findall(".//Property"):
        if prop.get("name") in ("CapoFret", "Capo"):
            for tag in ("Fret", "Number", "Float"):
                raw = prop.findtext(tag, "")
                if raw:
                    try:
                        return max(0, int(float(raw)))
                    except (ValueError, OverflowError):
                        return 0
    return 0


def _gp7_timing(root):
    """Master-bar tick positions and all tempo changes, including within bars."""
    bars = root.findall("./MasterBars/MasterBar")
    starts, signatures = [], []
    current = 0.0
    for bar in bars:
        num, den = signature(*bar.findtext("Time", "4/4").split("/"))
        starts.append(current)
        signatures.append((num, den))
        current += num * 4 / den * TICKS_PER_QUARTER
    tempo_map = TempoMap(120)
    for auto in root.findall(".//MasterTrack/Automations/Automation"):
        if auto.findtext("Type") != "Tempo":
            continue
        bar = int(auto.findtext("Bar", "0"))
        if bar >= len(starts):
            continue
        position = float(auto.findtext("Position", "0"))
        num, den = signatures[bar]
        tick = starts[bar] + position * num * 4 / den * TICKS_PER_QUARTER
        tempo_map.add_change(tick, float(auto.findtext("Value", "120 2").split()[0]))
    beats = [BeatInfo(tempo_map.tick_to_ms(start + pulse * 4 / den * TICKS_PER_QUARTER), index, pulse)
             for index, (start, (num, den)) in enumerate(zip(starts, signatures))
             for pulse in range(num)]
    return tempo_map, starts, signatures, beats


def _load_gp7_file(path: str | Path, track_index: int | None = None,
                   instrument=None) -> Timeline:
    """Load a GP7/GP8 file (ZIP containing Content/score.gpif XML)."""
    root = read_gpif(path)

    # ── Parse lookup tables ──────────────────────────────────────────────

    # Rhythms: id → duration in quarter notes
    rhythms: dict[str, float] = {}
    rhythms_el = root.find("Rhythms")
    if rhythms_el is not None:
        for r in rhythms_el.findall("Rhythm"):
            rid = r.get("id", "")
            nv = r.findtext("NoteValue", "Quarter")
            base = _GP7_NOTE_VALUES.get(nv, 1.0)
            dot = r.find("AugmentationDot")
            if dot is not None:
                dot_count = int(dot.get("count", "1"))
                base *= 2 - 2 ** (-dot_count)
            tuplet = r.find("PrimaryTuplet")
            if tuplet is not None:
                num = int(tuplet.get("num", "1"))
                den = int(tuplet.get("den", "1"))
                if num > 0:
                    base = base * den / num
            rhythms[rid] = base

    # Notes: id → (fret, gp7_string)  (gp7_string is 0-indexed, 0=low E)
    notes_map: dict[str, tuple[int, int]] = {}
    notes_el = root.find("Notes")
    if notes_el is not None:
        for n in notes_el.findall("Note"):
            nid = n.get("id", "")
            fret = None
            string_val = None
            for prop in n.findall(".//Property"):
                pname = prop.get("name", "")
                if pname == "Fret":
                    try:
                        fret = int(prop.findtext("Fret", "0"))
                    except (ValueError, OverflowError):
                        pass
                elif pname == "String":
                    try:
                        raw = prop.findtext("String", "0")
                        f = float(raw)
                        if f != int(f):
                            continue  # fractional = drum/percussion, skip
                        string_val = int(f)
                    except (ValueError, OverflowError):
                        pass
            if fret is not None and string_val is not None:
                notes_map[nid] = (fret, string_val)

    # Beats: id → (rhythm_ref, [note_ids])
    beats_map: dict[str, tuple[str, list[str]]] = {}
    beats_el = root.find("Beats")
    if beats_el is not None:
        for b in beats_el.findall("Beat"):
            bid = b.get("id", "")
            rhythm_ref_el = b.find("Rhythm")
            rhythm_ref = rhythm_ref_el.get("ref", "") if rhythm_ref_el is not None else ""
            notes_text = b.findtext("Notes", "").strip()
            note_ids = notes_text.split() if notes_text else []
            beats_map[bid] = (rhythm_ref, note_ids)

    # Voices: id → [beat_ids]
    voices_map: dict[str, list[str]] = {}
    voices_el = root.find("Voices")
    if voices_el is not None:
        for v in voices_el.findall("Voice"):
            vid = v.get("id", "")
            beats_text = v.findtext("Beats", "").strip()
            voices_map[vid] = beats_text.split() if beats_text else []

    # Bars: id → [voice_ids]
    bars_map: dict[str, list[str]] = {}
    bars_el = root.find("Bars")
    if bars_el is not None:
        for bar in bars_el.findall("Bar"):
            bid = bar.get("id", "")
            voices_text = bar.findtext("Voices", "").strip()
            bars_map[bid] = voices_text.split() if voices_text else []

    # ── Parse tracks ─────────────────────────────────────────────────────

    track_list: list[dict] = []
    tracks_el = root.find("Tracks")
    if tracks_el is not None:
        for t in tracks_el.findall("Track"):
            tuning_pitches: list[int] = []
            for prop in t.findall(".//Property"):
                if prop.get("name") == "Tuning":
                    raw = prop.findtext("Pitches", "")
                    tuning_pitches = [int(x) for x in raw.split() if x]
            capo = _gp7_capo(t)
            if capo:
                tuning_pitches = [pitch + capo for pitch in tuning_pitches]
            midi_prog = -1
            try:
                midi_prog = int(t.findtext(".//MIDI/Program", "-1"))
            except ValueError:
                pass
            is_drum = (midi_prog == 1024 or
                       t.findtext(".//InstrumentSet", "") == "drums")
            # Heuristic: drums have instrumentId=1024 in the search API;
            # in GPIF the program is the GM number. Drum kits use channel 10.
            track_list.append({
                "id": t.get("id", ""),
                "name": t.findtext("Name", "").strip(),
                "tuning": tuning_pitches,
                "midi_program": midi_prog,
                "num_strings": len(tuning_pitches),
                "is_guitar": (
                    6 <= len(tuning_pitches) <= MAX_STRINGS
                    and GUITAR_INSTRUMENT_MIN <= midi_prog <= GUITAR_INSTRUMENT_MAX
                ),
                "is_percussion": is_drum,
            })

    # ── Select track ─────────────────────────────────────────────────────

    if track_index is not None:
        selected_index = track_index
    else:
        if instrument is None:
            from pickhero import instruments as _inst
            instrument = _inst.GUITAR
        selected_index = select_track_index(
            _gp7_track_infos_from_root(root), instrument
        )
    if not track_list:
        # A GPIF with no Tracks element at all. Returning an empty timeline
        # lets the caller report "no playable track" instead of dying on an
        # IndexError inside the event loop.
        return Timeline([], SongMetadata(title=Path(path).stem))
    selected_index = min(max(selected_index, 0), len(track_list) - 1)

    sel = track_list[selected_index]
    tuning = sel["tuning"]  # [low_E, A, D, G, B, high_E] (0-indexed)
    num_strings = sel["num_strings"] or 6

    # Build our tuning dict: {1: high_E_midi, ..., 6: low_E_midi}
    tuning_dict: dict[int, int] = {}
    for gp7_idx, midi_val in enumerate(tuning):
        our_string = num_strings - gp7_idx  # 0→6, 1→5, ..., 5→1
        tuning_dict[our_string] = midi_val

    # ── Parse tempos ─────────────────────────────────────────────────────

    tempo_map, bar_ticks, bar_signatures, pulses = _gp7_timing(root)
    initial_bpm = tempo_map.tempo_at_tick(0)

    # ── Walk MasterBars → extract notes and measures ─────────────────────

    master_bars = root.findall(".//MasterBar")
    note_events: list[NoteEvent] = []
    measure_infos: list[MeasureInfo] = []

    for mb_idx, mb in enumerate(master_bars):
        current_tick = bar_ticks[mb_idx]
        current_ms = tempo_map.tick_to_ms(current_tick)
        ts_num, ts_den = bar_signatures[mb_idx]
        measure_ticks = ts_num * 4 / ts_den * TICKS_PER_QUARTER

        measure_start_ms = current_ms
        measure_duration_ms = tempo_map.duration_ticks_to_ms(measure_ticks, current_tick)

        # Get bar IDs for this master bar (one per track)
        bar_ids_text = mb.findtext("Bars", "").strip()
        bar_ids = bar_ids_text.split()

        if selected_index < len(bar_ids):
            bar_id = bar_ids[selected_index]
            voice_ids = bars_map.get(bar_id, [])

            # Walk voices → beats → notes
            for vid in voice_ids:
                if vid == "-1":
                    continue
                beat_ids = voices_map.get(vid, [])
                beat_tick = current_tick

                for bid in beat_ids:
                    rhythm_ref, note_ids = beats_map.get(bid, ("", []))
                    dur_quarters = rhythms.get(rhythm_ref, 1.0)
                    duration_ticks = dur_quarters * TICKS_PER_QUARTER
                    beat_pos_ms = tempo_map.tick_to_ms(beat_tick)
                    dur_ms = tempo_map.duration_ticks_to_ms(duration_ticks, beat_tick)

                    for nid in note_ids:
                        if nid not in notes_map:
                            continue
                        fret, gp7_string = notes_map[nid]
                        our_string = num_strings - gp7_string
                        if not 1 <= our_string <= min(num_strings, MAX_STRINGS) or gp7_string < 0:
                            continue
                        if 0 <= gp7_string < len(tuning):
                            midi_note = tuning[gp7_string] + fret
                        else:
                            midi_note = fret  # fallback

                        if not 0 <= midi_note <= 127:
                            continue

                        note_events.append(NoteEvent(
                            timestamp_ms=beat_pos_ms,
                            duration_ms=dur_ms,
                            midi_note=midi_note,
                            string=our_string,
                            fret=fret,
                            measure=mb_idx,
                        ))

                    beat_tick += duration_ticks

        measure_infos.append(MeasureInfo(
            index=mb_idx,
            start_ms=measure_start_ms,
            end_ms=measure_start_ms + measure_duration_ms,
        ))

    # ── Build metadata and timeline ──────────────────────────────────────

    title = root.findtext(".//Score/Title", "").strip()
    artist = root.findtext(".//Score/Artist", "").strip()
    album = root.findtext(".//Score/Album", "").strip()

    metadata = SongMetadata(
        title=title,
        artist=artist,
        album=album,
        track_name=sel["name"],
        tempo=initial_bpm,
        tuning=tuning_dict,
        track_index=selected_index,
    )

    return Timeline(note_events, metadata, measures=measure_infos,
                    beats=pulses, tempo_changes=tempo_map.changes_ms)


def _extract_gp7_backing_track(
    path: str | Path,
    exclude_track_indices: set[int] | None = None,
    instrument=None,
) -> BackingTrack:
    """Extract non-guitar tracks from a GP7/GP8 file as MIDI events."""
    root = read_gpif(path)

    # Reuse the same lookup tables as _load_gp7_file
    # Rhythms
    rhythms: dict[str, float] = {}
    rhythms_el = root.find("Rhythms")
    if rhythms_el is not None:
        for r in rhythms_el.findall("Rhythm"):
            rid = r.get("id", "")
            nv = r.findtext("NoteValue", "Quarter")
            base = _GP7_NOTE_VALUES.get(nv, 1.0)
            dot = r.find("AugmentationDot")
            if dot is not None:
                dot_count = int(dot.get("count", "1"))
                base *= 2 - 2 ** (-dot_count)
            tuplet = r.find("PrimaryTuplet")
            if tuplet is not None:
                num = int(tuplet.get("num", "1"))
                den = int(tuplet.get("den", "1"))
                if num > 0:
                    base = base * den / num
            rhythms[rid] = base

    # Notes: id → (fret, gp7_string)
    notes_map: dict[str, tuple[int, int]] = {}
    notes_el = root.find("Notes")
    if notes_el is not None:
        for n in notes_el.findall("Note"):
            nid = n.get("id", "")
            fret = None
            string_val = None
            for prop in n.findall(".//Property"):
                pname = prop.get("name", "")
                if pname == "Fret":
                    try:
                        fret = int(prop.findtext("Fret", "0"))
                    except (ValueError, OverflowError):
                        pass
                elif pname == "String":
                    try:
                        raw = prop.findtext("String", "0")
                        f = float(raw)
                        string_val = int(round(f))
                    except (ValueError, OverflowError):
                        pass
            if fret is not None and string_val is not None:
                notes_map[nid] = (fret, string_val)

    # Beats
    beats_map: dict[str, tuple[str, list[str]]] = {}
    beats_el = root.find("Beats")
    if beats_el is not None:
        for b in beats_el.findall("Beat"):
            bid = b.get("id", "")
            rhythm_ref_el = b.find("Rhythm")
            rhythm_ref = rhythm_ref_el.get("ref", "") if rhythm_ref_el is not None else ""
            notes_text = b.findtext("Notes", "").strip()
            beats_map[bid] = (rhythm_ref, notes_text.split() if notes_text else [])

    # Voices
    voices_map: dict[str, list[str]] = {}
    voices_el = root.find("Voices")
    if voices_el is not None:
        for v in voices_el.findall("Voice"):
            vid = v.get("id", "")
            beats_text = v.findtext("Beats", "").strip()
            voices_map[vid] = beats_text.split() if beats_text else []

    # Bars
    bars_map: dict[str, list[str]] = {}
    bars_el = root.find("Bars")
    if bars_el is not None:
        for bar in bars_el.findall("Bar"):
            bid = bar.get("id", "")
            voices_text = bar.findtext("Voices", "").strip()
            bars_map[bid] = voices_text.split() if voices_text else []

    # Tracks
    track_list: list[dict] = []
    tracks_el = root.find("Tracks")
    if tracks_el is not None:
        for t in tracks_el.findall("Track"):
            tuning_pitches: list[int] = []
            for prop in t.findall(".//Property"):
                if prop.get("name") == "Tuning":
                    raw = prop.findtext("Pitches", "")
                    tuning_pitches = [int(x) for x in raw.split() if x]
            capo = _gp7_capo(t)
            if capo:
                tuning_pitches = [pitch + capo for pitch in tuning_pitches]
            midi_prog = -1
            try:
                midi_prog = int(t.findtext(".//MIDI/Program", "-1"))
            except ValueError:
                pass
            track_list.append({
                "tuning": tuning_pitches,
                "midi_program": midi_prog,
                "num_strings": len(tuning_pitches),
                "is_guitar": (
                    6 <= len(tuning_pitches) <= MAX_STRINGS
                    and GUITAR_INSTRUMENT_MIN <= midi_prog <= GUITAR_INSTRUMENT_MAX
                ),
            })

    if exclude_track_indices is None:
        if instrument is None:
            from pickhero import instruments as _inst
            instrument = _inst.GUITAR
        exclude_track_indices = {
            select_track_index(_gp7_track_infos_from_root(root), instrument)
        }

    tempo_map, bar_ticks, _, _ = _gp7_timing(root)

    # Walk master bars and extract MIDI events for non-excluded tracks
    master_bars = root.findall(".//MasterBar")
    events: list[MidiEvent] = []

    # Program changes at t=0
    for i, ti in enumerate(track_list):
        if i in exclude_track_indices:
            continue
        if ti["midi_program"] >= 0:
            channel = i % 16
            events.append(MidiEvent(
                timestamp_ms=0.0,
                channel=channel,
                event_type=PROGRAM_CHANGE,
                data1=ti["midi_program"],
                data2=0,
            ))

    for mb_idx, mb in enumerate(master_bars):
        current_tick = bar_ticks[mb_idx]

        bar_ids_text = mb.findtext("Bars", "").strip()
        bar_ids = bar_ids_text.split()

        for track_idx, bar_id in enumerate(bar_ids):
            if track_idx in exclude_track_indices:
                continue
            if track_idx >= len(track_list):
                continue

            ti = track_list[track_idx]
            tuning = ti["tuning"]
            channel = track_idx % 16

            voice_ids = bars_map.get(bar_id, [])
            for vid in voice_ids:
                if vid == "-1":
                    continue
                beat_ids = voices_map.get(vid, [])
                beat_tick = current_tick

                for bid in beat_ids:
                    rhythm_ref, note_ids = beats_map.get(bid, ("", []))
                    dur_quarters = rhythms.get(rhythm_ref, 1.0)
                    duration_ticks = dur_quarters * TICKS_PER_QUARTER
                    beat_pos_ms = tempo_map.tick_to_ms(beat_tick)
                    dur_ms = tempo_map.duration_ticks_to_ms(duration_ticks, beat_tick)

                    for nid in note_ids:
                        if nid not in notes_map:
                            continue
                        fret, gp7_string = notes_map[nid]
                        if 0 <= gp7_string < len(tuning):
                            midi_note = tuning[gp7_string] + fret
                        else:
                            midi_note = fret
                        midi_note = max(0, min(127, midi_note))

                        events.append(MidiEvent(
                            timestamp_ms=beat_pos_ms,
                            channel=channel,
                            event_type=NOTE_ON,
                            data1=midi_note,
                            data2=80,
                        ))
                        events.append(MidiEvent(
                            timestamp_ms=beat_pos_ms + dur_ms,
                            channel=channel,
                            event_type=NOTE_OFF,
                            data1=midi_note,
                            data2=0,
                        ))

                    beat_tick += duration_ticks

    return BackingTrack(events)


def load_gp_file(
    path: str | Path,
    track_index: int | None = None,
    instrument=None,
) -> Timeline:
    """Load a GP file and return a Timeline.

    Args:
        path: Path to GP3/GP4/GP5/GP7/GP8 file.
        track_index: Explicit track index to load. If None, auto-selects the
                     track that best suits `instrument`.
        instrument: An Instrument profile. Defaults to guitar, which reproduces
                    the previous behaviour. Pass a bass profile and the file's
                    4-string bass track is chosen instead, which is the whole
                    point: the old selector required exactly 6 strings, so a
                    bass track could never be picked.
    """
    if instrument is None:
        from pickhero import instruments as _inst
        instrument = _inst.GUITAR

    if str(path).lower().endswith((".mid", ".midi")):
        # MIDI is the one notation format that is freely licensed in bulk --
        # Mutopia, Wikimedia Commons and the public-domain parts of MuseScore
        # all publish it -- so it is the answer to "where do I legally get
        # something to play". It carries pitch and time but no fingering, so
        # the importer chooses the string and fret.
        from pickhero.tabs.midi_import import load_midi_file
        return load_midi_file(path, instrument=instrument,
                              track_index=track_index)

    if _is_gp7_file(path):
        return _load_gp7_file(path, track_index, instrument)

    song = _parse_legacy(path)
    tempo_map = _build_tempo_map(song)

    if not song.tracks:
        return Timeline([], SongMetadata(title=song.title or Path(path).stem))

    # Select track
    if track_index is not None:
        selected_index = track_index
        track = song.tracks[track_index]
    else:
        selected_index = select_track_index(list_tracks(path), instrument)
        selected_index = min(selected_index, len(song.tracks) - 1)
        track = song.tracks[selected_index]

    notes = _extract_notes(track, tempo_map)
    measures, pulses = _legacy_beats(track, tempo_map)

    metadata = SongMetadata(
        title=song.title or "",
        artist=song.artist or "",
        album=song.album or "",
        track_name=track.name or "",
        tempo=song.tempo,
        tuning=_extract_tuning(track),
        track_index=selected_index,
    )

    return Timeline(notes, metadata, measures=measures, beats=pulses,
                    tempo_changes=tempo_map.changes_ms)


def extract_backing_track(
    path: str | Path,
    exclude_track_indices: set[int] | None = None,
    instrument=None,
) -> BackingTrack:
    """Extract the accompaniment from a GP file as MIDI events.

    Args:
        path: Path to GP3/GP4/GP5/GP7/GP8 file.
        exclude_track_indices: Track indices to leave out, normally the one the
                               player is playing. If None, excludes the track
                               that `instrument` would have selected.
        instrument: An Instrument profile; defaults to guitar.

    Returns:
        BackingTrack with note_on, note_off, and program_change events.

    Note the previous behaviour excluded every guitar track regardless of what
    the player was playing, so a bass player got no guitars in their backing.
    """
    if instrument is None:
        from pickhero import instruments as _inst
        instrument = _inst.GUITAR

    if str(path).lower().endswith((".mid", ".midi")):
        from pickhero.tabs.midi_import import extract_midi_backing
        return extract_midi_backing(path, exclude_track_indices, instrument)

    if _is_gp7_file(path):
        return _extract_gp7_backing_track(path, exclude_track_indices, instrument)

    song = _parse_legacy(path)
    tempo_map = _build_tempo_map(song)

    if exclude_track_indices is None:
        exclude_track_indices = {select_track_index(list_tracks(path), instrument)}

    events: list[MidiEvent] = []

    for i, track in enumerate(song.tracks):
        if i in exclude_track_indices:
            continue

        # Determine MIDI channel: percussion on channel 9, others use GP channel
        if track.channel.isPercussionChannel:
            channel = 9
        else:
            channel = (track.channel.channel - 1) % 16  # GP is 1-indexed

        # Program change at t=0 for non-percussion tracks
        if not track.channel.isPercussionChannel:
            events.append(MidiEvent(
                timestamp_ms=0.0,
                channel=channel,
                event_type=PROGRAM_CHANGE,
                data1=track.channel.instrument,
                data2=0,
            ))

        # Extract note events
        for measure in track.measures:
            for voice in measure.voices:
                for beat in voice.beats:
                    timestamp_ms = tempo_map.tick_to_ms(beat.start)
                    duration_ms = tempo_map.duration_ticks_to_ms(
                        beat.duration.time, beat.start,
                    )

                    for note in beat.notes:
                        if note.type.value not in (1, 3):
                            continue

                        velocity = note.velocity if note.velocity > 0 else 80
                        midi_note = note.realValue + _capo(track)

                        events.append(MidiEvent(
                            timestamp_ms=timestamp_ms,
                            channel=channel,
                            event_type=NOTE_ON,
                            data1=midi_note,
                            data2=velocity,
                        ))
                        events.append(MidiEvent(
                            timestamp_ms=timestamp_ms + duration_ms,
                            channel=channel,
                            event_type=NOTE_OFF,
                            data1=midi_note,
                            data2=0,
                        ))

    return BackingTrack(events)
