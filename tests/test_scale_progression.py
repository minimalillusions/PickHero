"""Playable beginner scales and safe migration of generated practice material."""
from dataclasses import replace

import pytest

from pickhero import exercises, i18n
from pickhero.exercises import EXERCISE_DIRNAME, QUARTER, Exercise, Step
from pickhero.instruments import BASS_GUITAR, BASS_UKULELE, GUITAR, with_tuning
from pickhero.library import content_identity
from pickhero.progress import ProgressTracker, SongRecord, song_key
from pickhero.tabs.loader import load_gp_file


@pytest.fixture(autouse=True)
def language():
    before = i18n.current_language()
    i18n.set_language("de")
    yield
    i18n.set_language(before)


def _steps(exercise):
    return [step for bar in exercise.bars for step in bar]


def _old_scale(instrument):
    """The shipped two-string exercise, independent of the revised builder."""
    low, second = instrument.strings_low_to_high()[:2]
    gap = instrument.tuning[second] - instrument.tuning[low]
    steps = [Step(low, interval) if interval <= 5 else Step(second, interval - gap)
             for interval in (0, 2, 4, 5, 7, 9, 11, 12)]
    bars = [steps[:4], steps[4:]]
    return Exercise(8, "scale_one_octave", 66,
                    (bars + [list(reversed(bar)) for bar in reversed(bars)]) * 2)


def _write_old_set(tmp_path, monkeypatch, change=None):
    build = exercises.build_exercises

    def old_build(instrument):
        result = build(instrument)
        result[7] = _old_scale(instrument)
        if change:
            change(result)
        return result

    folder = tmp_path / EXERCISE_DIRNAME
    with monkeypatch.context() as patch:
        patch.setattr(exercises, "build_exercises", old_build)
        paths = exercises.write_exercises(BASS_UKULELE, folder)
    (folder / ".instrument").write_text("bass_ukulele de", encoding="utf-8")
    return paths


def _store(tracker, path, instrument=BASS_UKULELE, *, by_path=False):
    key = song_key(path, instrument.key,
                   content_id=None if by_path else content_identity(path))
    tracker._data[key] = SongRecord(
        best_accuracy=95, best_hits=30, best_total=32, attempts=3,
        goal_pitch_met=True, goal_rhythm_met=True,
    )
    return key


@pytest.mark.parametrize("instrument", [GUITAR, BASS_GUITAR, BASS_UKULELE],
                         ids=lambda value: value.key)
def test_standard_scale_uses_familiar_frets_and_preserves_music(instrument, tmp_path):
    scale = exercises.build_exercises(instrument)[7]
    old = _old_scale(instrument)
    low, second, third = instrument.strings_low_to_high()[:3]
    assert [(step.string, step.fret) for step in _steps(scale)[:8]] == [
        (low, 0), (low, 2), (low, 4), (second, 0),
        (second, 2), (second, 4), (third, 1), (third, 2),
    ]
    assert max(step.fret for step in _steps(scale)) == 4
    assert scale.bpm == old.bpm == 66
    assert len(scale.bars) == len(old.bars) == 8
    assert [instrument.tuning[s.string] + s.fret for s in _steps(scale)] == [
        instrument.tuning[s.string] + s.fret for s in _steps(old)]
    assert [step.value for step in _steps(scale)] == [QUARTER] * 32
    written = exercises.write_exercises(instrument, tmp_path)[7]
    timeline = load_gp_file(written, track_index=0, instrument=instrument)
    assert len(timeline) == 32
    assert [(note.string, note.fret) for note in timeline.notes] == [
        (step.string, step.fret) for step in _steps(scale)]
    assert len({note.timestamp_ms for note in timeline.notes}) == 32


@pytest.mark.parametrize("tuning,max_fret", [
    ({1: 43, 2: 38, 3: 33, 4: 26}, 16),  # Drop D
    ({1: 52, 2: 40}, 12),               # Wide gap: old split made negative frets
    ({1: 40}, 12),                      # One string needs the full neck
    ({1: 52, 2: 40, 3: 45}, 8),         # Re-entrant string numbering
    ({1: 40, 2: 40, 3: 45, 4: 50}, 4), # Equal open pitches, bounded neck
])
def test_alternate_tunings_have_only_real_positions(tuning, max_fret, tmp_path):
    instrument = replace(with_tuning(GUITAR, tuning), max_fret=max_fret)
    scale = exercises.build_exercises(instrument)[7]
    ascending = _steps(scale)[:8]
    assert [tuning[step.string] + step.fret for step in ascending] == [
        min(tuning.values()) + interval for interval in (0, 2, 4, 5, 7, 9, 11, 12)]
    assert all(step.string in tuning and 0 <= step.fret <= max_fret for step in _steps(scale))
    written = exercises.write_exercises(instrument, tmp_path)[7]
    assert [note.midi_note for note in load_gp_file(written, track_index=0).notes] == [
        tuning[step.string] + step.fret for step in _steps(scale)]


def test_impossible_octave_is_rejected_instead_of_writing_unplayable_frets():
    instrument = replace(with_tuning(GUITAR, {1: 40}), max_fret=7)
    with pytest.raises(ValueError, match="no playable position"):
        exercises.build_exercises(instrument)


