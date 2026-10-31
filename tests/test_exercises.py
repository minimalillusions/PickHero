"""Tests for the generated practice exercises.

The test that matters is test_notes_do_not_collapse_into_chords. Building a
Guitar Pro file by hand is easy to get subtly wrong: pyguitarpro's Beat.status
defaults to `empty`, the GP5 writer then marks each beat as a rest, and the
reader consumes the following beats' bytes as part of the first one. The file
still parses, the note count still looks plausible, and every note of a bar
lands on the same timestamp as a chord. Nothing raises. Only checking the
timestamps catches it.
"""

from pathlib import Path

import pytest

from pickhero import instruments
from pickhero.exercises import (
    EXERCISE_DIRNAME,
    _LEGACY_STEMS,
    _SLUGS,
    build_exercises,
    ensure_exercises,
    migrate_legacy_names,
    write_exercises,
)
from pickhero.tabs.loader import load_gp_file

PROFILES = [instruments.GUITAR, instruments.BASS_UKULELE]


@pytest.fixture(params=PROFILES, ids=lambda p: p.key)
def profile(request):
    return request.param


class TestBuild:
    def test_set_is_graded_and_non_empty(self, profile):
        exercises = build_exercises(profile)
        assert len(exercises) >= 8
        assert [e.order for e in exercises] == sorted(e.order for e in exercises)
        assert all(e.note_count > 0 for e in exercises)
        assert all(e.bpm <= 80 for e in exercises), "nothing here should be fast"

    def test_starts_with_a_single_open_string(self, profile):
        first = build_exercises(profile)[0]
        steps = [s for bar in first.bars for s in bar]
        assert {s.fret for s in steps} == {0}
        assert len({s.string for s in steps}) == 1

    def test_only_uses_strings_and_frets_the_instrument_has(self, profile):
        for ex in build_exercises(profile):
            for bar in ex.bars:
                for step in bar:
                    assert step.string in profile.tuning
                    assert 0 <= step.fret <= profile.max_fret

    def test_adapts_to_the_instrument(self):
        guitar = build_exercises(instruments.GUITAR)[1]
        uke = build_exercises(instruments.BASS_UKULELE)[1]
        # "every open string in turn" must have one bar per string
        assert len(guitar.bars) == 2 * instruments.GUITAR.string_count
        assert len(uke.bars) == 2 * instruments.BASS_UKULELE.string_count


class TestWrite:
    def test_files_load_back_with_the_right_notes(self, profile, tmp_path):
        paths = write_exercises(profile, tmp_path)
        assert len(paths) == len(build_exercises(profile))
        for path, ex in zip(paths, build_exercises(profile)):
            timeline = load_gp_file(path, instrument=profile)
            assert len(timeline) == ex.note_count, f"{path.name}"
            assert timeline.metadata.tempo == ex.bpm
            assert timeline.metadata.tuning == profile.tuning

    def test_notes_do_not_collapse_into_chords(self, profile, tmp_path):
        """Each note must have its own timestamp, in ascending order."""
        for path in write_exercises(profile, tmp_path):
            notes = load_gp_file(path, instrument=profile).notes
            times = [n.timestamp_ms for n in notes]
            assert times == sorted(times), f"{path.name}: out of order"
            assert len(set(times)) == len(times), (
                f"{path.name}: {len(times) - len(set(times))} notes share a "
                "timestamp, so a bar collapsed into a chord"
            )

    def test_every_note_is_in_the_instrument_range(self, profile, tmp_path):
        for path in write_exercises(profile, tmp_path):
            for note in load_gp_file(path, instrument=profile).notes:
                assert profile.midi_min <= note.midi_note <= profile.midi_max

    def test_the_track_is_selected_by_the_instrument(self, profile, tmp_path):
        """A generated exercise must survive the normal auto-selection path."""
        for path in write_exercises(profile, tmp_path):
            assert len(load_gp_file(path, instrument=profile)) > 0


