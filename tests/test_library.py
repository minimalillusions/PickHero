"""Difficulty and the library index.

"Sorted by difficulty" is the whole reason the browser parses every file, and
none of it was tested. A difficulty that does not track the notes turns the
graded exercise set into an arbitrary list.
"""

import json

import pytest

from pickhero import instruments, library
from pickhero.library import (LEVEL_KEYS, SongInfo, estimate_difficulty,
                              level_name, render_reason, scan)
from pickhero.tabs.timeline import NoteEvent, SongMetadata, Timeline


def _timeline(count: int, *, bpm: int = 120, spacing_ms: float = 500.0,
              frets=(0,), strings=(1,), duration_ms: float = 400.0) -> Timeline:
    notes = []
    for i in range(count):
        notes.append(NoteEvent(
            timestamp_ms=i * spacing_ms, duration_ms=duration_ms,
            midi_note=40 + frets[i % len(frets)],
            string=strings[i % len(strings)], fret=frets[i % len(frets)]))
    return Timeline(notes, SongMetadata(title="t", tempo=bpm))


BASS = instruments.BASS_UKULELE


class TestDifficulty:
    def test_an_empty_piece_is_the_easiest_level(self):
        level, reasons = estimate_difficulty(Timeline([]), BASS)
        assert level == 1
        assert reasons

    def test_faster_notes_are_harder(self):
        slow, _ = estimate_difficulty(_timeline(8, spacing_ms=2000.0), BASS)
        fast, _ = estimate_difficulty(_timeline(8, spacing_ms=120.0), BASS)
        assert fast > slow

    def test_a_wider_fret_span_is_harder(self):
        near, _ = estimate_difficulty(_timeline(16, frets=(0, 1)), BASS)
        far, _ = estimate_difficulty(_timeline(16, frets=(0, 12)), BASS)
        assert far > near

    def test_string_changes_make_it_harder(self):
        one, _ = estimate_difficulty(_timeline(24, strings=(1,)), BASS)
        many, _ = estimate_difficulty(_timeline(24, strings=(1, 2, 3, 4)), BASS)
        assert many >= one

    def test_every_level_has_a_name(self):
        for level in range(1, 6):
            assert LEVEL_KEYS[level]
            assert level_name(level).strip()

    def test_the_level_stays_in_range(self):
        for spacing in (10.0, 60.0, 250.0, 4000.0):
            for count in (1, 5, 200):
                level, _ = estimate_difficulty(
                    _timeline(count, spacing_ms=spacing, frets=(0, 5, 12, 16),
                              strings=(1, 2, 3, 4)), BASS)
                assert 1 <= level <= 5

    def test_the_reasons_are_never_empty(self):
        _, reasons = estimate_difficulty(_timeline(20), BASS)
        assert reasons
        for key, value in reasons:
            assert isinstance(key, str) and key
            assert isinstance(value, float)
            assert render_reason((key, value)).strip()

    def test_a_reason_is_a_key_and_a_number_not_a_sentence(self):
        """The reasons are cached, and the cache is keyed on the file, which
        for a .gp5 never changes again. A finished sentence in there would
        still be German long after the player switched the app to English."""
        _, reasons = estimate_difficulty(_timeline(20), BASS)
        for key, _value in reasons:
            assert key.startswith("reason.")

    def test_rendering_a_damaged_reason_does_not_raise(self):
        """This runs inside a draw method."""
        for junk in (None, "reason.density", (), ("reason.density", "x"),
                     ["reason.no_notes", 0.0]):
            assert isinstance(render_reason(junk), str)


class TestGeneratedExercisesAreGraded:
    """The set is numbered as a progression. The browser sorts by the measured
    difficulty, so the two have to agree or the numbering is a lie."""

    def test_difficulty_never_drops_across_the_set(self, tmp_path):
        from pickhero.exercises import write_exercises

        write_exercises(BASS, tmp_path)
        songs = sorted(scan(tmp_path, BASS, use_cache=False),
                       key=lambda s: s.file.name)
        assert len(songs) == 10
        levels = [s.difficulty for s in songs]
        assert levels == sorted(levels), levels

    def test_the_set_starts_at_the_easiest_level(self, tmp_path):
        from pickhero.exercises import write_exercises

        write_exercises(BASS, tmp_path)
        songs = sorted(scan(tmp_path, BASS, use_cache=False),
                       key=lambda s: s.file.name)
        assert songs[0].difficulty == 1

    def test_the_guitar_set_is_graded_too(self, tmp_path):
        from pickhero.exercises import write_exercises

        write_exercises(instruments.GUITAR, tmp_path)
        songs = sorted(scan(tmp_path, instruments.GUITAR, use_cache=False),
                       key=lambda s: s.file.name)
        levels = [s.difficulty for s in songs]
        assert levels == sorted(levels), levels


