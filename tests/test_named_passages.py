"""Named loops persist by content/track and compare only like conditions."""
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path

import pytest

from pickhero import progress as storage
from pickhero.config import Config
from pickhero.practice_advice import (
    filter_key_for_state,
    input_fingerprint,
    section_advice,
)
from pickhero.progress import MAX_SAVED_PASSAGES, ProgressTracker, song_key


def state(**changes):
    return dict({"song_path": str(Path("practice.gp5").resolve()), "instrument": "bass_ukulele",
                 "track_index": 0, "position_ms": 2500, "loop_start_ms": 1000,
                 "loop_end_ms": 5000, "loop_enabled": True, "tempo_factor": .5,
                 "active_strings": [True] * 4, "max_fret": 12, "wait_mode": False,
                 "chord_partial_credit": False}, **changes)


def identity(track=0, digest="a"):
    return song_key("practice.gp5", "bass_ukulele", track, content_id=digest * 64)


def attempt(**changes):
    return dict({"start_ms": 1000, "end_ms": 5000, "tempo_factor": .5,
                 "filter_key": filter_key_for_state(state()), "accuracy": 40, "timing_percent": 20,
                 "total": 8, "reliable": True, "uniform_tempo": True, "complete_loop": True,
                 "timing_assessed": True, "uniform_wait": True, "wait_mode": False,
                 "input_key": "a" * 64, "uniform_input": True,
                 "completed_at": "2026-10-03T10:00:00+00:00"}, **changes)


def record(tracker, key, **changes):
    entry = attempt(**changes)
    stats = {"accuracy_percent": entry["accuracy"], "timing_percent": entry["timing_percent"],
             "total": entry["total"], "reliable": entry["reliable"], "timing_assessed": entry["timing_assessed"]}
    tracker.record_section_result(key, stats, entry)


def test_named_loop_roundtrip_edit_delete_preserves_scores_and_snapshot():
    tracker = ProgressTracker()
    key = identity()
    source = state()
    identifier = tracker.save_passage(key, "  Refrain  ", source)
    source["active_strings"][0] = False
    fresh = ProgressTracker()
    item = fresh.saved_passages(key)[0]
    assert item["id"] == identifier and item["name"] == "Refrain"
    assert item["state"]["active_strings"] == [True] * 4
    assert item["state"]["position_ms"] == 1000
    item["state"]["tempo_factor"] = .75
    assert fresh.saved_passages(key)[0]["state"]["tempo_factor"] == .5
    fresh.record_result(key, {"accuracy_percent": 85, "total": 8, "hits": 7})
    assert fresh.rename_passage(key, identifier, "Refrain langsam")
    assert ProgressTracker().saved_passages(key)[0]["name"] == "Refrain langsam"
    assert fresh.delete_passage(key, identifier)
    assert not fresh.delete_passage(key, identifier)
    assert not ProgressTracker().saved_passages(key)
    assert ProgressTracker().get_best(key).best_accuracy == 85


def test_content_identity_follows_moved_file_but_never_another_track_or_content():
    tracker = ProgressTracker()
    key = identity()
    tracker.save_passage(key, "Refrain", state())
    tracker.migrate_content_identity("moved.gp5", "bass_ukulele", 0, "a" * 64)
    item = ProgressTracker().saved_passages(key)[0]
    assert item["state"]["song_path"] == str(Path("moved.gp5").resolve())
    assert not tracker.saved_passages(identity(track=1))
    assert not tracker.saved_passages(identity(digest="b"))
    with pytest.raises(ValueError, match="state"):
        tracker.save_passage(identity(track=1), "Wrong track", state())


@pytest.mark.parametrize("name", ["", "  ", "x" * 61, "a\nb", "a\x00b", None, 5])
def test_bad_names_are_rejected(name):
    with pytest.raises(ValueError, match="name"):
        ProgressTracker().save_passage(identity(), name, state())


@pytest.mark.parametrize("change", [{"loop_end_ms": 1000}, {"loop_enabled": False},
                                  {"tempo_factor": float("nan")}, {"wait_mode": "yes"},
                                  {"active_strings": [False] * 4}, {"max_fret": 100}])
def test_bad_passage_states_are_rejected(change):
    with pytest.raises(ValueError, match="state"):
        ProgressTracker().save_passage(identity(), "Refrain", state(**change))