class TestEnsure:
    def test_generates_once_then_leaves_it_alone(self, tmp_path):
        uke = instruments.BASS_UKULELE
        assert ensure_exercises(uke, tmp_path) is True
        assert ensure_exercises(uke, tmp_path) is False

    def test_regenerates_when_the_instrument_changes(self, tmp_path):
        ensure_exercises(instruments.BASS_UKULELE, tmp_path)
        assert ensure_exercises(instruments.GUITAR, tmp_path) is True
        folder = Path(tmp_path) / EXERCISE_DIRNAME
        for path in folder.glob("*.gp5"):
            assert load_gp_file(path, instrument=instruments.GUITAR).metadata.tuning \
                == instruments.GUITAR.tuning

    def test_replaces_its_own_files_but_keeps_the_users(self, tmp_path):
        """Switching instrument rewrites the set without touching anything else.

        It used to delete every .gp5 in the folder, so a tab the user had put
        there themselves vanished the next time they pressed I. Now only the
        filenames this module produces are removed, and everything else is
        left alone.
        """
        ensure_exercises(instruments.GUITAR, tmp_path)
        folder = Path(tmp_path) / EXERCISE_DIRNAME
        mine = folder / "99 Von Hand abgelegt.gp5"
        mine.write_bytes((folder / "01 open_string.gp5").read_bytes())

        ensure_exercises(instruments.BASS_UKULELE, tmp_path)

        assert mine.exists(), "the app deleted a file the user put there"
        generated = sorted(p for p in folder.glob("*.gp5") if p != mine)
        assert len(generated) == len(build_exercises(instruments.BASS_UKULELE))
        for path in generated:
            tuning = load_gp_file(path, instrument=instruments.BASS_UKULELE).metadata.tuning
            assert tuning == instruments.BASS_UKULELE.tuning, f"{path.name} is stale"

    def test_recovers_if_the_folder_is_emptied(self, tmp_path):
        uke = instruments.BASS_UKULELE
        ensure_exercises(uke, tmp_path)
        for path in (Path(tmp_path) / EXERCISE_DIRNAME).glob("*.gp5"):
            path.unlink()
        assert ensure_exercises(uke, tmp_path) is True


class TestIdentityIsNotTranslated:
    """The filename is the progress key. It must not move when the text does.

    The titles live in the translation catalogue now. If the filename were
    still built from the title, switching the interface to English would
    rename all ten files, and every record in progress.json -- which is keyed
    on the file stem -- would be orphaned with nothing anywhere to say so.
    """

    def test_filenames_are_slugs_and_never_the_title(self, profile, monkeypatch):
        from pickhero import exercises as exercises_module

        names = [ex.filename for ex in build_exercises(profile)]
        assert names == [f"{i:02d} {slug}.gp5"
                         for i, slug in enumerate(_SLUGS, 1)]
        monkeypatch.setattr(exercises_module, "t",
                            lambda key, **kw: f"TRANSLATED {key}")
        assert [ex.filename for ex in build_exercises(profile)] == names

    def test_the_folder_on_disk_keeps_its_ascii_name(self):
        assert EXERCISE_DIRNAME == "Uebungen"

    def test_every_legacy_name_maps_onto_a_name_we_now_write(self):
        from pickhero.exercises import _generated_names

        produced = _generated_names()
        assert len(produced) == len(_SLUGS)
        for new in _LEGACY_STEMS.values():
            assert f"{new}.gp5" in produced

    def test_the_language_is_recorded_so_the_text_can_be_rewritten(
            self, tmp_path, monkeypatch):
        """Changing language rewrites the files without renaming them.

        The titles live inside the .gp5 files, so they go stale when the player
        switches language. The filenames do not, because they are slugs.
        """
        from pickhero import exercises as exercises_module

        uke = instruments.BASS_UKULELE
        assert ensure_exercises(uke, tmp_path) is True
        folder = Path(tmp_path) / EXERCISE_DIRNAME
        before = sorted(p.name for p in folder.glob("*.gp5"))
        assert ensure_exercises(uke, tmp_path) is False

        monkeypatch.setattr(exercises_module, "current_language", lambda: "en")
        assert ensure_exercises(uke, tmp_path) is True
        assert sorted(p.name for p in folder.glob("*.gp5")) == before


