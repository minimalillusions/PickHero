"""Preserve supported preferences and history through validation/migration."""

import json
from pathlib import Path

import numpy as np
import pytest

from pickhero import config, i18n, instruments, library
from pickhero.audio.detector import PitchDetector
from pickhero.audio.input import AudioCapture
from pickhero.config import Config
from pickhero.progress import ProgressTracker, song_key


def write_settings(data):
    config.CONFIG_FILE.write_text(json.dumps(data), encoding="utf-8")


@pytest.mark.parametrize("code", ["nl", "fr-CA", "pt_BR"])
def test_custom_language_survives_restart_and_remains_selectable(code, monkeypatch):
    folder = config.CONFIG_DIR / "lang"
    folder.mkdir()
    (folder / f"{code}.json").write_text(json.dumps({
        "name": "Custom", "strings": {"menu.play": "CUSTOM PLAY"}}), encoding="utf-8")
    monkeypatch.setattr(i18n, "_catalogues", {})
    monkeypatch.setattr(i18n, "_names", {})
    monkeypatch.setattr(i18n, "_current", "de")
    original = Config(language=code)
    assert original.save()
    loaded = Config.load()
    assert loaded.language == code
    i18n.set_language(loaded.language)
    assert i18n.t("menu.play") == "CUSTOM PLAY"


@pytest.mark.parametrize("code", ["../evil", "de.foo", "a" * 33, "", [], 7])
def test_invalid_language_identifier_keeps_safe_defaults(code):
    write_settings({"language": code, "look": "stage"})
    loaded = Config.load()
    assert loaded.language == "de" and loaded.look == "stage"


@pytest.mark.parametrize("overrides", [
    {"buf_size": 128, "hop_size": 2048},
    {"buf_size": 4096, "hop_size": 2048},
    {"buf_size": 128},
])
def test_active_and_stashed_incompatible_windows_recover_without_losing_gate(overrides):
    settings = {**overrides, "noise_gate_db": -42.0, "confidence_threshold": 0.6}
    write_settings({"instrument": "guitar", "audio": settings,
                    "audio_by_instrument": {"bass_guitar": settings}})
    loaded = Config.load()
    for key in ["guitar", "bass_guitar", "guitar"]:
        loaded.switch_instrument(key)
        capture = AudioCapture(loaded)  # detector only; no stream is opened
        assert capture.detector.hop_size <= capture.detector.buf_size
        assert capture.detector.hop_size <= capture.detector.onset_buf_size
        assert loaded.audio.noise_gate_db == -42.0
        assert loaded.audio.confidence_threshold == 0.6


def test_in_memory_stash_is_checked_when_restored():
    settings = Config(audio_by_instrument={
        "bass_guitar": {"buf_size": 128, "hop_size": 2048, "noise_gate_db": -42}})
    settings.switch_instrument("bass_guitar")
    capture = AudioCapture(settings)
    assert capture.detector.hop_size <= capture.detector.onset_buf_size
    assert capture.detector.hop_size <= capture.detector.buf_size
    assert capture.detector.noise_gate_db == -42


@pytest.mark.parametrize("profile", list(instruments.PROFILES.values()),
                         ids=lambda profile: profile.key)
@pytest.mark.parametrize("rate", [44100, 48000, 192000])
def test_negotiated_sample_rates_keep_onset_and_pitch_windows_compatible(profile, rate):
    detector = PitchDetector(sample_rate=rate, instrument=profile)
    assert detector.hop_size <= detector.onset_buf_size
    assert detector.hop_size <= detector.buf_size
    assert detector.process(np.zeros(detector.hop_size, dtype=np.float32)) is None
    if rate == 44100:
        assert detector.hop_size == profile.hop_size
        assert detector.buf_size == profile.buf_size


def test_valid_overrides_and_calibration_survive_round_trip():
    data = {"instrument": "bass_guitar", "language": "en", "look": "hifi",
            "audio": {"device_index": 3, "sample_rate": 48000,
                      "buf_size": 8192, "hop_size": 1024, "noise_gate_db": -38},
            "active_strings": [True, False, True, False],
            "audio_by_instrument": {"guitar": {"noise_gate_db": -53}},
            "calibration": {"instrument": "bass_guitar", "strings": {
                "4": {"midi_note": 28, "frequency": 41.2, "noise_floor_db": -51}}}}
    write_settings(data)
    loaded = Config.load()
    assert loaded.save()
    restored = Config.load()
    for name in ["instrument", "language", "look", "active_strings", "calibration"]:
        assert getattr(restored, name) == data[name]
    for name, expected in data["audio"].items():
        assert getattr(restored.audio, name) == expected
    restored.switch_instrument("guitar")
    assert restored.audio.noise_gate_db == -53
    restored.switch_instrument("bass_guitar")
    assert restored.audio.buf_size == 8192 and restored.audio.hop_size == 1024


@pytest.mark.parametrize("profile", list(instruments.PROFILES.values()),
                         ids=lambda profile: profile.key)
def test_library_and_loaded_track_share_progress_identity(profile, tmp_path):
    import shutil
    from pickhero.tabs.loader import load_gp_file

    path = tmp_path / "demo.gp5"
    shutil.copyfile(Path(__file__).parent / "fixtures" / "Demo_v5.gp5", path)
    info = library.scan(tmp_path, profile)[0]
    timeline = load_gp_file(path, instrument=profile)
    assert info.track_index == timeline.metadata.track_index
    saved = song_key(path, profile.key, timeline.metadata.track_index)
    displayed = song_key(info.file, profile.key, info.track_index)
    assert saved == displayed


def test_legacy_migration_does_not_overwrite_newer_progress(tmp_path):
    song = library.SongInfo(path=str(tmp_path / "song.gp5"), track_index=1)
    key = song_key(song.file, "guitar", 1)
    tracker = ProgressTracker()
    tracker.record_result("song", {"accuracy_percent": 70})
    tracker.record_result(key, {"accuracy_percent": 90})
    tracker.migrate_legacy([song], "guitar")
    loaded = ProgressTracker()
    assert loaded.get_best(key).best_accuracy == 90
    assert loaded.get_best("legacy-v1|song").best_accuracy == 70


def test_studio_demo_uses_current_song_identity(tmp_path, monkeypatch):
    import pygame
    from pickhero.ui.menu import MenuScreen
    from pickhero.ui.paint_studio import _demo_page

    song = library.SongInfo(path=str(tmp_path / "song.gp5"), track_index=3)
    tracker = ProgressTracker()
    key = song_key(song.file, "guitar", 3)
    tracker.record_result(key, {"accuracy_percent": 80})
    monkeypatch.setattr(library, "scan", lambda *args, **kwargs: [song])
    screen = MenuScreen(tmp_path, config=Config(), progress=tracker)
    page = _demo_page(screen, pygame.Surface((1280, 720)))
    assert page.history[0][0] == 1
    assert page.scores[0] == 80