def test_names_and_storage_are_bounded_and_loader_discards_malformed_items(monkeypatch):
    tracker = ProgressTracker()
    key = identity()
    first = tracker.save_passage(key, "Refrain", state())
    with pytest.raises(ValueError, match="duplicate"):
        tracker.save_passage(key, "REFRAIN", state())
    second = tracker.save_passage(key, "Strophe", state())
    with pytest.raises(ValueError, match="duplicate"):
        tracker.rename_passage(key, second, "refrain")
    for index in range(MAX_SAVED_PASSAGES - 2):
        tracker.save_passage(key, f"Loop {index}", state())
    with pytest.raises(ValueError, match="limit"):
        tracker.save_passage(key, "Noch eine", state())
    raw = asdict(tracker.get_best(key))
    raw["saved_passages"][0]["state"]["track_index"] = 5
    raw["saved_passages"][1]["id"] = "bad"
    raw["saved_passages"][2]["created_at"] = "not a date"
    monkeypatch.setattr(storage.jsonfile, "read", lambda *a, **k: {key: raw})
    fresh = ProgressTracker()
    assert len(fresh.saved_passages(key)) == MAX_SAVED_PASSAGES - 3
    assert all(item["id"] not in (first, second) for item in fresh.saved_passages(key))


def test_comparison_excludes_other_tempo_filters_wait_and_signal_errors():
    tracker = ProgressTracker()
    key = identity()
    passage = tracker.save_passage(key, "Refrain", state())
    record(tracker, key, accuracy=40)
    for change in ({"tempo_factor": .55}, {"filter_key": "other"}, {"wait_mode": True},
                   {"uniform_tempo": False}, {"reliable": False}, {"timing_assessed": False},
                   {"complete_loop": False}, {"uniform_wait": False}, {"start_ms": 0}, {"total": 4}):
        record(tracker, key, accuracy=100, **change)
    record(tracker, key, accuracy=75)
    comparison = ProgressTracker().passage_comparison(key, passage)
    assert [item["accuracy"] for item in comparison] == [40, 75]
    assert all(item["tempo_factor"] == .5 and item["completed_at"] for item in comparison)


def test_wait_comparison_shows_pitch_without_a_timing_claim():
    tracker = ProgressTracker()
    passage = tracker.save_passage(identity(), "Griffe", state(wait_mode=True))
    record(tracker, identity(), wait_mode=True, timing_assessed=False, accuracy=30)
    record(tracker, identity(), wait_mode=True, timing_assessed=False, accuracy=90)
    compared = tracker.passage_comparison(identity(), passage)
    assert [item["accuracy"] for item in compared] == [30, 90]
    assert all(not item["timing_assessed"] for item in compared)


def test_revision_changes_with_mutation_but_not_reading():
    tracker = ProgressTracker()
    assert tracker.revision == 0
    tracker.save_passage(identity(), "A", state())
    revision = tracker.revision
    assert revision > 0
    tracker.saved_passages(identity())
    assert tracker.revision == revision


@pytest.mark.parametrize("change", [{"reliable": False}, {"timing_assessed": False},
    {"uniform_tempo": False}, {"uniform_wait": False}, {"complete_loop": False},
    {"uniform_input": False}, {"input_key": "b" * 64}, {"input_key": None},
    {"tempo_factor": .6}, {"filter_key": "other"}, {"total": 3}, {"total": 9},
    {"accuracy": float("nan")}, {"timing_percent": float("inf")}])
def test_one_incomparable_or_unreliable_result_blocks_advice(change):
    assert section_advice([attempt(), attempt(**change), attempt()]) is None


def test_difficulty_advice_requires_repeated_problem_and_never_changes_settings():
    source = [attempt(accuracy=95, timing_percent=30) for _ in range(3)]
    before = deepcopy(source)
    assert section_advice(source)["target_tempo"] == .45
    assert source == before
    assert section_advice(source[:2]) is None
    assert section_advice([attempt(accuracy=95, timing_percent=95)] * 3)["target_tempo"] == .55
    assert section_advice([attempt(accuracy=80, timing_percent=70)] * 3) is None


def test_offers_survive_reload_and_prevent_immediate_tempo_oscillation():
    tracker = ProgressTracker()
    key = identity()
    for _ in range(3):
        record(tracker, key, accuracy=95, timing_percent=95)
    offered = tracker.claim_section_advice(key)
    assert offered["target_tempo"] == .55
    tracker = ProgressTracker()
    assert tracker.claim_section_advice(key) is None
    for _ in range(3):
        record(tracker, key, tempo_factor=.55)
    assert tracker.claim_section_advice(key) is None
    for _ in range(3):
        record(tracker, key, tempo_factor=.55)
    assert tracker.claim_section_advice(key)["target_tempo"] == .5


