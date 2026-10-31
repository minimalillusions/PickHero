"""The song library: metadata, difficulty, and a cache so it stays fast.

The browser needs more than filenames. It needs to say how long a piece is,
how fast, and above all how hard, because "which of these 300 can I actually
play today" is the question a beginner cannot answer from a list of names.

That means parsing every file, which is far too slow to do on every keystroke,
so results are cached by path, modification time and instrument. A file only
gets re-read when it actually changes.
"""

from __future__ import annotations

import json
import hashlib
from dataclasses import asdict, dataclass, field
from pathlib import Path

from pickhero.config import CONFIG_DIR
from pickhero.i18n import t
from pickhero import jsonfile
from pickhero.validation import integer, number, text

CACHE_FILE = CONFIG_DIR / "library.json"
# 2: reasons are stored as (key, number) instead of a finished German
# sentence, so the cache no longer freezes the explanation in whatever
# language the file happened to be scanned in.
CACHE_VERSION = 5

GP_EXTENSIONS = {".gp3", ".gp4", ".gp5", ".gp7", ".gp8", ".gpx", ".gp", ".mid", ".midi"}

# Five levels, easiest first. The value is a catalogue key, not a label:
# a level survives a language change, its name does not.
LEVEL_KEYS = {
    1: "level.beginner",
    2: "level.easy",
    3: "level.medium",
    4: "level.hard",
    5: "level.pro",
}

# How the number in a difficulty reason is written out. The reason itself
# carries the raw value, so the same cached entry renders in any language.
_REASON_FORMAT = {
    "reason.density": "{:.1f}",
    "reason.subdivision": "{:.2f}",
    "reason.fret_span": "{:.0f}",
    "reason.highest_fret": "{:.0f}",
    "reason.string_changes": "{:.1f}",
    "reason.chords": "{:.0f}",
}


def level_name(level: int) -> str:
    """The name of a difficulty level, in the player's language."""
    key = LEVEL_KEYS.get(level)
    return t(key) if key else "?"


def render_reason(reason) -> str:
    """One difficulty reason as a finished line of text.

    Takes what estimate_difficulty stored -- a key and a number -- and never
    raises, because this is called from inside a draw method. A reason read
    back from the JSON cache arrives as a list, which unpacks the same way.
    """
    try:
        key, value = reason
    except (TypeError, ValueError):
        return ""
    template = _REASON_FORMAT.get(key)
    if template is None:
        return t(key)
    try:
        number = template.format(float(value))
    except (TypeError, ValueError):
        number = str(value)
    return t(key, n=number)


@dataclass
class SongInfo:
    """Everything the browser shows about one file."""

    path: str
    title: str = ""
    artist: str = ""
    track_name: str = ""
    bars: int = 0
    bpm: int = 0
    notes: int = 0
    duration_ms: float = 0.0
    difficulty: int = 3
    strings: int = 0
    playable: bool = True
    mtime: float = 0.0
    instrument: str = ""
    track_index: int = 0
    compatible: bool = True
    content_id: str = ""
    file_size: int = 0
    # Why it landed on that level, so the number is never unexplained.
    # A catalogue key and its number, not a sentence: this is serialised
    # into the cache, and the cache outlives any one language.
    reasons: list[tuple[str, float]] = field(default_factory=list)

    @property
    def file(self) -> Path:
        return Path(self.path)

    @property
    def display_title(self) -> str:
        return self.title or self.file.stem

    @property
    def level_name(self) -> str:
        return level_name(self.difficulty)

    def summary(self) -> str:
        """The metadata line under the title."""
        parts = []
        if self.artist:
            parts.append(self.artist)
        if self.bars:
            parts.append(t("song.bars", count=self.bars))
        if self.bpm:
            parts.append(t("song.bpm", bpm=self.bpm))
        # Only when it says something the title has not already said. A
        # generated exercise names its track after itself.
        track = self.track_name.strip()
        if track and track.lower() not in self.display_title.lower():
            parts.append(track)
        return "  ·  ".join(parts)


# ── Difficulty ───────────────────────────────────────────────────────────

def _clip(value: float) -> float:
    return max(0.0, min(1.0, value))


