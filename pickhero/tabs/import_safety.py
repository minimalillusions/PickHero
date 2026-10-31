"""Resource limits shared by local score importers.

Limits deliberately allow long, densely orchestrated pieces. They apply before
decompression/tree construction/reference expansion, not only after allocation.
"""

from __future__ import annotations

import io
import math
from pathlib import Path
import struct
import xml.etree.ElementTree as ET
import zipfile

from defusedxml.ElementTree import DefusedXMLParser

from pickhero.bounded_io import FileTooLargeError, read_bounded
from pickhero.validation import text

MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_XML_BYTES = 16 * 1024 * 1024
MAX_XML_ELEMENTS = 250_000
MAX_XML_DEPTH = 64
MAX_ZIP_MEMBERS = 1024
MAX_TRACKS = 128
MAX_MEASURES = 20_000
MAX_TRACK_MEASURES = 200_000
MAX_NOTES = 200_000
MAX_WORK = 2_000_000
MAX_DURATION_MS = 24 * 60 * 60 * 1000
MIN_TEMPO = 1.0
MAX_TEMPO = 1000.0


class ImportLimitError(ValueError):
    """A score is invalid or exceeds a documented import resource limit."""


def finite_range(value, minimum: float, maximum: float, name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ImportLimitError(f"Invalid {name}") from exc
    if not math.isfinite(number) or not minimum <= number <= maximum:
        raise ImportLimitError(f"{name} must be finite and between {minimum} and {maximum}")
    return number


def tempo(value) -> float:
    return finite_range(value, MIN_TEMPO, MAX_TEMPO, "tempo")


def bounded_file(path: str | Path) -> bytes:
    path = Path(path)
    try:
        with path.open("rb") as stream:
            return read_bounded(stream, MAX_FILE_BYTES)
    except FileTooLargeError as exc:
        raise ImportLimitError("Score exceeds the 32 MiB file limit") from exc


class _TreeBuilder(ET.TreeBuilder):
    def __init__(self):
        super().__init__()
        self.depth = self.count = 0

    def start(self, tag, attrs):
        self.depth += 1
        self.count += 1
        if self.depth > MAX_XML_DEPTH or self.count > MAX_XML_ELEMENTS:
            raise ImportLimitError("Score XML exceeds depth/element limit")
        return super().start(tag, attrs)

    def end(self, tag):
        element = super().end(tag)
        self.depth -= 1
        return element


def read_gpif(path: str | Path):
    """Read a bounded ZIP/XML score with all DTD/entity processing forbidden."""
    data = bounded_file(path)
    # ZipFile constructs every ZipInfo before exposing infolist(). Bound the
    # central directory first so a ZIP of empty members cannot exhaust memory.
    eocd = data.rfind(b"PK\x05\x06", max(0, len(data) - 65_557))
    if eocd < 0 or eocd + 22 > len(data):
        raise ImportLimitError("Invalid ZIP end record")
    _, disk, directory_disk, disk_count, count, directory_size, _, _ = struct.unpack_from("<4s4H2LH", data, eocd)
    if disk or directory_disk or count != disk_count:
        raise ImportLimitError("Multi-volume score ZIP is not supported")
    if count > MAX_ZIP_MEMBERS or directory_size > MAX_ZIP_MEMBERS * 2048:
        raise ImportLimitError("Score ZIP directory exceeds import limit")
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        members = archive.infolist()
        if len(members) > MAX_ZIP_MEMBERS:
            raise ImportLimitError("Too many ZIP members")
        scores = [m for m in members if m.filename == "Content/score.gpif"]
        if len(scores) != 1:
            raise ImportLimitError("Archive must contain exactly one Content/score.gpif")
        member = scores[0]
        if member.file_size > MAX_XML_BYTES:
            raise ImportLimitError("Score XML exceeds the 16 MiB limit")
        parser = DefusedXMLParser(target=_TreeBuilder(), forbid_dtd=True,
                                 forbid_entities=True, forbid_external=True)
        with archive.open(member) as stream:
            total = 0
            while chunk := stream.read(64 * 1024):
                total += len(chunk)
                if total > MAX_XML_BYTES:
                    raise ImportLimitError("Score XML exceeds the 16 MiB limit")
                parser.feed(chunk)
        root = parser.close()
    _validate_gpif(root)
    return root


def signature(numerator, denominator) -> tuple[int, int]:
    numerator = finite_range(numerator, 1, 64, "time signature numerator")
    denominator = finite_range(denominator, 1, 128, "time signature denominator")
    if not numerator.is_integer() or not denominator.is_integer():
        raise ImportLimitError("Time signature values must be integers")
    num, den = int(numerator), int(denominator)
    if den & (den - 1):
        raise ImportLimitError("Time signature denominator must be a power of two")
    return num, den


def _validate_gpif(root) -> None:
    tracks = root.findall("./Tracks/Track")
    master_bars = root.findall("./MasterBars/MasterBar")
    if len(tracks) > MAX_TRACKS or len(master_bars) > MAX_MEASURES:
        raise ImportLimitError("Too many tracks or measures")
    for element_path in ("Score/Title", "Score/Artist", "Score/Album", "Tracks/Track/Name"):
        if any(not text(element.text or "") for element in root.findall(element_path)):
            raise ImportLimitError("Invalid or oversized score metadata text")
    for track in tracks:
        for prop in track.findall(".//Property"):
            if prop.get("name") == "Tuning":
                pitches = prop.findtext("Pitches", "").split()
                if len(pitches) > 12:
                    raise ImportLimitError("At most 12 strings are supported")
                for pitch in pitches:
                    finite_range(pitch, 0, 127, "tuning pitch")
    for auto in root.findall(".//MasterTrack/Automations/Automation"):
        if auto.findtext("Type") == "Tempo":
            tempo(auto.findtext("Value", "120").split()[0])
            finite_range(auto.findtext("Bar", "0"), 0, MAX_MEASURES, "tempo bar")
            finite_range(auto.findtext("Position", "0"), 0, 1, "tempo position")
    for rhythm in root.findall("./Rhythms/Rhythm"):
        tuplet = rhythm.find("PrimaryTuplet")
        if tuplet is not None:
            finite_range(tuplet.get("num", "1"), 1, 128, "tuplet numerator")
            finite_range(tuplet.get("den", "1"), 1, 128, "tuplet denominator")
        dot = rhythm.find("AugmentationDot")
        if dot is not None:
            finite_range(dot.get("count", "1"), 0, 4, "augmentation dots")
    for bar in master_bars:
        parts = bar.findtext("Time", "4/4").split("/")
        if len(parts) != 2:
            raise ImportLimitError("Invalid time signature")
        signature(*parts)

    # Count the expanded graph bottom-up. Repeated references can multiply a
    # tiny XML document into millions of notes; count them before any walk.
    costs: dict[str, tuple[int, int]] = {}
    levels = (("Beats/Beat", "Notes"), ("Voices/Voice", "Beats"),
              ("Bars/Bar", "Voices"), ("MasterBars/MasterBar", "Bars"))
    for level, (element_path, refs_tag) in enumerate(levels):
        next_costs = {}
        total_notes = total_work = 0
        for element in root.findall(element_path):
            refs = element.findtext(refs_tag, "").split()
            if level == 0:
                notes, work = len(refs), 1 + len(refs)
            else:
                notes = sum(costs.get(ref, (0, 0))[0] for ref in refs)
                work = 1 + sum(costs.get(ref, (0, 0))[1] for ref in refs)
            total_notes += notes
            total_work += work
            if max(notes, total_notes) > MAX_NOTES or max(work, total_work) > MAX_WORK:
                raise ImportLimitError("Score reference expansion exceeds import limit")
            key = element.get("id", "")
            if key in next_costs and level < 3:
                raise ImportLimitError("Duplicate score identifier")
            next_costs[key] = notes, work
        costs = next_costs


class BoundedGPStream(io.BytesIO):
    """Stop legacy parsers at EOF and bound work even for forged counters.

    pyguitarpro otherwise supplies defaults for certain reads past EOF, which
    can turn an attacker-controlled count into a long loop of empty objects.
    """
    def __init__(self, data: bytes):
        super().__init__(data)
        self.reads = 0
        self.eof_reads = 0

    def read(self, size=-1):
        self.reads += 1
        if size < 0 or size > MAX_FILE_BYTES or self.reads > MAX_WORK:
            raise ImportLimitError("Legacy score read exceeds import limit")
        result = super().read(size)
        if len(result) != size:
            self.eof_reads += 1
            # GP5 permits its final lineBreak byte to be omitted. A single
            # empty read keeps that compatibility; repeated defaults cannot
            # turn forged counters into an unbounded parse.
            if size != 1 or self.eof_reads > 1:
                raise ImportLimitError("Truncated legacy score")
        return result


def parse_legacy_score(path):
    """Guard pyguitarpro's count-driven allocations before they are performed.

    Hooks are attached only to this parser instance, so parallel imports never
    mutate global parser classes. `_open` is pyguitarpro's version dispatcher;
    the actual GP3/4/5 decoding remains in the pinned library.
    """
    from guitarpro.io import _open

    with BoundedGPStream(bounded_file(path)) as stream:
        parser, _ = _open(None, stream, "rb", encoding="cp1252")
        headers = parser.readMeasureHeaders
        tracks = parser.readTracks
        read_note = parser.readNote
        note_count = 0

        def limited_headers(song, count, *args):
            if not 0 <= count <= MAX_MEASURES:
                raise ImportLimitError("Too many legacy measures")
            return headers(song, count, *args)

        def limited_tracks(song, count, *args):
            if not 0 <= count <= MAX_TRACKS or count * len(song.measureHeaders) > MAX_TRACK_MEASURES:
                raise ImportLimitError("Too many legacy tracks or track-measures")
            return tracks(song, count, *args)

        def limited_note(*args, **kwargs):
            nonlocal note_count
            note_count += 1
            if note_count > MAX_NOTES:
                raise ImportLimitError("Too many legacy notes")
            return read_note(*args, **kwargs)

        def limited_voice(start, voice):
            count = parser.readI32()
            if not 0 <= count <= MAX_WORK:
                raise ImportLimitError("Too many legacy beats")
            for beat in range(count):
                parser._currentBeatNumber = beat + 1
                start += parser.readBeat(start, voice)
            parser._currentBeatNumber = None

        parser.readMeasureHeaders = limited_headers
        parser.readTracks = limited_tracks
        parser.readNote = limited_note
        parser.readVoice = limited_voice
        song = parser.readSong()
        labels = [song.title, song.artist, song.album] + [track.name for track in song.tracks]
        if any(not text(label or "") for label in labels):
            raise ImportLimitError("Invalid or oversized legacy score metadata text")
        return song
