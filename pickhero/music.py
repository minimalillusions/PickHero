"""Short pieces, each in two playable versions: originals and known ones.

These compositions use explicit pitches and rests, not detector test data.
Files have separate stable identities; only unchanged music inherits history.

The known pieces are all in the public domain -- Beethoven, Grieg, a
traditional hymn and the boogie figure, which belongs to nobody -- so that
a beginner can say what the player is playing. The melodies are written from
published scores, not from memory, and transposed into first position.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import io
import json
from pathlib import Path
import tempfile

import guitarpro as gp
from guitarpro import models as m

from pickhero.exercises import Exercise, Step, _build_song, _music_signature, FIRST_TICK
from pickhero.i18n import t

MUSIC_DIRNAME = "Musikstuecke"
VERSIONS = ("start", "groove")


@dataclass(frozen=True)
class Piece:
    slug: str
    bpm: int
    style: str
    section_bars: tuple[int, ...] = ()
    # Somebody else's music. The library prints the artist under every
    # title, and Beethoven is not a PickHero practice piece.
    known: bool = False

    @property
    def title(self):
        return t(f"music.{self.slug}.title")

    def filename(self, version):
        if version not in VERSIONS:
            raise ValueError("Unknown music version")
        return f"ph_{self.slug}_{version}.gp5"


PIECES = (Piece("night_drive", 72, "rock"), Piece("sunday_blues", 72, "blues"),
          Piece("little_journey", 68, "melody"),
          Piece("evening_walk", 80, "groove", (0, 4, 8)),
          Piece("ode_to_joy", 80, "melody", known=True),
          Piece("saints", 96, "melody", known=True),
          Piece("boogie", 84, "blues", known=True),
          Piece("mountain_king", 76, "melody", known=True))


def music_for_path(path):
    path = Path(path)
    if path.parent.name != MUSIC_DIRNAME:
        return None
    return next(((piece, version) for piece in PIECES for version in VERSIONS
                 if path.name == piece.filename(version)), None)


def _patterns(slug, version):
    # (semitones above the lowest open string, written duration).
    # None is a rest. Every row is a complete 4/4 bar.
    if slug == "evening_walk":
        if version == "start":
            hook = [
                [(0, 4), (0, 4), (3, 4), (5, 4)],
                [(5, 4), (5, 4), (0, 4), (3, 4)],
                [(0, 4), (3, 4), (5, 4), (7, 4)],
                [(5, 4), (3, 4), (0, 4), (None, 4)],
            ]
            ending = [
                [(0, 4), (5, 4), (0, 4), (5, 4)],
                [(5, 4), (5, 4), (3, 4), (0, 4)],
                [(7, 4), (5, 4), (3, 4), (0, 4)],
                [(0, 2), (None, 2)],
            ]
        else:
            hook = [
                [(0, 4), (0, 8), (0, 8), (3, 4), (5, 4)],
                [(5, 4), (5, 8), (0, 8), (5, 4), (3, 4)],
                [(0, 4), (3, 4), (5, 8), (0, 8), (7, 4)],
                [(5, 4), (3, 4), (0, 4), (None, 4)],
            ]
            ending = [
                [(0, 8), (5, 8), (0, 8), (5, 8), (3, 4), (0, 4)],
                [(5, 4), (5, 8), (5, 8), (3, 4), (0, 4)],
                [(7, 4), (5, 4), (3, 4), (0, 4)],
                [(0, 2), (None, 2)],
            ]
        # The quieter middle repeats the same melody and fingering, so the
        # player changes touch without having to learn a second phrase.
        return [list(bar) for bar in hook] + [list(bar) for bar in hook] + ending
    if slug == "night_drive":
        if version == "start":
            return [
                [(0, 4), (0, 4), (3, 2)], [(0, 2), (None, 4), (3, 4)],
                [(0, 4), (3, 4), (0, 2)], [(3, 4), (0, 4), (0, 2)],
                [(0, 4), (0, 4), (3, 2)], [(0, 2), (None, 4), (3, 4)],
                [(3, 4), (0, 4), (3, 4), (0, 4)], [(0, 2), (None, 2)],
            ]
        return [
            [(0, 4), (0, 8), (3, 8), (5, 4), (3, 4)],
            [(0, 2), (None, 4), (3, 4)],
            [(0, 4), (3, 4), (5, 4), (3, 4)], [(3, 4), (0, 4), (0, 2)],
            [(5, 4), (5, 8), (7, 8), (5, 4), (3, 4)],
            [(0, 2), (None, 4), (3, 4)],
            [(7, 4), (5, 4), (3, 4), (0, 4)], [(0, 2), (None, 2)],
        ]
    if slug == "sunday_blues":
        roots = (0, 0, 0, 0, 5, 5, 0, 0, 7, 5, 0, 0)
        if version == "start":
            return [[(root, 2), (root, 4), (None, 4)] for root in roots[:-1]] + [[(0, 1)]]
        bars = [[(root, 4), (root, 4), (root+7, 4), (root, 4)] for root in roots]
        bars[3] = [(0, 4), (3, 4), (5, 4), (0, 4)]
        bars[9] = [(5, 4), (7, 4), (5, 4), (3, 4)]
        bars[-1] = [(7, 4), (5, 4), (3, 4), (0, 4)]
        return bars
    if slug == "little_journey":
        if version == "start":
            return [[(0, 2), (3, 2)], [(5, 2), (3, 2)], [(7, 2), (5, 2)], [(3, 2), (0, 2)],
                    [(0, 2), (3, 2)], [(5, 2), (7, 2)], [(5, 2), (3, 2)], [(0, 1)]]
        return [[(0, 4), (3, 4), (5, 2)], [(7, 2), (5, 4), (3, 4)],
                [(5, 4), (7, 4), (10, 2)], [(12, 2), (10, 4), (7, 4)],
                [(10, 4), (7, 4), (5, 2)], [(7, 4), (5, 4), (3, 2)],
                [(5, 4), (3, 4), (0, 2)], [(0, 2), (None, 2)]]
    # The known pieces. Sources: Ode to Joy from the LilyPond score in
    # Wikipedia's article, When the Saints from the ABC score in the German
    # article, the Mountain King theme from the first score excerpt in
    # Wikipedia's article on it.
    #
    # This table has no dotted notes, so a dotted note is written as the
    # plain note followed by a rest of the dot's length. The strike lands
    # exactly where the score puts it -- which is what gets scored -- and the
    # string rings on through the rest anyway.
    if slug == "ode_to_joy":
        # C major in first position: G1=3 C2=8 D2=10 E2=12 F2=13 G2=15.
        rise = [(12, 4), (12, 4), (13, 4), (15, 4)]     # E E F G
        fall = [(15, 4), (13, 4), (12, 4), (10, 4)]     # G F E D
        climb = [(8, 4), (8, 4), (10, 4), (12, 4)]     # C C D E
        if version == "start":
            # The two lines everybody knows, the dotted cadences evened out
            # to quarter, quarter, half.
            tune = [rise, fall, climb, [(12, 4), (10, 4), (10, 2)],
                    rise, fall, climb, [(10, 4), (8, 4), (8, 2)]]
            return [list(bar) for bar in tune]
        half_close = [(12, 4), (None, 8), (10, 8), (10, 2)]   # E. D D
        full_close = [(10, 4), (None, 8), (8, 8), (8, 2)]     # D. C C
        tune = [rise, fall, climb, half_close, rise, fall, climb, full_close,
                [(10, 4), (10, 4), (12, 4), (8, 4)],             # D D E C
                [(10, 4), (12, 8), (13, 8), (12, 4), (8, 4)],    # D E F E C
                [(10, 4), (12, 8), (13, 8), (12, 4), (10, 4)],   # D E F E D
                # C D G and the E that comes a beat early. The score ties it
                # over the bar line; here it is struck on four and held
                # through the rest that opens the next bar.
                [(8, 4), (10, 4), (3, 4), (12, 4)],
                [(None, 4), (12, 4), (13, 4), (15, 4)],
                fall, climb, full_close]
        return [list(bar) for bar in tune]
    if slug == "saints":
        # C major: C2=8 D2=10 E2=12 F2=13 G2=15. The source is in 2/4.
        if version == "start":
            # Every value doubled, one source bar to a bar: the hymn at half
            # speed, so the three rising calls stand apart.
            call = [(None, 4), (8, 4), (12, 4), (13, 4)]    # - C E F
            tune = [call, [(15, 1)], call, [(15, 1)], call,
                    [(15, 2), (12, 2)], [(8, 2), (12, 2)], [(10, 1)],
                    [(None, 4), (12, 4), (12, 4), (10, 4)], [(8, 1)],
                    [(12, 2), (15, 2)], [(15, 4), (13, 2), (None, 4)],
                    [(None, 2), (12, 4), (13, 4)], [(15, 2), (12, 2)],
                    [(8, 2), (10, 2)], [(8, 1)]]
            return [list(bar) for bar in tune]
        # The source's own values, two source bars to a bar.
        call = [(None, 8), (8, 8), (12, 8), (13, 8)]
        tune = [call + [(15, 2)], call + [(15, 2)],
                call + [(15, 4), (12, 4)], [(8, 4), (12, 4), (10, 2)],
                [(None, 8), (12, 8), (12, 8), (10, 8), (8, 2)],
                [(12, 4), (15, 4), (15, 8), (13, 4), (None, 8)],
                [(None, 4), (12, 8), (13, 8), (15, 4), (12, 4)],
                [(8, 4), (10, 4), (8, 2)]]
        # Twice through, as a band plays it.
        return [list(bar) for bar in tune + tune]
    if slug == "boogie":
        # Twelve bars in A on the open strings: A1=5 is I, D2=10 is IV,
        # E2=12 is V, with E1=0 F#1=2 A1=5 B1=7 C#2=9 beneath them.
        form = ("I", "I", "I", "I", "IV", "IV", "I", "I", "V", "IV", "I", "end")
        if version == "start":
            # The root and the fifth below it, rocking back and forth.
            shapes = {"I": [(5, 4), (0, 4), (5, 4), (0, 4)],
                      "IV": [(10, 4), (5, 4), (10, 4), (5, 4)],
                      "V": [(12, 4), (7, 4), (12, 4), (7, 4)],
                      "end": [(5, 1)]}
        else:
            # Root, fifth, sixth, fifth: the figure under countless blues and
            # rock'n'roll records. The sixth over E is the one note past the
            # third fret, and it comes once, in bar nine.
            shapes = {"I": [(5, 4), (0, 4), (2, 4), (0, 4)],
                      "IV": [(10, 4), (5, 4), (7, 4), (5, 4)],
                      "V": [(12, 4), (7, 4), (9, 4), (7, 4)],
                      "end": [(5, 4), (0, 4), (5, 2)]}
        return [list(shapes[chord]) for chord in form]
    if slug == "mountain_king":
        # E minor rather than Grieg's B minor, so the theme starts on the
        # lowest open string the way it starts in the lowest instruments of
        # the orchestra: E1=0 F1=1 F#1=2 G1=3 A1=5 A#1=6 B1=7 D2=10 E2=12.
        if version == "start":
            # Every value doubled: the theme at the pace it creeps in.
            tune = [[(0, 4), (2, 4), (3, 4), (5, 4)], [(7, 4), (3, 4), (7, 2)],
                    [(6, 4), (2, 4), (6, 2)], [(5, 4), (1, 4), (5, 2)],
                    [(0, 4), (2, 4), (3, 4), (5, 4)], [(7, 4), (3, 4), (7, 4), (12, 4)],
                    [(10, 4), (7, 4), (3, 4), (7, 4)], [(10, 2), (None, 2)]]
            return [list(bar) for bar in tune]
        theme = [[(0, 8), (2, 8), (3, 8), (5, 8), (7, 8), (3, 8), (7, 4)],
                 [(6, 8), (2, 8), (6, 4), (5, 8), (1, 8), (5, 4)],
                 [(0, 8), (2, 8), (3, 8), (5, 8), (7, 8), (3, 8), (7, 8), (12, 8)],
                 [(10, 8), (7, 8), (3, 8), (7, 8), (10, 4), (None, 4)]]
        # Twice through. Grieg repeats it louder and faster until the hall
        # comes down; two rounds at one steady tempo is the practisable part.
        return [list(bar) for bar in theme + theme]
    raise ValueError("Unknown original piece")


def build_music(piece, version, instrument):
    if piece not in PIECES or version not in VERSIONS:
        raise ValueError("Unknown original piece/version")
    bars = []
    for pattern in _patterns(piece.slug, version):
        bar = []
        for interval, duration in pattern:
            if interval is None:
                bar.append(Step(None, 0, duration))
                continue
            pitch = instrument.midi_min + interval
            positions = [(pitch-instrument.tuning[s], s) for s in instrument.strings_low_to_high()
                         if 0 <= pitch-instrument.tuning[s] <= instrument.max_fret]
            if not positions:
                raise ValueError("This tuning cannot play the original piece")
            fret, string = min(positions)
            bar.append(Step(string, fret, duration))
        bars.append(bar)
    return Exercise(1, piece.slug, piece.bpm, bars)


def _drums(bar):
    # A quiet straight beat, with a final downbeat instead of an endless fill.
    for eighth in range(8):
        notes = [(42, 42)]
        if eighth in (0, 4):
            notes.append((36, 58))
        if eighth in (2, 6):
            notes.append((38, 54))
        if bar == "last":
            notes = [(36, 58), (42, 42)] if eighth == 0 else []
        yield eighth, notes


def music_song(piece, version, instrument):
    exercise = build_music(piece, version, instrument)
    song = _build_song(exercise, instrument, title=piece.title)
    song.title = t("music.version_title", title=piece.title, version=t(f"music.version.{version}"))
    if piece.known:
        song.artist, song.album = t(f"music.{piece.slug}.composer"), t("music.album_known")
    else:
        song.artist, song.album = t("music.artist"), t("music.album")
    song.tracks[0].name = piece.title
    for bar in piece.section_bars:
        song.measureHeaders[bar].marker = m.Marker(title=t(f"music.{piece.slug}.section.{bar}"))
    if piece.slug == "evening_walk":
        for bar, measure in enumerate(song.tracks[0].measures):
            for beat in measure.voices[0].beats:
                for note in beat.notes:
                    note.velocity = 63 if 4 <= bar < 8 else 95
    drums = m.Track(song, 2)
    drums.name = t("music.drums")
    drums.channel = m.MidiChannel(channel=9, effectChannel=9, instrument=0)
    drums.strings = [m.GuitarString(number=number, value=0) for number in (1, 2, 3)]
    drums.measures = []
    for index, header in enumerate(song.measureHeaders):
        measure = m.Measure(drums, header)
        voice = m.Voice(measure)
        voice.beats = []
        for eighth, notes in _drums("last" if index == len(song.measureHeaders)-1 else index):
            beat = m.Beat(voice)
            beat.start = FIRST_TICK + index*3840 + eighth*480
            beat.duration = m.Duration(value=8)
            beat.status = m.BeatStatus.normal if notes else m.BeatStatus.rest
            beat.notes = []
            for string, (pitch, velocity) in enumerate(notes, 1):
                note = m.Note(beat)
                note.string, note.value, note.velocity = string, pitch, velocity
                note.type = m.NoteType.normal
                beat.notes.append(note)
            voice.beats.append(beat)
        measure.voices = [voice, m.Voice(measure)]
        drums.measures.append(measure)
    song.tracks.append(drums)
    return song


def _atomic_bytes(path, data):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".ph-", suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(data)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def ensure_music(instrument, songs_dir, progress=None):
    """Create only owned files, preserving user edits and equivalent history."""
    from pickhero.library import content_identity
    folder = Path(songs_dir)/MUSIC_DIRNAME
    folder.mkdir(parents=True, exist_ok=True)
    marker = folder/".pickhero-music.json"
    try:
        owned = json.loads(marker.read_text(encoding="utf-8"))
        if not isinstance(owned, dict):
            owned = {}
    except (OSError, ValueError):
        owned = {}
    changed = False
    for piece in PIECES:
        for version in VERSIONS:
            path = folder/piece.filename(version)
            stream = io.BytesIO()
            gp.write(music_song(piece, version, instrument), stream, version=(5, 1, 0))
            data = stream.getvalue()
            old = path.read_bytes() if path.exists() else None
            digest = hashlib.sha256(data).hexdigest()
            if old == data:
                owned[path.name] = digest
                continue
            if old is not None and hashlib.sha256(old).hexdigest() != owned.get(path.name):
                raise ValueError("Generated music file was modified; preserve the user's version")
            previous = None
            if old is not None and progress is not None:
                previous = content_identity(path), _music_signature(path)
                progress.migrate_content_identity(path, instrument.key, 0, previous[0])
            _atomic_bytes(path, data)
            owned[path.name] = digest
            _atomic_bytes(marker, json.dumps(owned, sort_keys=True).encode("utf-8"))
            changed = True
            if previous and previous[1] is not None and previous[1] == _music_signature(path):
                progress.migrate_content_replacement(previous[0], content_identity(path), instrument.key)
    return changed


def music_practice(path, instrument):
    from pickhero.curriculum import LessonPractice, PracticeStep
    found = music_for_path(path)
    if found is None:
        raise ValueError("Unknown music file")
    piece, version = found
    exercise = build_music(piece, version, instrument)
    return LessonPractice(Path(path), PracticeStep("full", 0, len(exercise.bars), 1., False, loop=False))


def next_music_practice(path, instrument, practice=None):
    found = music_for_path(path)
    if found is None:
        return None
    current = music_practice(path, instrument)
    if practice is not None and practice != current:
        return None
    piece, version = found
    if version == "start":
        following = piece, "groove"
    elif PIECES.index(piece)+1 < len(PIECES):
        following = PIECES[PIECES.index(piece)+1], "start"
    else:
        return None
    return music_practice(Path(path).with_name(following[0].filename(following[1])), instrument)


def build_music_demo(piece, version, instrument):
    from pickhero.audio.midi_playback import BackingTrack, MidiEvent, NOTE_ON, NOTE_OFF
    from pickhero.curriculum import build_demo, LessonDemo
    exercise = build_music(piece, version, instrument)
    demo = build_demo(exercise, instrument, bars=4)
    events = demo.track.events
    quarter = 60000/piece.bpm
    for bar in range(demo.bars):
        for eighth, notes in _drums(bar):
            at = demo.count_in_ms + (bar*4+eighth/2)*quarter
            for pitch, velocity in notes:
                events.extend((MidiEvent(at, 9, NOTE_ON, pitch, velocity),
                               MidiEvent(at+quarter/2, 9, NOTE_OFF, pitch)))
    return LessonDemo(BackingTrack(events), demo.duration_ms, demo.count_in_ms, demo.bars)
