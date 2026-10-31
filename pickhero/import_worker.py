"""Isolated score work. No device access, user settings or progress writes.

Only the parent launches this entry point, with a private request directory.
The result is JSON, never executable pickle data. Existing parser limits still
apply inside an additional, cancellable operating-system process boundary.
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict
from pathlib import Path

MAX_RESULT_BYTES = 64 * 1024 * 1024
MAX_PACKET_BYTES = 256 * 1024
PACKET_ROWS = 128


class DecodeCancelled(Exception):
    """The owner no longer needs this bounded result."""


def write_result(path, value):
    """Write bounded JSON packets; never require a whole-result JSON parse.

    A single json.loads of a large score holds Python's interpreter lock even
    in a thread. Small packets keep that indivisible work below a frame.
    """
    value = dict(value)
    arrays = {}
    header = {"version": 2, "ok": True, "value": value, "kind": "value"}
    if "song" in value:
        song = value.pop("song")
        header.update(kind="song", metadata=song["metadata"])
        arrays = {name: song[name] for name in
                  ("notes", "measures", "beats", "tempo_changes", "backing")}
    else:
        for name in ("songs", "tracks"):
            if name in value:
                arrays[name] = value.pop(name)
    target = Path(path)
    temporary = target.with_suffix(".tmp")
    total = 0
    with temporary.open("wb") as stream:
        def packet(row):
            nonlocal total
            encoded = json.dumps(row, ensure_ascii=False, allow_nan=False,
                                 separators=(",", ":")).encode("utf-8") + b"\n"
            if len(encoded) > MAX_PACKET_BYTES:
                raise ValueError("Import packet exceeds 256 KiB")
            total += len(encoded)
            if total > MAX_RESULT_BYTES:
                raise ValueError("Import result exceeds 64 MiB")
            stream.write(encoded)

        packet(header)
        for name, rows in arrays.items():
            for offset in range(0, len(rows), PACKET_ROWS):
                chunk = rows[offset:offset + PACKET_ROWS]
                # Long track/song titles are legal within parser limits;
                # split those packets by bytes as well as by row count.
                pending = [chunk]
                while pending:
                    part = pending.pop()
                    encoded_size = len(json.dumps({"field": name, "rows": part},
                                       ensure_ascii=False).encode("utf-8")) + 1
                    if encoded_size > MAX_PACKET_BYTES and len(part) > 1:
                        half = len(part) // 2
                        pending.extend((part[half:], part[:half]))
                        continue
                    packet({"field": name, "rows": part})
        packet({"complete": True, "counts": {name: len(rows) for name, rows in arrays.items()}})
    temporary.replace(target)


def read_result(path, *, cancelled=lambda: False, report=None):
    """Decode packets off the UI thread and return (metadata, prepared song).

    Limits and timeline value validation still apply at this trust boundary.
    Cancellation is checked between every packet and every construction stage.
    """
    from pickhero.audio.midi_playback import BackingTrack, MidiEvent
    from pickhero.tabs.import_safety import (
        MAX_MEASURES,
        MAX_NOTES,
        MAX_TRACKS,
        MAX_WORK,
    )
    from pickhero.tabs.timeline import (
        BeatInfo,
        MeasureInfo,
        NoteEvent,
        SongMetadata,
        Timeline,
    )

    limits = {"notes": MAX_NOTES, "measures": MAX_MEASURES, "beats": MAX_WORK,
              "tempo_changes": MAX_WORK, "backing": MAX_WORK, "songs": 10000,
              "tracks": MAX_TRACKS}
    constructors = {"notes": NoteEvent, "measures": MeasureInfo,
                    "beats": BeatInfo, "backing": MidiEvent}
    rows = {}
    consumed = 0
    footer = None
    with Path(path).open("rb") as stream:
        def read_packet():
            nonlocal consumed
            if cancelled():
                raise DecodeCancelled()
            raw = stream.readline(MAX_PACKET_BYTES + 1)
            consumed += len(raw)
            if consumed > MAX_RESULT_BYTES or len(raw) > MAX_PACKET_BYTES:
                raise ValueError("Import transport exceeds its limits")
            if not raw or not raw.endswith(b"\n"):
                raise ValueError("Incomplete import result")
            packet = json.loads(raw)
            if not isinstance(packet, dict):
                raise ValueError("Malformed import packet")
            return packet

        header = read_packet()
        if (header.get("version") != 2 or header.get("ok") is not True
                or not isinstance(header.get("value"), dict)
                or header.get("kind") not in ("song", "value")):
            raise ValueError("Malformed import header")
        while True:
            packet = read_packet()
            if packet.get("complete") is True:
                footer = packet
                if stream.read(1):
                    raise ValueError("Trailing import data")
                break
            name, items = packet.get("field"), packet.get("rows")
            if (not isinstance(name, str) or name not in limits or not isinstance(items, list)
                    or not 1 <= len(items) <= PACKET_ROWS):
                raise ValueError("Malformed import rows")
            bucket = rows.setdefault(name, [])
            if len(bucket) + len(items) > limits[name]:
                raise ValueError("Import result exceeds score limits")
            constructor = constructors.get(name)
            if constructor is not None:
                bucket.extend(constructor(**item) for item in items)
            else:
                bucket.extend(items)
            if report is not None:
                report(consumed)
            # Give rendering and a simulated/real audio callback a scheduling
            # opportunity even while the filesystem serves cached data.
            time.sleep(0)
    counts = footer.get("counts")
    if not isinstance(counts, dict) or set(counts) - set(limits):
        raise ValueError("Malformed import counts")
    if any(type(count) is not int or count != len(rows.get(name, []))
           for name, count in counts.items()) or set(rows) - set(counts):
        raise ValueError("Incomplete import arrays")
    value = header["value"]
    if header["kind"] != "song":
        if set(counts) - {"songs", "tracks"}:
            raise ValueError("Unexpected import arrays")
        value.update({name: rows.get(name, []) for name in counts})
        return value, None
    expected = {"notes", "measures", "beats", "tempo_changes", "backing"}
    if set(counts) != expected:
        raise ValueError("Incomplete score")
    if cancelled():
        raise DecodeCancelled()
    metadata = dict(header["metadata"])
    metadata["tuning"] = {int(k): v for k, v in metadata["tuning"].items()}
    timeline = Timeline(rows.get("notes", []), SongMetadata(**metadata),
                        rows.get("measures", []), rows.get("beats", []), rows.get("tempo_changes", []))
    if cancelled():
        raise DecodeCancelled()
    backing = BackingTrack(rows.get("backing", []))
    return value, (timeline, backing)


def write_json(path, value):
    data = json.dumps(value, ensure_ascii=False, allow_nan=False,
                      separators=(",", ":")).encode("utf-8")
    if len(data) > MAX_RESULT_BYTES:
        raise ValueError("Import result exceeds 64 MiB")
    target = Path(path)
    temporary = target.with_suffix(".tmp")
    temporary.write_bytes(data)
    # Windows cannot replace a file while the parent's short status read is
    # holding it open. Retry that sharing violation, never abort the score.
    for attempt in range(20):
        try:
            temporary.replace(target)
            break
        except PermissionError:
            if attempt == 19:
                raise
            time.sleep(.005)


def encode_song(timeline, backing=None):
    return {"metadata": asdict(timeline.metadata),
            "notes": [asdict(note) for note in timeline.notes],
            "measures": [asdict(measure) for measure in timeline.measures],
            "beats": [asdict(beat) for beat in timeline._beats],
            "tempo_changes": list(zip(timeline._tempo_times, timeline._tempos)),
            "backing": [asdict(event) for event in backing.events] if backing else []}


def decode_song(raw):
    from pickhero.audio.midi_playback import BackingTrack, MidiEvent
    from pickhero.tabs.import_safety import MAX_MEASURES, MAX_NOTES, MAX_WORK
    from pickhero.tabs.timeline import (
        BeatInfo,
        MeasureInfo,
        NoteEvent,
        SongMetadata,
        Timeline,
    )
    if (len(raw["notes"]) > MAX_NOTES or len(raw["measures"]) > MAX_MEASURES
            or len(raw["beats"]) > MAX_WORK or len(raw["backing"]) > MAX_WORK):
        raise ValueError("Import result exceeds score limits")
    metadata = dict(raw["metadata"])
    metadata["tuning"] = {int(k): v for k, v in metadata["tuning"].items()}
    timeline = Timeline([NoteEvent(**row) for row in raw["notes"]],
                        SongMetadata(**metadata),
                        [MeasureInfo(**row) for row in raw["measures"]],
                        [BeatInfo(**row) for row in raw["beats"]],
                        raw["tempo_changes"])
    backing = BackingTrack([MidiEvent(**row) for row in raw["backing"]])
    return timeline, backing


def perform(request, report):
    from pickhero import instruments, library
    from pickhero.tabs.loader import (
        extract_backing_track,
        list_tracks,
        load_gp_file,
        select_track_index,
        track_matches_instrument,
    )
    instrument = instruments.PROFILES[request["instrument"]]
    path = Path(request["path"])
    operation = request["operation"]
    if operation == "scan":
        # The parent deliberately supplies both paths. Tests can therefore
        # isolate child writes too; no inherited monkeypatch is assumed.
        library.CACHE_FILE = Path(request["cache_file"])
        library.CONFIG_DIR = library.CACHE_FILE.parent
        return {"songs": [asdict(song) for song in library.scan(
            path, instrument, include_incompatible=True, quiet_cache=True,
            on_progress=lambda current, total, name: report(
                {"current": current, "total": total, "name": name}))]}
    content_id = library.content_identity(path)
    report({"current": 0, "total": 2, "name": path.name})
    if operation == "inspect":
        tracks = list_tracks(path)
        midi = path.suffix.lower() in {".mid", ".midi"}
        for track in tracks:
            track["compatible"] = (not track["is_percussion"] and track.get("notes", 1) > 0
                                   if midi else track_matches_instrument(track, instrument))
        return {"tracks": tracks, "selected": select_track_index(tracks, instrument),
                "content_id": content_id}
    if operation != "load":
        raise ValueError("Unknown score operation")
    if request.get("content_id") and request["content_id"] != content_id:
        raise ValueError("Score changed after track selection")
    timeline = load_gp_file(path, instrument=instrument, track_index=request.get("track_index"))
    report({"current": 1, "total": 2, "name": path.name})
    backing = None
    try:
        backing = extract_backing_track(path, exclude_track_indices={timeline.metadata.track_index})
    except (ValueError, OSError, KeyError, IndexError):
        pass  # Optional accompaniment never prevents practising the score.
    # Do not attach a new content identity to a score read while being edited.
    if library.content_identity(path) != content_id:
        raise ValueError("Score changed while importing")
    return {"song": encode_song(timeline, backing), "content_id": content_id}


def run(directory):
    directory = Path(directory)
    try:
        # Parent installs the Job Object before permitting any file parsing.
        deadline = time.monotonic() + 10
        while not (directory / "start").exists():
            if time.monotonic() > deadline:
                return 2
            time.sleep(.01)
        from pickhero.bounded_io import read_bounded
        with (directory / "request.json").open("rb") as stream:
            raw = read_bounded(stream, 65536)
        request = json.loads(raw)
        result = perform(request, lambda value: write_json(directory / "status.json", value))
        write_result(directory / "result.jsonl", result)
        return 0
    except Exception as exc:
        # Internal details stay out of user-facing UI and user logs.
        try:
            write_json(directory / "result.json", {"ok": False, "error": type(exc).__name__})
        except Exception:
            pass
        return 1