class TestLegacyMigration:
    """Their practice history has to survive the rename. It is five records."""

    HISTORY = {
        "01 Eine leere Saite": {"best_accuracy": 88.0, "attempts": 4},
        "02 Alle leeren Saiten": {"best_accuracy": 71.5, "attempts": 2},
        "03 Viertel auf einer Saite": {"best_accuracy": 64.0, "attempts": 7},
        "04 Saitenwechsel": {"best_accuracy": 55.0, "attempts": 1},
        "06 Die ersten Buende": {"best_accuracy": 42.0, "attempts": 3},
    }

    def _write_progress(self, payload) -> Path:
        import json as _json
        from pickhero import progress as progress_module

        path = Path(progress_module.PROGRESS_FILE)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            payload if isinstance(payload, str) else _json.dumps(payload),
            encoding="utf-8",
        )
        return path

    def _read_progress(self) -> dict:
        import json as _json
        from pickhero import progress as progress_module

        return _json.loads(
            Path(progress_module.PROGRESS_FILE).read_text(encoding="utf-8")
        )

    def test_the_old_keys_come_out_as_the_new_ones_with_the_same_values(self, tmp_path):
        self._write_progress(self.HISTORY)
        migrate_legacy_names(tmp_path)

        after = self._read_progress()
        assert set(after) == {_LEGACY_STEMS[k] for k in self.HISTORY}
        for old, record in self.HISTORY.items():
            assert after[_LEGACY_STEMS[old]] == record

    def test_records_that_are_not_exercises_are_left_alone(self, tmp_path):
        payload = dict(self.HISTORY)
        payload["Nirvana - Come As You Are"] = {"best_accuracy": 31.0,
                                                "attempts": 9}
        self._write_progress(payload)
        migrate_legacy_names(tmp_path)

        after = self._read_progress()
        assert after["Nirvana - Come As You Are"] == {"best_accuracy": 31.0,
                                                      "attempts": 9}

    def test_a_progress_file_without_the_old_keys_is_untouched(self, tmp_path):
        from pickhero import progress as progress_module

        payload = {"Irgendein Song": {"best_accuracy": 12.0, "attempts": 1}}
        path = self._write_progress(payload)
        before = path.read_text(encoding="utf-8")

        migrate_legacy_names(tmp_path)
        assert path.read_text(encoding="utf-8") == before
        assert Path(progress_module.PROGRESS_FILE).exists()

    def test_a_malformed_progress_file_does_not_raise(self, tmp_path):
        path = self._write_progress("{das ist kein json")
        assert migrate_legacy_names(tmp_path) is False
        assert path.read_text(encoding="utf-8") == "{das ist kein json"

    def test_a_progress_file_that_is_not_an_object_does_not_raise(self, tmp_path):
        self._write_progress([1, 2, 3])
        assert migrate_legacy_names(tmp_path) is False

    def test_a_missing_progress_file_does_not_raise(self, tmp_path):
        from pickhero import progress as progress_module

        assert not Path(progress_module.PROGRESS_FILE).exists()
        assert migrate_legacy_names(tmp_path) is False

    def test_it_runs_only_once(self, tmp_path):
        self._write_progress(self.HISTORY)
        assert migrate_legacy_names(tmp_path) is True

        # A record written under an old key afterwards is not theirs; the
        # migration is done and must not touch it again.
        payload = self._read_progress()
        payload["01 Eine leere Saite"] = {"best_accuracy": 1.0, "attempts": 1}
        self._write_progress(payload)

        assert migrate_legacy_names(tmp_path) is False
        assert "01 Eine leere Saite" in self._read_progress()

    def test_the_old_files_are_deleted_and_nothing_else_is(self, tmp_path):
        folder = Path(tmp_path) / EXERCISE_DIRNAME
        folder.mkdir(parents=True)
        for stem in _LEGACY_STEMS:
            (folder / f"{stem}.gp5").write_bytes(b"alt")
        keep = folder / "Mein eigener Song.gp5"
        keep.write_bytes(b"meins")

        migrate_legacy_names(tmp_path)

        assert sorted(p.name for p in folder.glob("*.gp5")) == [keep.name]

    def test_a_songs_folder_that_does_not_exist_does_not_raise(self, tmp_path):
        assert migrate_legacy_names(tmp_path / "gibt-es-nicht") is False

    def test_a_tracker_already_in_memory_is_re_keyed_too(self, tmp_path):
        """The tracker is built before the migration runs, and its next save
        writes the whole dict. Without this, the old keys come straight back
        and the records the migration just moved are shadowed."""
        from pickhero.progress import ProgressTracker

        self._write_progress(self.HISTORY)
        tracker = ProgressTracker()
        assert tracker.get_best("01 Eine leere Saite") is not None

        migrate_legacy_names(tmp_path, tracker)

        assert tracker.get_best("01 Eine leere Saite") is None
        record = tracker.get_best("01 open_string")
        assert record is not None and record.attempts == 4

        tracker.record_result("01 open_string", {"accuracy_percent": 90.0})
        assert set(self._read_progress()) == {
            _LEGACY_STEMS[k] for k in self.HISTORY}

    def test_ensure_exercises_runs_it(self, tmp_path):
        self._write_progress(self.HISTORY)
        ensure_exercises(instruments.BASS_UKULELE, tmp_path)
        assert "01 open_string" in self._read_progress()