def test_mixed_input_settings_and_unknown_legacy_conditions_never_compare():
    tracker = ProgressTracker()
    passage = tracker.save_passage(identity(), "Griffe", state())
    record(tracker, identity(), accuracy=30)
    record(tracker, identity(), accuracy=40)
    record(tracker, identity(), accuracy=95, input_key="b" * 64)
    assert [item["accuracy"] for item in tracker.passage_comparison(identity(), passage)] == [95]
    assert tracker.passage_comparison(identity(), passage, input_key="c" * 64) == []
    assert [item["accuracy"] for item in tracker.passage_comparison(
        identity(), passage, input_key="a" * 64)] == [30, 40]
    record(tracker, identity(), accuracy=100, input_key=None)
    assert [item["accuracy"] for item in tracker.passage_comparison(identity(), passage)] == [95]


@pytest.mark.parametrize("field", ["gate", "device", "rate", "latency", "calibration", "window"])
def test_measurement_fingerprint_changes_with_audio_configuration(field):
    config = Config(instrument="bass_ukulele")
    previous = input_fingerprint(config, config.get_instrument())
    if field == "gate":
        config.audio.noise_gate_db = -40
    elif field == "device":
        config.audio.device_index = 3
    elif field == "rate":
        config.audio.sample_rate = 48000
    elif field == "latency":
        config.audio_latency_offset_ms = 20
    elif field == "window":
        config.timing_window_ms = 120
    else:
        config.calibration = {"instrument": "bass_ukulele", "strings": {"4": {"frequency": 42}}}
    assert input_fingerprint(config, config.get_instrument()) != previous


def test_mixed_problem_types_are_not_three_repeated_measurements():
    assert section_advice([attempt(accuracy=30, timing_percent=95),
                           attempt(accuracy=95, timing_percent=20),
                           attempt(accuracy=30, timing_percent=95)]) is None


@pytest.mark.parametrize("operation", ["save", "rename", "delete"])
def test_passage_write_failure_rolls_back_memory_and_preserves_previous_file(monkeypatch, operation):
    tracker = ProgressTracker()
    key = identity()
    identifier = tracker.save_passage(key, "Refrain", state())
    before = tracker.saved_passages(key)
    original = storage.PROGRESS_FILE.read_bytes()
    with monkeypatch.context() as patch:
        patch.setattr(Path, "replace", lambda *a, **k: (_ for _ in ()).throw(PermissionError("disk full")))
        with pytest.raises(ValueError, match="write"):
            if operation == "save":
                tracker.save_passage(key, "Solo", state())
            elif operation == "rename":
                tracker.rename_passage(key, identifier, "Solo")
            else:
                tracker.delete_passage(key, identifier)
    assert tracker.last_save_error == "write"
    assert tracker.saved_passages(key) == before
    assert storage.PROGRESS_FILE.read_bytes() == original
    assert not storage.PROGRESS_FILE.with_suffix(".tmp").exists()
    assert ProgressTracker().saved_passages(key) == before
    assert tracker.rename_passage(key, identifier, "Refrain 2")
    assert tracker.last_save_error is None


def test_capacity_guard_never_replaces_readable_progress_with_oversized_file(monkeypatch):
    tracker = ProgressTracker()
    key = identity()
    tracker.save_passage(key, "Refrain", state())
    before = storage.PROGRESS_FILE.read_bytes()
    monkeypatch.setattr(storage, "MAX_PROGRESS_BYTES", len(before) + 20)
    with pytest.raises(ValueError, match="capacity"):
        tracker.save_passage(key, "Solo", state())
    assert storage.PROGRESS_FILE.read_bytes() == before
    assert len(tracker.saved_passages(key)) == 1
    assert tracker.last_save_error == "capacity"
    assert not storage.PROGRESS_FILE.with_suffix(".tmp").exists()
    assert ProgressTracker().saved_passages(key)[0]["name"] == "Refrain"


def test_general_result_save_failure_is_visible_without_claiming_audio_fault(monkeypatch):
    tracker = ProgressTracker()
    monkeypatch.setattr(storage, "MAX_PROGRESS_BYTES", 1)
    assert tracker.record_result(identity(), {"accuracy_percent": 90, "hits": 9, "total": 10})
    assert tracker.last_save_error == "capacity"
    assert tracker.get_best(identity()).best_accuracy == 90
    assert not storage.PROGRESS_FILE.exists()


def test_verified_language_roundtrip_preserves_renames_and_does_not_resurrect_deleted_passages():
    tracker = ProgressTracker()
    german, english = identity(), identity(digest="b")
    identifier = tracker.save_passage(german, "Refrain", state())
    tracker.migrate_content_replacement("a" * 64, "b" * 64, "bass_ukulele")
    tracker.rename_passage(english, identifier, "Chorus")
    tracker.migrate_content_replacement("b" * 64, "a" * 64, "bass_ukulele")
    assert tracker.saved_passages(german)[0]["name"] == "Chorus"
    tracker.delete_passage(german, identifier)
    tracker.migrate_content_replacement("a" * 64, "b" * 64, "bass_ukulele")
    assert tracker.saved_passages(english) == []