def estimate_difficulty(timeline, instrument) -> tuple[int, list[tuple[str, float]]]:
    """Rate a piece from 1 (Anfang) to 5 (Profi), with the reasons.

    Six measurable things, each normalised to 0..1 and weighted. Nothing here
    is a guess about the music; every term is counted off the notes.

    The reasons come back as (catalogue key, number) pairs rather than as
    text. They are cached, and a cached sentence would still be German after
    the player switched to English -- the cache is keyed on the file, which
    never changes again.

    The weights say what actually makes a piece hard for a beginner on a
    fretted instrument. Speed of notes and rhythmic subdivision dominate,
    because that is what runs out of hand first. Hand position matters next:
    a piece confined to the first three frets can be played without shifting.
    Chords come last, since on a bass line they barely occur.
    """
    notes = timeline.notes
    if not notes:
        return 1, [("reason.no_notes", 0.0)]

    seconds = max(timeline.duration_ms / 1000.0, 1.0)
    bpm = max(float(getattr(timeline.metadata, "tempo", 120) or 120), 1.0)
    beat_ms = 60000.0 / bpm

    stamps = sorted({n.timestamp_ms for n in notes})
    density = len(stamps) / seconds

    gaps = [b - a for a, b in zip(stamps, stamps[1:]) if b - a > 1.0]
    shortest_beats = (min(gaps) / beat_ms) if gaps else 1.0
    subdivision = 1.0 / max(shortest_beats, 0.0625)

    frets = [n.fret for n in notes]
    span = max(frets) - min(frets)
    highest = max(frets)

    ordered = sorted(notes, key=lambda n: (n.timestamp_ms, n.string))
    changes = sum(1 for a, b in zip(ordered, ordered[1:]) if a.string != b.string)
    string_changes = changes / seconds

    per_stamp: dict[float, int] = {}
    for note in notes:
        per_stamp[note.timestamp_ms] = per_stamp.get(note.timestamp_ms, 0) + 1
    chorded = sum(1 for count in per_stamp.values() if count > 1)
    chord_fraction = chorded / max(len(per_stamp), 1)

    terms = [
        (0.28, _clip(density / 6.0), ("reason.density", density)),
        (0.24, _clip((subdivision - 1.0) / 3.0),
         ("reason.subdivision", shortest_beats)),
        (0.16, _clip(span / 10.0), ("reason.fret_span", float(span))),
        (0.14, _clip(highest / max(instrument.max_fret, 1)),
         ("reason.highest_fret", float(highest))),
        (0.10, _clip(string_changes / 3.0),
         ("reason.string_changes", string_changes)),
        (0.08, _clip(chord_fraction * 2.0),
         ("reason.chords", chord_fraction * 100.0)),
    ]
    score = sum(weight * value for weight, value, _ in terms)

    # Thresholds chosen so the generated beginner exercises land on 1 and 2
    # and a real song at speed lands on 4 or 5. Validated in the tests.
    for level, upper in ((1, 0.12), (2, 0.24), (3, 0.42), (4, 0.64)):
        if score <= upper:
            break
    else:
        level = 5

    ranked = sorted(terms, key=lambda term: -term[0] * term[1])
    return level, [reason for _, _, reason in ranked[:3]]


# ── Scanning ─────────────────────────────────────────────────────────────

def content_identity(path: str | Path) -> str:
    """A bounded byte identity, independent of a song's name and location."""
    from pickhero.tabs.import_safety import bounded_file
    return hashlib.sha256(bounded_file(path)).hexdigest()


def _read_song(path: Path, instrument) -> SongInfo:
    from pickhero.tabs.loader import (
        list_tracks, load_gp_file, select_track_index, track_matches_instrument,
    )

    info = SongInfo(path=str(path), instrument=instrument.key)
    try:
        info.mtime = path.stat().st_mtime
        info.file_size = path.stat().st_size
    except OSError:
        pass
    try:
        info.content_id = content_identity(path)
        track_index = None
        if path.suffix.lower() not in {".mid", ".midi"}:
            tracks = list_tracks(path)
            if not any(track_matches_instrument(track, instrument) for track in tracks):
                info.compatible = False
                info.playable = False
                info.title = path.stem
                return info
            track_index = select_track_index(tracks, instrument)
        timeline = load_gp_file(path, instrument=instrument, track_index=track_index)
    except Exception:
        info.playable = False
        info.title = path.stem
        info.reasons = [("reason.unreadable", 0.0)]
        return info

    meta = timeline.metadata
    info.title = meta.title or path.stem
    info.artist = meta.artist or ""
    info.track_name = meta.track_name or ""
    info.track_index = meta.track_index
    info.bpm = int(meta.tempo or 0)
    info.bars = len(timeline.measures)
    info.notes = len(timeline)
    info.duration_ms = timeline.duration_ms
    info.strings = len(meta.tuning or {})
    info.playable = info.notes > 0
    info.difficulty, info.reasons = estimate_difficulty(timeline, instrument)
    return info


def _load_cache(*, quiet: bool = False) -> dict:
    # A cache, so a bad one costs a rescan and nothing else -- but it still
    # may not take the app down, and a non-UTF-8 file would have.
    if quiet:
        # Workers must not log into the user's config directory, including
        # when the disposable metadata cache happens to be corrupt.
        from pickhero.bounded_io import read_bounded
        try:
            with CACHE_FILE.open("rb") as stream:
                data = json.loads(read_bounded(stream, jsonfile.MAX_JSON_BYTES))
        except (OSError, ValueError, RecursionError):
            data = None
    else:
        data = jsonfile.read(CACHE_FILE, what="Der Bibliotheks-Zwischenspeicher")
    if data is None:
        return {}
    if not isinstance(data, dict) or data.get("version") != CACHE_VERSION:
        return {}
    entries = data.get("entries")
    return entries if isinstance(entries, dict) else {}