class TestStaleDetection:
    """The marker file can lie; the files on disk are the truth."""

    def test_a_marker_that_disagrees_with_the_files_triggers_a_rebuild(self, tmp_path):
        from pickhero.tabs.loader import list_tracks

        write_exercises(instruments.GUITAR, Path(tmp_path) / EXERCISE_DIRNAME)
        marker = Path(tmp_path) / EXERCISE_DIRNAME / ".instrument"
        marker.write_text("bass_ukulele", encoding="utf-8")

        assert ensure_exercises(instruments.BASS_UKULELE, tmp_path) is True
        for path in (Path(tmp_path) / EXERCISE_DIRNAME).glob("*.gp5"):
            assert list_tracks(path)[0]["tuning"] == instruments.BASS_UKULELE.tuning


class TestTuningMismatchIsVisible:
    """A tab written for another instrument must say so, not silently score 0."""

    def test_guitar_exercise_on_a_bass_profile_warns(self, tmp_path):
        import os
        os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
        import pygame
        pygame.init()
        pygame.display.set_mode((1280, 720))
        try:
            from pickhero.config import Config
            from pickhero.ui.scrolling import PlayingScreen

            paths = write_exercises(instruments.GUITAR, tmp_path)
            config = Config()
            config.instrument = "bass_ukulele"
            timeline = load_gp_file(paths[0], instrument=config.get_instrument())
            screen = PlayingScreen(timeline, config=config)
            assert screen._tuning_warning is not None
            assert "4" in screen._tuning_warning and "6" in screen._tuning_warning
        finally:
            pygame.quit()

    def test_matching_exercise_does_not_warn(self, tmp_path):
        import os
        os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
        import pygame
        pygame.init()
        pygame.display.set_mode((1280, 720))
        try:
            from pickhero.config import Config
            from pickhero.ui.scrolling import PlayingScreen

            paths = write_exercises(instruments.BASS_UKULELE, tmp_path)
            config = Config()
            config.instrument = "bass_ukulele"
            timeline = load_gp_file(paths[0], instrument=config.get_instrument())
            screen = PlayingScreen(timeline, config=config)
            assert screen._tuning_warning is None
            assert screen._lane_count == 4
        finally:
            pygame.quit()


class TestPersistenceCannotLoseData:
    """Saving must not silently drop values or take the app down."""

    def test_numpy_values_survive_a_round_trip(self):
        """The measured noise gate is a numpy float, and json.dump refuses it.

        The write failed after the temp file was opened and before the atomic
        replace, so the value vanished with no error anywhere. That is why the
        room measurement kept reading back as null.
        """
        import numpy as np
        from pickhero.config import Config

        config = Config()
        config.audio.noise_gate_db = np.float64(-33.5)
        config.tempo_factor = np.float32(0.75)
        assert config.save() is True

        loaded = Config.load()
        assert loaded.audio.noise_gate_db == pytest.approx(-33.5)
        assert loaded.tempo_factor == pytest.approx(0.75, abs=1e-6)

    def test_save_reports_failure_instead_of_raising(self, tmp_path, monkeypatch):
        """A file where the config directory should be, as on a locked profile."""
        from pickhero import config as config_module

        blocked = tmp_path / "not-a-dir"
        blocked.write_text("x", encoding="utf-8")
        monkeypatch.setattr(config_module, "CONFIG_DIR", blocked)
        monkeypatch.setattr(config_module, "CONFIG_FILE", blocked / "settings.json")
        assert config_module.Config().save() is False

    def test_one_bad_progress_record_does_not_lose_the_others(self):
        import json
        from pickhero import progress as progress_module
        from pickhero.progress import ProgressTracker

        progress_module.PROGRESS_FILE.parent.mkdir(parents=True, exist_ok=True)
        progress_module.PROGRESS_FILE.write_text(json.dumps({
            "good": {"best_accuracy": 72.0, "attempts": 3},
            "bad": {"best_accuracy": "not a number", "attempts": []},
            "also_good": {"best_accuracy": 51.0, "attempts": 1},
        }), encoding="utf-8")
        tracker = ProgressTracker()
        assert tracker.get_best("good") is not None
        assert tracker.get_best("also_good") is not None

    def test_a_progress_file_that_is_not_an_object_does_not_crash(self):
        import json
        from pickhero import progress as progress_module
        from pickhero.progress import ProgressTracker

        progress_module.PROGRESS_FILE.parent.mkdir(parents=True, exist_ok=True)
        progress_module.PROGRESS_FILE.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
        assert ProgressTracker().get_best("anything") is None


