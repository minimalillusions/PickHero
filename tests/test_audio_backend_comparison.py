"""Backend experiments must preserve evidence and never alter the app globally."""
from dataclasses import asdict
import hashlib
import importlib.util
import json
from pathlib import Path
import wave

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("audio_backend_comparison", ROOT / "scripts/compare-audio-backend.py")
comparison = importlib.util.module_from_spec(spec)
spec.loader.exec_module(comparison)


@pytest.fixture
def bass_pluck():
    rate = 44100
    seconds = np.arange(round(.7 * rate)) / rate
    sound = sum(a * np.sin(2 * np.pi * 41.203445 * (i + 1) * seconds)
                for i, a in enumerate((1, .25, .125, .06))) * .15 * np.exp(-3 * seconds)
    sound[:221] *= np.linspace(0, 1, 221)
    sound[-221:] *= np.linspace(1, 0, 221)
    return sound.astype(np.float32), rate


@pytest.mark.parametrize("profile", ["bass_guitar", "bass_ukulele", "guitar"])
def test_profile_mode_retains_original_events(profile, bass_pluck):
    sound, rate = bass_pluck
    original, _ = comparison.benchmark.detect(sound, rate, profile)
    actual, _, settings = comparison.detect(sound, rate, profile, "profile")
    assert actual == original
    assert not settings["primary_replaced"]


@pytest.mark.parametrize("backend", ["yin", "yinfast"])
def test_bass_only_swap_preserves_other_profiles_and_global_state(backend, bass_pluck):
    from pickhero.audio.detector import PitchDetector
    from pickhero.audio.harmonic import HarmonicPitchDetector
    from pickhero.instruments import PROFILES

    saved = {key: asdict(profile) for key, profile in PROFILES.items()}
    sound, rate = bass_pluck
    _, settings = comparison.make_detector("bass_guitar", rate, backend)
    assert settings["primary_replaced"]
    assert settings["primary_method"] == backend
    assert settings["secondary_yinfast"]
    assert not settings["independent_primary_and_secondary_algorithms"]
    assert settings["actual_pitch_window"] == 4096
    for profile in ("guitar", "bass_ukulele"):
        original, _ = comparison.benchmark.detect(sound, rate, profile)
        found, _, configured = comparison.detect(sound, rate, profile, backend)
        assert found == original
        assert not configured["primary_replaced"]
    assert {key: asdict(profile) for key, profile in PROFILES.items()} == saved
    assert isinstance(PitchDetector(instrument=PROFILES["bass_guitar"])._pitch, HarmonicPitchDetector)


@pytest.mark.parametrize("backend", ["yin", "yinfast"])
def test_library_estimator_tracks_low_e_and_reset_recreates_clean_history(backend):
    detector, _ = comparison.make_detector("bass_guitar", 44100, backend)
    fresh, _ = comparison.make_detector("bass_guitar", 44100, backend)
    hop = detector.hop_size
    seconds = np.arange(hop * 30) / 44100
    old = (.2 * np.sin(2 * np.pi * 220 * seconds)).astype(np.float32)
    low_e = (.2 * np.sin(2 * np.pi * 41.203445 * seconds)).astype(np.float32)
    for offset in range(0, len(old), hop):
        detector.process(old[offset:offset + hop])
    detector.reset()
    for offset in range(0, len(low_e), hop):
        actual = detector.process(low_e[offset:offset + hop])
        expected = fresh.process(low_e[offset:offset + hop])
        assert actual == expected
    frequency, confidence = detector._pitch.process(low_e[-hop:])
    assert abs(1200 * np.log2(frequency / 41.203445)) < 5
    assert confidence > .9


def test_fft_window_is_not_silently_rounded_or_resampled():
    with pytest.raises(ValueError, match="4480 samples at 48000 Hz"):
        comparison.make_detector("bass_guitar", 48000, "yinfast")
    _, settings = comparison.make_detector("bass_guitar", 48000, "yin")
    assert settings["actual_pitch_window"] == settings["configured_pitch_window"] == 4480


@pytest.mark.parametrize("backend", ["yinfft", "unknown", "harmonic"])
def test_unsupported_backend_is_rejected_before_reading_input(tmp_path, backend):
    output = tmp_path / "report.json"
    with pytest.raises(ValueError, match="Unsupported"):
        comparison.run(tmp_path / "missing.json", output, backend)
    assert not output.exists()


def test_report_records_experiment_and_retains_duplicate_predictions(tmp_path, bass_pluck):
    sound, rate = bass_pluck
    audio = tmp_path / "note.wav"
    with wave.open(str(audio), "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(rate)
        target.writeframes(np.rint(sound * 32768).astype("<i2").tobytes())
    original = audio.read_bytes()
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"schema_version": 1, "files": [{
        "path": audio.name, "sha256": hashlib.sha256(original).hexdigest(),
        "profile": "bass_guitar", "kind": "single", "expected": [{"midi": 28}]}]}), encoding="utf-8")
    output = tmp_path / "report.json"
    result = comparison.run(manifest, output, "yinfast")
    record = result["records"][0]
    assert record["settings"]["primary_replaced"]
    assert record["metrics"]["correct"] == 1
    assert record["metrics"]["extra"] == len(record["detected"]) - 1
    assert "not independent" in result["limitations"]
    assert "scripts/compare-audio-backend.py" in result["source_sha256"]
    assert len(result["runtime"]["aubio_extension_sha256"]) == 64
    assert audio.read_bytes() == original
    saved = output.read_bytes()
    with pytest.raises(ValueError, match="already exists"):
        comparison.run(manifest, output, "profile")
    assert output.read_bytes() == saved