class TestScan:
    def test_an_empty_folder_gives_an_empty_index(self, tmp_path):
        assert scan(tmp_path, BASS, use_cache=False) == []

    def test_a_file_that_is_not_a_tab_is_ignored(self, tmp_path):
        (tmp_path / "notizen.txt").write_text("nichts", encoding="utf-8")
        assert scan(tmp_path, BASS, use_cache=False) == []

    def test_an_unreadable_tab_is_listed_but_marked(self, tmp_path):
        """Better a row saying the file will not open than a file that
        silently vanishes from the library."""
        (tmp_path / "kaputt.gp5").write_bytes(b"das ist keine gp5 datei")
        songs = scan(tmp_path, BASS, use_cache=False)
        assert len(songs) == 1
        assert songs[0].playable is False
        assert songs[0].reasons

    def test_subfolders_are_included(self, tmp_path):
        from pickhero.exercises import write_exercises

        (tmp_path / "tief").mkdir()
        write_exercises(BASS, tmp_path / "tief")
        assert len(scan(tmp_path, BASS, use_cache=False)) == 10

    def test_the_cache_is_used_on_the_second_pass(self, tmp_path):
        from pickhero.exercises import write_exercises

        write_exercises(BASS, tmp_path)
        first = scan(tmp_path, BASS)
        calls = []
        original = library._read_song

        def counting(path, instrument):
            calls.append(path)
            return original(path, instrument)

        library._read_song = counting
        try:
            second = scan(tmp_path, BASS)
        finally:
            library._read_song = original
        assert calls == [], "nichts haette neu gelesen werden duerfen"
        assert [s.path for s in second] == [s.path for s in first]

    def test_a_changed_file_is_read_again(self, tmp_path):
        """Editing a tab has to show up. The cache is keyed on the
        modification time, so the file has to be genuinely newer."""
        import os
        from pickhero.exercises import write_exercises

        write_exercises(BASS, tmp_path)
        scan(tmp_path, BASS)
        target = sorted(tmp_path.glob("*.gp5"))[0]
        stamp = target.stat().st_mtime + 10.0
        os.utime(target, (stamp, stamp))

        calls = []
        original = library._read_song

        def counting(path, instrument):
            calls.append(path)
            return original(path, instrument)

        library._read_song = counting
        try:
            scan(tmp_path, BASS)
        finally:
            library._read_song = original
        assert calls == [target]

    def test_a_corrupt_cache_file_is_ignored(self, tmp_path):
        library.CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        library.CACHE_FILE.write_text("{kaputt", encoding="utf-8")
        assert scan(tmp_path, BASS) == []

    def test_each_instrument_gets_its_own_index(self, tmp_path):
        """The same file is a different piece on a different instrument: a
        different track, a different difficulty."""
        from pickhero.exercises import write_exercises

        write_exercises(BASS, tmp_path)
        scan(tmp_path, BASS)
        entries = json.loads(library.CACHE_FILE.read_text(encoding="utf-8"))
        assert all(key.startswith("bass_ukulele|")
                   for key in entries["entries"])


class TestSongInfo:
    def test_the_filename_stands_in_for_a_missing_title(self, tmp_path):
        info = SongInfo(path=str(tmp_path / "Ohne Titel.gp5"))
        assert info.display_title == "Ohne Titel"

    def test_the_summary_does_not_repeat_the_title(self, tmp_path):
        info = SongInfo(path=str(tmp_path / "a.gp5"), title="Achtel",
                        track_name="Achtel", bars=8, bpm=60)
        assert info.summary().count("Achtel") == 0

    def test_the_summary_names_a_track_that_adds_something(self, tmp_path):
        info = SongInfo(path=str(tmp_path / "a.gp5"), title="Song",
                        track_name="Bass", bars=8, bpm=60)
        assert "Bass" in info.summary()

    def test_a_single_bar_is_not_plural(self, tmp_path):
        """It printed "1 Takte" long before anything was translated."""
        info = SongInfo(path=str(tmp_path / "a.gp5"), bars=1)
        assert "1 Takt" in info.summary()
        assert "1 Takte" not in info.summary()