class TestDottedAndSixteenthNotes:
    """The writer knew four note values, and a figure needs more than four.

    A dotted quarter followed by an eighth is most of what makes a bass line
    move rather than count, and until these were here it could not be written
    at all -- so every generated exercise was a march by construction, which
    is a fair part of why they sound like exercises.
    """

    def test_a_dot_adds_half_again(self):
        from pickhero.exercises import (EIGHTH, HALF, QUARTER, SIXTEENTH,
                                        WHOLE, Step)

        for value in (WHOLE, HALF, QUARTER, EIGHTH, SIXTEENTH):
            plain = Step(1, 0, value).ticks
            dotted = Step(1, 0, value, dotted=True).ticks
            assert dotted == plain * 3 // 2, f"{value}: {plain} -> {dotted}"

    def test_a_bar_of_dotted_quarters_and_eighths_adds_up(self):
        from pickhero.exercises import EIGHTH, QUARTER, Step

        bar = [Step(4, 0, QUARTER, dotted=True), Step(4, 0, EIGHTH),
               Step(3, 0, QUARTER, dotted=True), Step(3, 0, EIGHTH)]
        assert sum(step.ticks for step in bar) == 4 * 960

    def test_a_dotted_note_survives_the_round_trip(self, tmp_path):
        """Written into a .gp5 and read back by the loader the app uses."""
        from pickhero import instruments
        from pickhero.exercises import (EIGHTH, QUARTER, Exercise, Step,
                                        _build_song)
        import guitarpro as gp

        bars = [[Step(4, 0, QUARTER, dotted=True), Step(4, 2, EIGHTH),
                 Step(4, 0, QUARTER), Step(4, 3, QUARTER)]] * 2
        exercise = Exercise(99, "test_dotted", 72, bars)
        song = _build_song(exercise, instruments.BASS_UKULELE, title="Test")
        path = tmp_path / "dotted.gp5"
        gp.write(song, str(path))

        back = gp.parse(str(path))
        beats = back.tracks[0].measures[0].voices[0].beats
        assert len(beats) == 4, [b.duration.value for b in beats]
        assert beats[0].duration.value == QUARTER
        assert beats[0].duration.isDotted, "the dot was lost on the way out"
        assert not beats[1].duration.isDotted

    def test_the_timeline_hears_the_dotted_length(self, tmp_path):
        """The app reads duration out of the file, so a dot that writes but
        does not read is a note the player is told to hold for a third less
        than it lasts."""
        from pathlib import Path

        from pickhero import instruments
        from pickhero.config import Config
        from pickhero.exercises import QUARTER, EIGHTH, Exercise, Step, _build_song
        from pickhero.tabs.loader import load_gp_file
        import guitarpro as gp

        bars = [[Step(4, 0, QUARTER, dotted=True), Step(4, 0, EIGHTH),
                 Step(4, 0, QUARTER), Step(4, 0, QUARTER)]] * 2
        song = _build_song(Exercise(99, "test_dotted", 60, bars),
                           instruments.BASS_UKULELE, title="Test")
        path = tmp_path / "dotted.gp5"
        gp.write(song, str(path))

        config = Config()
        config.instrument = "bass_ukulele"
        timeline = load_gp_file(Path(path), instrument=config.get_instrument())
        starts = [n.timestamp_ms for n in timeline.notes[:4]]
        # 60 BPM: a beat is 1000 ms, so a dotted quarter is 1500.
        assert abs(starts[1] - starts[0] - 1500) < 1, starts
        assert abs(starts[2] - starts[1] - 500) < 1, starts
