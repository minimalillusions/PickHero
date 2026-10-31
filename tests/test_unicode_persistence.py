"""Valid JSON must not smuggle unrenderable Unicode into the UI."""

import json
import shutil
from pathlib import Path

import guitarpro
import pytest

from pickhero import config, instruments, library, progress
from pickhero.tabs.loader import load_gp_file
from pickhero.tabs.midi_import import MidiError, read_midi
from pickhero.validation import text
from tests.test_midi_import import midi_file, note, track_chunk, track_name


@pytest.mark.parametrize("invalid", ["\ud800", "\udfff", "bad\x00title"])
def test_invalid_text_is_refused_without_rejecting_valid_unicode(invalid):
    assert not text(invalid)
    assert text("Gitarre 🎸 – Bässe")


def test_surrogate_setting_preserves_neighbor_settings():
    config.CONFIG_FILE.write_text(json.dumps({
        "songs_dir": "\ud800", "look": "hifi", "language": "en",
    }), encoding="utf-8")
    loaded = config.Config.load()
    assert loaded.songs_dir == "songs"
    assert loaded.look == "hifi" and loaded.language == "en"
    assert loaded.save()


def test_surrogate_progress_fields_do_not_poison_other_history():
    progress.PROGRESS_FILE.write_text(json.dumps({
        "valid": {"attempts": 5, "best_accuracy": 95, "last_played": "\ud800"},
        "\udfff": {"attempts": 7, "best_accuracy": 100},
    }), encoding="utf-8")
    tracker = progress.ProgressTracker()
    tracker.record_result("another", {"accuracy_percent": 50})
    restored = progress.ProgressTracker()
    assert restored.get_best("valid").best_accuracy == 95
    assert restored.get_best("valid").attempts == 5
    assert restored.get_best("valid").last_played == ""
    assert restored.get_best("another").best_accuracy == 50
    assert restored.get_best("\udfff") is None


def test_surrogate_cached_title_is_reparsed_from_original_song(tmp_path):
    source = Path(__file__).parent / "fixtures" / "notes.gp5"
    target = tmp_path / source.name
    shutil.copyfile(source, target)
    original = library.scan(tmp_path, instruments.GUITAR)[0]
    cache = json.loads(library.CACHE_FILE.read_text(encoding="utf-8"))
    entry = next(iter(cache["entries"].values()))
    entry["title"] = "\ud800"
    library.CACHE_FILE.write_text(json.dumps(cache), encoding="utf-8")
    restored = library.scan(tmp_path, instruments.GUITAR)[0]
    assert restored.title == original.title
    assert restored.notes == original.notes
    assert restored.playable


def test_midi_name_with_nul_is_rejected_before_rendering(tmp_path):
    path = tmp_path / "invalid.mid"
    path.write_bytes(midi_file([track_chunk(track_name("bad\x00title") + note(40, 0, 480))]))
    with pytest.raises(MidiError, match="Spurname"):
        read_midi(path)


def test_legacy_name_with_nul_is_rejected_before_rendering(tmp_path):
    song = guitarpro.parse(str(Path(__file__).parent / "fixtures" / "notes.gp5"))
    song.title = "bad\x00title"
    path = tmp_path / "invalid.gp5"
    guitarpro.write(song, str(path))
    with pytest.raises(ValueError, match="metadata"):
        load_gp_file(path)