def _save_cache(entries: dict) -> None:
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        tmp = CACHE_FILE.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump({"version": CACHE_VERSION, "entries": entries}, handle)
        tmp.replace(CACHE_FILE)
    except OSError:
        pass


def _cached_song(raw, path: Path, instrument, mtime: float) -> SongInfo | None:
    """One corrupt cache entry must not hide the entire library."""
    if not isinstance(raw, dict):
        return None
    if raw.get("path") != str(path) or raw.get("instrument") != instrument.key:
        return None
    identity = raw.get("content_id", "")
    if (not isinstance(identity, str) or (identity and
            (len(identity) != 64 or any(c not in "0123456789abcdef" for c in identity)))):
        return None
    try:
        if raw.get("file_size") != path.stat().st_size:
            return None
    except OSError:
        return None
    if not number(raw.get("mtime"), 0, 1e12) or abs(raw["mtime"] - mtime) >= 1e-6:
        return None
    if any(not text(raw.get(key), 8192) for key in ("title", "artist", "track_name")):
        return None
    if any(not integer(raw.get(key), 0, 1_000_000) for key in ("bars", "bpm", "notes", "track_index")):
        return None
    if (not integer(raw.get("difficulty"), 1, 5) or not integer(raw.get("strings"), 0, 12)
            or not number(raw.get("duration_ms"), 0, 24 * 60 * 60 * 1000)
            or type(raw.get("playable")) is not bool
            or type(raw.get("compatible")) is not bool):
        return None
    reasons = raw.get("reasons")
    if not isinstance(reasons, list) or len(reasons) > 20:
        return None
    for reason in reasons:
        if (not isinstance(reason, list) or len(reason) != 2 or not text(reason[0], 100)
                or not number(reason[1], -1e9, 1e9)):
            return None
    try:
        return SongInfo(**raw)
    except TypeError:
        return None


def scan(songs_dir: str | Path, instrument, use_cache: bool = True,
         *, include_incompatible: bool = False, on_progress=None,
         quiet_cache: bool = False) -> list[SongInfo]:
    """Index every tab under `songs_dir` for `instrument`.

    Parsing a few hundred Guitar Pro files takes seconds, which is fine once
    and unacceptable on every keystroke, so results are cached by path,
    modification time and instrument key. ``include_incompatible`` preserves
    hidden file identities for safe legacy-history migration in the browser.
    """
    root = Path(songs_dir)
    files = []
    # Symlink directories are not followed by Path.walk. A finite catalogue
    # prevents a mistakenly selected system drive from consuming the worker.
    for folder, directories, names in root.walk(follow_symlinks=False):
        directories[:] = [name for name in directories if not name.startswith(".")]
        for name in names:
            path = folder / name
            if path.suffix.lower() in GP_EXTENSIONS and path.is_file():
                files.append(path)
                if len(files) > 10_000:
                    raise ValueError("Library exceeds 10000 score files")
    files.sort()

    cache = _load_cache(quiet=quiet_cache) if use_cache else {}
    out: list[SongInfo] = []
    dirty = False

    for index, path in enumerate(files):
        if on_progress is not None:
            on_progress(index, len(files), path.name)
        key = f"{instrument.key}|{path}"
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = 0.0
        cached = _cached_song(cache.get(key), path, instrument, mtime)
        if cached is not None:
            if cached.compatible or include_incompatible:
                out.append(cached)
            continue
        info = _read_song(path, instrument)
        cache[key] = asdict(info)
        dirty = True
        if info.compatible or include_incompatible:
            out.append(info)

    if dirty and use_cache:
        # Drop entries for files that are gone, so the cache cannot grow forever.
        live = {f"{instrument.key}|{p}" for p in files}
        cache = {k: v for k, v in cache.items()
                 if not k.startswith(f"{instrument.key}|") or k in live}
        _save_cache(cache)
    if on_progress is not None:
        on_progress(len(files), len(files), "")
    return out


def counts_by_level(songs: list[SongInfo]) -> dict[int, int]:
    result = {level: 0 for level in LEVEL_KEYS}
    for song in songs:
        result[song.difficulty] = result.get(song.difficulty, 0) + 1
    return result


def counts_by_folder(songs: list[SongInfo], root: str | Path) -> dict[str, int]:
    root = Path(root)
    result: dict[str, int] = {}
    for song in songs:
        try:
            relative = song.file.relative_to(root)
        except ValueError:
            continue
        folder = relative.parent.as_posix()
        if folder in (".", ""):
            folder = t("menu.no_folder")
        result[folder] = result.get(folder, 0) + 1
    return result