def test_old_version_rebuilds_scale_without_copying_its_goal(tmp_path, monkeypatch):
    paths = _write_old_set(tmp_path, monkeypatch)
    tracker = ProgressTracker()
    old_keys = [_store(tracker, path) for path in paths]
    assert exercises.ensure_exercises(BASS_UKULELE, tmp_path, tracker)
    assert not exercises.ensure_exercises(BASS_UKULELE, tmp_path, tracker)
    for index, path in enumerate(paths):
        new_key = song_key(path, BASS_UKULELE.key, content_id=content_identity(path))
        assert tracker.get_best(old_keys[index]).goal_rhythm_met
        if index == 7:
            assert new_key != old_keys[index]
            assert tracker.get_best(new_key) is None
            assert max(note.fret for note in load_gp_file(path, track_index=0).notes) == 4
        else:
            assert tracker.get_best(new_key).attempts == 3
            assert tracker.get_best(new_key).goal_rhythm_met
    assert "v" in (paths[0].parent / ".instrument").read_text(encoding="utf-8")


def test_path_history_binds_to_old_scale_before_library_sees_replacement(tmp_path, monkeypatch):
    path = _write_old_set(tmp_path, monkeypatch)[7]
    tracker = ProgressTracker()
    path_key = _store(tracker, path, by_path=True)
    old_id = content_identity(path)
    assert exercises.ensure_exercises(BASS_UKULELE, tmp_path, tracker)
    new_id = content_identity(path)
    tracker.migrate_content_identity(path, BASS_UKULELE.key, 0, new_id)
    assert tracker.get_best(song_key(path, BASS_UKULELE.key, content_id=new_id)) is None
    assert tracker.get_best(song_key(path, BASS_UKULELE.key, content_id=old_id)).goal_rhythm_met
    assert tracker.get_best(path_key).migrated_content_id == old_id


def test_changed_scale_archives_unbound_stem_and_preserves_existing_archive(tmp_path, monkeypatch):
    from types import SimpleNamespace

    path = _write_old_set(tmp_path, monkeypatch)[7]
    tracker = ProgressTracker()
    tracker._data[path.stem] = SongRecord(best_accuracy=95, attempts=3, goal_rhythm_met=True)
    archive_key = f"legacy-v1|{path.stem}"
    tracker._data[archive_key] = SongRecord(best_accuracy=75, attempts=2)
    assert exercises.ensure_exercises(BASS_UKULELE, tmp_path, tracker)
    tracker = ProgressTracker()
    tracker.migrate_legacy([SimpleNamespace(
        file=path, playable=True, track_index=0, content_id=content_identity(path),
    )], BASS_UKULELE.key)
    assert tracker.get_best(path.stem) is None
    assert tracker.get_best(archive_key).best_accuracy == 75
    assert tracker.get_best(f"{archive_key}|generated-1").best_accuracy == 95
    assert tracker.get_best(f"{archive_key}|generated-1").goal_rhythm_met
    assert tracker.get_best(song_key(path, BASS_UKULELE.key,
                                     content_id=content_identity(path))) is None


def test_language_rewrite_retains_goals_for_exactly_the_same_material(tmp_path):
    tracker = ProgressTracker()
    exercises.ensure_exercises(BASS_UKULELE, tmp_path, tracker)
    paths = [tmp_path / EXERCISE_DIRNAME / ex.filename
             for ex in exercises.build_exercises(BASS_UKULELE)]
    old_keys = [_store(tracker, path) for path in paths]
    i18n.set_language("en")
    assert exercises.ensure_exercises(BASS_UKULELE, tmp_path, tracker)
    for path, old_key in zip(paths, old_keys):
        new_key = song_key(path, BASS_UKULELE.key, content_id=content_identity(path))
        assert new_key != old_key
        assert tracker.get_best(old_key).goal_rhythm_met
        assert tracker.get_best(new_key).goal_rhythm_met
        assert tracker.get_best(new_key).attempts == 3


def test_different_tuning_under_same_profile_does_not_copy_goals(tmp_path):
    tracker = ProgressTracker()
    exercises.ensure_exercises(BASS_UKULELE, tmp_path, tracker)
    paths = [tmp_path / EXERCISE_DIRNAME / ex.filename
             for ex in exercises.build_exercises(BASS_UKULELE)]
    old_keys = [_store(tracker, path) for path in paths]
    drop_d = with_tuning(BASS_UKULELE, {1: 43, 2: 38, 3: 33, 4: 26})
    assert drop_d.key == BASS_UKULELE.key
    assert exercises.ensure_exercises(drop_d, tmp_path, tracker)
    for path, old_key in zip(paths, old_keys):
        new_key = song_key(path, drop_d.key, content_id=content_identity(path))
        assert tracker.get_best(old_key).goal_rhythm_met
        assert new_key != old_key
        assert tracker.get_best(new_key) is None


@pytest.mark.parametrize("change", [
    lambda items: setattr(items[5], "bpm", 72),
    lambda items: setattr(items[5].bars[0][1], "fret", 2),
])
def test_other_changed_material_never_inherits_its_previous_goal(tmp_path, monkeypatch, change):
    paths = _write_old_set(tmp_path, monkeypatch, change)
    path = paths[5]
    tracker = ProgressTracker()
    old_key = _store(tracker, path)
    assert exercises.ensure_exercises(BASS_UKULELE, tmp_path, tracker)
    new_key = song_key(path, BASS_UKULELE.key, content_id=content_identity(path))
    assert new_key != old_key
    assert tracker.get_best(old_key).goal_rhythm_met
    assert tracker.get_best(new_key) is None
