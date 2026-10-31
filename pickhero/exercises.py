"""Generated practice exercises.

A beginner opening this app has no library, and the Guitar Pro files that
circulate are real songs: too fast, too dense, and written for someone who
already plays. The first thing a beginner needs is one note, slowly, over and
over, and then two notes.

So the app generates its own graded exercises as real .gp5 files. They are
built from the active instrument's own tuning, so the same set works for a
6-string guitar and a 4-string bass ukulele, and once written they are
ordinary songs that the loader, the matcher and the scoring already handle.

Nothing here is downloaded, so there is no licence question: these are scales
and open strings, which are facts rather than compositions.

Identity and display are deliberately separate here. An exercise's slug is a
fixed ASCII word that never changes, and the filename is built from it; the
title and the subtitle are looked up in the active language and only ever
shown. That split exists because the progress history is keyed on the file
stem: if the filename followed the translated title, switching the interface
to English would silently orphan every record the player has. See _LEGACY_STEMS for
the one-off migration that moved the set onto slugs.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import guitarpro as gp
from guitarpro import models as m

from pickhero.config import log_error
from pickhero.i18n import current_language, t

# pyguitarpro tick resolution: a quarter note is 960 ticks, and bar 1 starts
# at tick 960 rather than 0.
TICKS_PER_QUARTER = 960
FIRST_TICK = 960

# Duration.value follows the usual note-value convention.
WHOLE, HALF, QUARTER, EIGHTH, SIXTEENTH = 1, 2, 4, 8, 16
# (value, dotted) -> ticks. A dot adds half again, which is the only reason
# the dotted entries are not simply a second table: they are the same four
# values. Without them a figure can only march -- a dotted quarter followed
# by an eighth is most of what makes a bass line move rather than count, and
# until these were here it could not be written at all.
_TICKS = {
    (WHOLE, False): 3840, (WHOLE, True): 5760,
    (HALF, False): 1920, (HALF, True): 2880,
    (QUARTER, False): 960, (QUARTER, True): 1440,
    (EIGHTH, False): 480, (EIGHTH, True): 720,
    (SIXTEENTH, False): 240, (SIXTEENTH, True): 360,
}

GM_BASS = 33      # Electric Bass (finger)
GM_GUITAR = 25    # Acoustic Guitar (steel)


@dataclass
class Step:
    """One note in an exercise: which string, which fret, how long."""
    string: int | None  # None is a written rest, preserving its duration.
    fret: int
    value: int = QUARTER
    dotted: bool = False

    @property
    def ticks(self) -> int:
        return _TICKS[(self.value, self.dotted)]


# The slugs, in order. A slug is the exercise's identity: it names the file,
# it keys the progress history, and it is the stem of the two catalogue keys
# that carry the text. It is ASCII and it is never translated, so the same
# file is the same piece on a German and an English interface.
_SLUGS = (
    "open_string",
    "all_open",
    "quarters_one_string",
    "string_change",
    "open_up_down",
    "first_frets",
    "frets_back",
    "scale_one_octave",
    "eighths",
    "first_pattern",
)


@dataclass
class Exercise:
    """A generated practice piece."""
    order: int
    slug: str
    bpm: int
    bars: list[list[Step]] = field(default_factory=list)

    @property
    def title(self) -> str:
        """The name shown to the player, in the language the player has chosen."""
        return t(f"exercise.{self.slug}.title")

    @property
    def subtitle(self) -> str:
        """One line saying what to actually do, in their language."""
        return t(f"exercise.{self.slug}.subtitle")

    @property
    def filename(self) -> str:
        """The file this exercise is written to.

        Built from the slug, never from the title. The stem is the key the
        progress history uses, so it has to survive a change of language and
        a change of instrument without moving.
        """
        return f"{self.order:02d} {self.slug}.gp5"

    @property
    def note_count(self) -> int:
        return sum(step.string is not None for bar in self.bars for step in bar)


def _repeat(bar: list[Step], times: int) -> list[list[Step]]:
    return [list(bar) for _ in range(times)]


def _major_scale_steps(instrument, low_to_high: list[int]) -> list[Step]:
    """Keep a major scale near the nut, using only playable positions.

    In standard tuning this is 0/2/4, 0/2/4, 1/2 on the lowest three
    strings. The old fixed two-string split reached fret seven and could
    even produce negative frets when the strings had a wider tuning gap.
    """
    root = instrument.tuning[low_to_high[0]]
    steps = []
    for interval in (0, 2, 4, 5, 7, 9, 11, 12):
        pitch = root + interval
        positions = [
            (pitch - instrument.tuning[string], order, string)
            for order, string in enumerate(low_to_high)
            if 0 <= pitch - instrument.tuning[string] <= instrument.max_fret
        ]
        if not positions:
            raise ValueError(f"The major scale note {pitch} has no playable position")
        fret, _, string = min(positions)
        steps.append(Step(string, fret, QUARTER))
    return steps


def build_exercises(instrument) -> list[Exercise]:
    """Build the graded set for an instrument, easiest first.

    `instrument` is a pickhero.instruments.Instrument. Only its tuning and
    string count are used, so this works for any stringed instrument.
    """
    low_to_high = instrument.strings_low_to_high()   # thickest first
    lowest = low_to_high[0]
    second = low_to_high[1] if len(low_to_high) > 1 else lowest
    out: list[Exercise] = []

    # 1. One string, one note, as slow as the app will play it. The point is
    #    to get a clean sound and see the app react, nothing else.
    out.append(Exercise(
        1, "open_string",
        60, _repeat([Step(lowest, 0, WHOLE)], 8),
    ))

    # 2. Every open string in turn, still one note per bar.
    bars = []
    for _ in range(2):
        for s in low_to_high:
            bars.append([Step(s, 0, WHOLE)])
    out.append(Exercise(
        2, "all_open",
        60, bars,
    ))

    # 3. Quarter notes on one string: the first rhythm.
    out.append(Exercise(
        3, "quarters_one_string",
        60, _repeat([Step(lowest, 0, QUARTER)] * 4, 8),
    ))

    # 4. Changing strings, which is the first real coordination problem.
    out.append(Exercise(
        4, "string_change",
        60, _repeat([Step(lowest, 0, HALF), Step(second, 0, HALF)], 8),
    ))

    # 5. All open strings as quarter notes, up and down.
    seq = [Step(s, 0, QUARTER) for s in low_to_high]
    down = list(reversed(seq))
    bars = []
    pool = (seq + down) * 4
    for i in range(0, len(pool) - 3, 4):
        bars.append(pool[i:i + 4])
    out.append(Exercise(
        5, "open_up_down",
        60, bars[:8],
    ))

    # 6. The left hand arrives: frets 0 to 3 on the lowest string.
    out.append(Exercise(
        6, "first_frets",
        60, _repeat([Step(lowest, f, QUARTER) for f in (0, 1, 2, 3)], 8),
    ))

    # 7. Same, descending, so the hand learns both directions.
    out.append(Exercise(
        7, "frets_back",
        66, _repeat([Step(lowest, f, QUARTER) for f in (3, 2, 1, 0)], 8),
    ))

    # 8. The first major scale stays near the familiar frets. Pitch, rhythm
    #    and length are unchanged; only the written fingering is gentler.
    steps = _major_scale_steps(instrument, low_to_high)
    bars = [steps[i:i + 4] for i in range(0, len(steps), 4)]
    out.append(Exercise(
        8, "scale_one_octave",
        66, (bars + [list(reversed(b)) for b in reversed(bars)]) * 2,
    ))

    # 9. Eighth notes: the first time the right hand has to keep a pulse.
    out.append(Exercise(
        9, "eighths",
        60, _repeat([Step(lowest, 0, EIGHTH)] * 8, 8),
    ))

    # 10. Everything so far in one bar: two strings, a fretted note and a pair
    #     of eighths. It is the last of the set and has to be the hardest of
    #     them, or the browser sorts it back among the first ones and the
    #     progression the numbering promises is not there.
    pattern = [
        Step(lowest, 0, QUARTER), Step(lowest, 2, EIGHTH), Step(lowest, 0, EIGHTH),
        Step(second, 0, QUARTER), Step(second, 2, EIGHTH), Step(lowest, 0, EIGHTH),
    ]
    out.append(Exercise(
        10, "first_pattern",
        72, _repeat(pattern, 8),
    ))

    return out


def _build_song(ex: Exercise, instrument, *, title: str | None = None) -> gp.models.Song:
    """Turn an Exercise into a pyguitarpro Song."""
    song = gp.models.Song()
    # The text is looked up once, here, and written into the file. Everything
    # downstream -- the browser, the sidebar, the header while the player plays --
    # reads it back out of the file and never sees a key.
    title = ex.title if title is None else title
    # The number belongs in the title, not only in the filename. The browser
    # sorts and shows the title from inside the file, so without it "02 Alle
    # leeren Saiten" sorts before "01 Eine leere Saite" and the graded order
    # the whole set depends on falls apart. The prefix is a sort key rather
    # than prose -- two digits and a space, no words -- so it is formatted
    # here and not in a catalogue where a translator could reorder it.
    song.title = f"{ex.order:02d} {title}"
    song.artist = t("exercise.artist", number=ex.order)
    song.album = t("exercise.album")
    song.tempo = ex.bpm
    song.tracks = []
    song.measureHeaders = []

    bar_ticks = 4 * TICKS_PER_QUARTER
    for i in range(len(ex.bars)):
        header = m.MeasureHeader(number=i + 1)
        header.start = FIRST_TICK + i * bar_ticks
        header.timeSignature = m.TimeSignature(
            numerator=4, denominator=m.Duration(value=QUARTER)
        )
        song.measureHeaders.append(header)

    track = m.Track(song, 1)
    track.name = title
    track.fretCount = instrument.max_fret
    track.strings = [
        m.GuitarString(number=s, value=instrument.tuning[s])
        for s in sorted(instrument.tuning)
    ]
    is_bass = instrument.midi_min < 40
    track.channel = m.MidiChannel(
        channel=0, effectChannel=1,
        instrument=GM_BASS if is_bass else GM_GUITAR,
    )
    track.measures = []

    for header, bar in zip(song.measureHeaders, ex.bars):
        measure = m.Measure(track, header)
        voice = m.Voice(measure)
        voice.beats = []
        tick = header.start
        for step in bar:
            beat = m.Beat(voice)
            beat.start = tick
            beat.duration = m.Duration(value=step.value,
                                       isDotted=step.dotted)
            # Beat.status defaults to `empty`, and the GP5 writer then marks
            # the beat as a rest, so the reader skips its notes and the bytes
            # of the following beats are consumed as part of this one. The
            # symptom is every note of a bar collapsing into one chord.
            beat.status = m.BeatStatus.rest if step.string is None else m.BeatStatus.normal
            if step.string is not None:
                note = m.Note(beat)
                note.value = step.fret
                note.string = step.string
                note.type = m.NoteType.normal
                beat.notes = [note]
            voice.beats.append(beat)
            tick += step.ticks
        # A GP5 measure carries two voices; the second stays empty.
        second_voice = m.Voice(measure)
        second_voice.beats = []
        measure.voices = [voice, second_voice]
        track.measures.append(measure)

    song.tracks = [track]
    return song


# The folder on disk. It is identity, not display: renaming it per language
# would strand every file inside it and every progress record keyed on them.
# The sidebar translates it when it prints it; the directory keeps this name,
# in ASCII, forever.
EXERCISE_DIRNAME = "Uebungen"
_MARKER = ".instrument"
# Bump when generated music changes, so existing installations regenerate.
# History is still transferred per file only after musical equivalence.
_GENERATOR_VERSION = 2


def _marker_text(instrument) -> str:
    """What the marker says about the files currently in the folder.

    Instrument, language and generator revision are baked into the files.
    Filenames stay stable; musical equivalence decides which histories may
    follow a rewrite, independently of the marker.
    """
    return f"{instrument.key} {current_language()} v{_GENERATOR_VERSION}"


def _music_signature(path: Path):
    """Read the actual played material, excluding translated display text.

    Comparing only the instrument key or sounding pitch is insufficient:
    another tuning or fingering is a different learning task, even if the
    notes sound the same. Unreadable material is never assumed equivalent.
    """
    try:
        from pickhero.tabs.loader import load_gp_file

        timeline = load_gp_file(path, track_index=0)
        beats = timeline.beats_in_range(0, timeline.duration_ms + 1)
        return (
            tuple(sorted(timeline.metadata.tuning.items())),
            timeline.metadata.tempo,
            tuple(timeline.notes),
            tuple(timeline.measures),
            tuple((beat, timeline.beat_duration_at(beat.timestamp_ms)) for beat in beats),
        )
    except Exception:  # noqa: BLE001 - parser failures cannot establish equivalence
        return None


def _archive_legacy_stem(progress, stem: str) -> bool:
    """Keep unbound old results from being assigned to changed exercises.

    The library otherwise assigns a stem-only result to the next matching
    file it sees. Retain every historical record, including an existing
    archive of the same name, without claiming it describes the new music.
    """
    data = getattr(progress, "_data", None)
    if not isinstance(data, dict) or stem not in data:
        return False
    base = f"legacy-v1|{stem}"
    key, suffix = base, 1
    while key in data:
        key = f"{base}|generated-{suffix}"
        suffix += 1
    data[key] = data.pop(stem)
    return True


def ensure_exercises(instrument, songs_dir: str | Path, progress=None) -> bool:
    """Make sure the exercise set in `songs_dir` matches `instrument`.

    Cheap to call on every startup: it regenerates only when the folder is
    missing, empty, or was written for a different instrument, language or
    generator revision. Returns True if it wrote anything, so the caller
    knows to rescan.

    `progress` is the live ProgressTracker, if the caller has one. It keeps
    history through equivalent rewrites and archives unbound legacy results
    when an exercise changes, as well as performing the old slug migration.
    """
    migrate_legacy_names(songs_dir, progress)

    folder = Path(songs_dir) / EXERCISE_DIRNAME
    marker = folder / _MARKER
    try:
        current = marker.read_text(encoding="utf-8").strip()
    except OSError:
        current = None
    expected = [folder / ex.filename for ex in build_exercises(instrument)]
    if (current == _marker_text(instrument)
            and all(path.is_file() for path in expected)
            and _tuning_matches(expected[0], instrument)):
        return False

    # Bind existing path history to the old file before replacing it. A
    # later library scan must not attach that history to changed material.
    # Translations may carry history forward; changed tuning/fingering may
    # not. Keep the original records in either case.
    previous_ids = {}
    if progress is not None and current and current.split()[0] == instrument.key:
        from pickhero.library import content_identity
        for path in expected:
            try:
                previous_id = content_identity(path)
            except (OSError, ValueError):
                continue
            previous_ids[path.name] = (previous_id, _music_signature(path))
            progress.migrate_content_identity(path, instrument.key, 0, previous_id)

    # Only remove files this function wrote. It used to delete every .gp5 in
    # the folder, so a tab the user had put there themselves disappeared the
    # next time they switched instrument.
    if folder.is_dir():
        for stale in folder.glob("*.gp5"):
            if stale.name in _generated_names():
                try:
                    stale.unlink()
                except OSError:
                    pass
    written = write_exercises(instrument, folder)
    archived = False
    if previous_ids:
        for path in written:
            if path.name not in previous_ids:
                continue
            previous_id, previous_music = previous_ids[path.name]
            replacement_music = _music_signature(path)
            if previous_music is None or previous_music != replacement_music:
                if previous_music is not None and replacement_music is not None:
                    archived = _archive_legacy_stem(progress, path.stem) or archived
                continue
            try:
                replacement_id = content_identity(path)
            except (OSError, ValueError):
                continue
            progress.migrate_content_replacement(previous_id, replacement_id, instrument.key)
    if archived:
        progress._save()
    try:
        marker.write_text(_marker_text(instrument), encoding="utf-8")
    except OSError:
        pass
    return bool(written)


def _generated_names() -> set[str]:
    """Filenames this module produces, for any instrument and any language.

    A fixed set, because the filename comes from the exercise order and its
    slug. Nothing about the instrument or the interface language reaches it.
    """
    return {f"{i:02d} {slug}.gp5" for i, slug in enumerate(_SLUGS, 1)}


# --- one-off migration: German titles to slugs -------------------------------
#
# The exercise files used to be named after their German titles, and the
# progress history is keyed on the file stem. Moving the titles into the
# translation catalogue would have renamed every file the moment the interface
# changed language, and every record the player had would have been orphaned with
# nothing to say so.
#
# These ten names are frozen here on purpose. They have to match what is on
# their disk, not what build_exercises happens to produce today, so they are a
# literal and not something derived.
_LEGACY_STEMS = {
    "01 Eine leere Saite": "01 open_string",
    "02 Alle leeren Saiten": "02 all_open",
    "03 Viertel auf einer Saite": "03 quarters_one_string",
    "04 Saitenwechsel": "04 string_change",
    "05 Leere Saiten rauf und runter": "05 open_up_down",
    "06 Die ersten Buende": "06 first_frets",
    "07 Buende rueckwaerts": "07 frets_back",
    "08 Tonleiter eine Oktave": "08 scale_one_octave",
    "09 Achtel": "09 eighths",
    "10 Erstes Muster": "10 first_pattern",
}

_MIGRATION_MARKER = ".exercise-slugs"


def migrate_legacy_names(songs_dir: str | Path, progress=None) -> bool:
    """Move the old German exercise set onto slugs, once.

    Deletes exactly the ten files the old naming produced -- nothing else in
    the folder is touched -- and renames the matching keys in progress.json to
    the new stems, so them practice history follows the files.

    Guarded by a marker in the config directory, so it runs on the first start
    after the change and never again. Tolerant of everything: a missing
    progress file, a malformed one, one that is not an object at all, a
    songs folder that does not exist yet. It never raises.
    """
    from pickhero import config as config_module

    flag = config_module.CONFIG_DIR / _MIGRATION_MARKER
    try:
        if flag.exists():
            return False
    except OSError:
        return False

    folder = Path(songs_dir) / EXERCISE_DIRNAME
    for stem in _LEGACY_STEMS:
        try:
            (folder / f"{stem}.gp5").unlink()
        except OSError:
            pass

    moved = _rekey_progress_file()
    # The tracker is built before this runs, so the file alone is not enough:
    # its next save would write the old keys straight back over the new ones.
    _rekey(getattr(progress, "_data", None))

    try:
        config_module.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        flag.write_text(
            "Exercise files renamed from German titles to slugs.\n",
            encoding="utf-8",
        )
    except OSError:
        pass
    return bool(moved)


def _rekey(data) -> int:
    """Rename the legacy keys in a progress mapping, in place.

    A key is only moved when the new one is not already there, so a record
    that has somehow been written under the new stem already is never
    overwritten by an older one.
    """
    if not isinstance(data, dict):
        return 0
    moved = 0
    for old, new in _LEGACY_STEMS.items():
        if old in data and new not in data:
            data[new] = data.pop(old)
            moved += 1
    return moved


def _rekey_progress_file() -> int:
    """Rewrite progress.json with the new stems. Returns how many moved."""
    from pickhero import progress as progress_module

    path = Path(progress_module.PROGRESS_FILE)
    try:
        with open(path, encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, ValueError):
        return 0
    if not isinstance(raw, dict):
        return 0
    moved = _rekey(raw)
    if not moved:
        return 0
    try:
        tmp = path.with_suffix(".migrating")
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(raw, handle, indent=2)
        tmp.replace(path)
    except (OSError, TypeError, ValueError) as exc:
        # A diagnostic for the log file, not something the player reads.
        log_error(f"Exercise progress keys could not be migrated: {exc}")
        return 0
    return moved


def _tuning_matches(path: Path, instrument) -> bool:
    """Check an exercise file really carries the instrument's tuning.

    The marker file alone is not enough. It can say "bass_ukulele" while the
    files on disk are the guitar set, and the symptom is silent: the app draws
    six lanes instead of four and asks for notes an octave above anything the
    instrument can produce, so every note scores a miss and nothing explains
    why. Reading one file costs a few milliseconds and removes the whole class
    of failure.
    """
    try:
        from pickhero.tabs.loader import list_tracks
        tracks = list_tracks(path)
    except Exception:
        return False
    return bool(tracks) and tracks[0].get("tuning") == instrument.tuning


def write_exercises(instrument, out_dir: str | Path) -> list[Path]:
    """Generate the exercise set for an instrument into `out_dir`.

    Existing files are overwritten, so regenerating after changing instrument
    is safe. Returns the paths written, in order.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for ex in build_exercises(instrument):
        path = out_dir / ex.filename
        gp.write(_build_song(ex, instrument), str(path))
        written.append(path)
    return written
