"""Malformed fields and song identity must not erase legitimate progress."""
import json
import shutil
import struct
from pathlib import Path

import pytest

from pickhero import config, instruments, jsonfile, library, progress
from pickhero.config import Config
from pickhero.progress import ProgressTracker, song_key


@pytest.mark.parametrize("bad", [
    {"theme": []}, {"songs_dir": 42}, {"instrument": ["guitar"]},
    {"tempo_factor": float("nan")}, {"tempo_factor": 10 ** 400},
    {"active_strings": "false"}, {"active_strings": [False] * 6},
    {"calibration": []}, {"audio_by_instrument": {"guitar": []}},
    {"audio": {"sample_rate": 0, "buf_size": 7, "hop_size": "512"}},
    {"audio": {"buf_size": 128, "hop_size": 2048}},
    {"display": {"width": -1, "height": [], "hit_zone_fraction": float("inf")}},
])
def test_invalid_setting_field_preserves_other_settings(bad):
    config.CONFIG_FILE.write_text(json.dumps({"look": "hifi", "language": "en", **bad}), encoding="utf-8")
    found = Config.load()
    assert found.look == "hifi"
    assert found.language == "en"
    assert isinstance(found.songs_dir, str)
    assert found.get_instrument().key in instruments.PROFILES
    assert 0.1 <= found.tempo_factor <= 2
    assert isinstance(found.calibration, dict)
    assert all(type(active) is bool for active in found.active_strings)
    assert any(found.active_strings)
    assert found.display.width > 0
    assert found.audio.sample_rate > 0
    assert (found.audio.hop_size or found.get_instrument().hop_size) <= (found.audio.buf_size or found.get_instrument().buf_size)


def test_valid_calibration_survives_bad_neighbor():
    good = {"midi_note": 40, "frequency": 82.4, "noise_floor_db": -50}
    config.CONFIG_FILE.write_text(json.dumps({
        "instrument": "guitar", "audio": {"device_index": 2, "noise_gate_db": -41},
        "calibration": {"strings": {"1": good, "2": {"frequency": "broken"}}},
    }), encoding="utf-8")
    found = Config.load()
    assert found.audio.device_index == 2
    assert found.audio.noise_gate_db == -41
    assert found.get_string_calibration(1).frequency == 82.4
    assert found.get_string_calibration(2) is None


def test_progress_field_errors_are_local_and_future_results_still_save():
    progress.PROGRESS_FILE.write_text(json.dumps({
        "bad": {"attempts": "1", "best_accuracy": float("nan"),
                "section_history": [None], "tempo_history": [{"attempt": 1, "accuracy": []}]},
        "good": {"attempts": 8, "best_accuracy": 92},
    }), encoding="utf-8")
    tracker = ProgressTracker()
    tracker.record_detailed_result("bad", {"accuracy_percent": 60}, [], 0.75)
    assert tracker.get_best("bad").attempts == 1
    assert tracker.get_best("good").best_accuracy == 92
    assert ProgressTracker().get_best("bad").best_accuracy == 60


def test_paths_instruments_and_tracks_have_distinct_histories(tmp_path):
    keys = [song_key(tmp_path / folder / "song.gp5", instrument, track)
            for folder in ("one", "two") for instrument in ("guitar", "bass_guitar") for track in (0, 1)]
    assert len(set(keys)) == 8
    tracker = ProgressTracker()
    tracker.record_result(keys[0], {"accuracy_percent": 100})
    assert all(tracker.get_best(key) is None for key in keys[1:])


def test_legacy_migration_preserves_original_and_happens_once(tmp_path):
    song = library.SongInfo(path=str(tmp_path / "song.gp5"), track_index=2)
    tracker = ProgressTracker()
    tracker.record_result("song", {"accuracy_percent": 70})
    tracker.migrate_legacy([song], "guitar")
    target = song_key(song.file, "guitar", 2)
    assert tracker.get_best(target).best_accuracy == 70
    tracker.record_result(target, {"accuracy_percent": 100})
    assert tracker.get_best("legacy-v1|song").best_accuracy == 70
    tracker.migrate_legacy([song], "bass_guitar")
    assert tracker.get_best(song_key(song.file, "bass_guitar", 2)) is None
    assert ProgressTracker().get_best(target).best_accuracy == 100


def test_ambiguous_legacy_name_is_preserved_without_guessing(tmp_path):
    songs = [library.SongInfo(path=str(tmp_path / folder / "song.gp5")) for folder in ("a", "b")]
    tracker = ProgressTracker()
    tracker.record_result("song", {"accuracy_percent": 80})
    tracker.migrate_legacy(songs, "guitar")
    assert tracker.get_best("song").best_accuracy == 80
    assert all(tracker.get_best(song_key(song.file, "guitar")) is None for song in songs)


@pytest.mark.parametrize("bad", ["not an object", {"mtime": "invalid"},
                                      {"mtime": float("nan")}, {"track_name": []}])
def test_one_corrupt_cache_entry_is_reparsed(tmp_path, bad):
    target = tmp_path / "notes.gp5"
    shutil.copyfile(Path(__file__).parent / "fixtures" / "notes.gp5", target)
    first = library.scan(tmp_path, instruments.GUITAR)
    cached = json.loads(library.CACHE_FILE.read_text(encoding="utf-8"))
    key = next(iter(cached["entries"]))
    cached["entries"][key] = bad if isinstance(bad, str) else {**cached["entries"][key], **bad}
    library.CACHE_FILE.write_text(json.dumps(cached), encoding="utf-8")
    second = library.scan(tmp_path, instruments.GUITAR)
    assert len(second) == 1
    assert second[0].notes == first[0].notes
    assert second[0].playable


def test_midi_files_appear_in_the_browser_index(tmp_path):
    track = b"\x00\x90\x40\x50\x83\x60\x80\x40\x00\x00\xff\x2f\x00"
    (tmp_path / "one.mid").write_bytes(b"MThd" + struct.pack(">IHHH", 6, 0, 1, 480)
                                     + b"MTrk" + struct.pack(">I", len(track)) + track)
    songs = library.scan(tmp_path, instruments.GUITAR)
    assert len(songs) == 1 and songs[0].playable and songs[0].notes == 1


def test_oversized_json_is_kept_for_recovery(tmp_path, monkeypatch):
    monkeypatch.setattr(jsonfile, "MAX_JSON_BYTES", 8)
    path = tmp_path / "large.json"
    raw = b'{"data":"' + b"x" * 50 + b'"}'
    path.write_bytes(raw)
    assert jsonfile.read(path) is None
    saved = list(tmp_path.glob("large.json.unlesbar-*"))
    assert len(saved) == 1 and saved[0].read_bytes() == raw
