"""Reference review must be independent of labels and conservative about doubt."""
import copy
import importlib.util
import json
from pathlib import Path
import wave

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("audio_reference_checker", ROOT / "scripts/check-audio-references.py")
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)


def tone(midi=40, rate=44100, duration=1.4, partials=(1, .3, .1)):
    time = np.arange(round(rate * duration)) / rate
    frequency = 440 * 2 ** ((midi - 69) / 12)
    return (sum(amplitude * np.sin(2 * np.pi * frequency * (index + 1) * time)
                for index, amplitude in enumerate(partials)) * .15).astype(np.float32)


def assess(signal, nominal=40, rate=44100, windows=checker.DEFAULT_WINDOWS):
    return checker.check_single(signal, rate, nominal, checker.validate_windows(windows), dict(checker.SETTINGS))


@pytest.mark.parametrize("midi,rate", [(28, 44100), (40, 48000), (64, 44100), (88, 96000)])
def test_clear_stable_reference_is_consistent(midi, rate):
    result = assess(tone(midi, rate), nominal=midi, rate=rate)
    assert result["status"] == "consistent"
    assert result["observed_midi"] == pytest.approx(midi, abs=.05)
    assert all(window["fundamental_spectral_support"] for window in result["windows"])


@pytest.mark.parametrize("shift", [-1, 1])
def test_stable_semitone_difference_is_only_flagged_for_review(shift):
    result = assess(tone(40 + shift))
    assert result["status"] == "suspect"
    assert result["cents_from_nominal"] == pytest.approx(shift * 100, abs=3)
    assert result["nominal_midi"] == 40


def test_nominal_label_cannot_steer_period_or_spectral_estimates():
    signal = tone(40)
    correct = assess(signal, nominal=40)
    wrong = assess(signal, nominal=39)
    assert correct["windows"] == wrong["windows"]
    assert correct["observed_midi"] == wrong["observed_midi"]
    assert correct["status"] == "consistent" and wrong["status"] == "suspect"


@pytest.mark.parametrize("signal", [np.zeros(61740, np.float32), np.ones(61740, np.float32) * .1,
                                   np.random.default_rng(17).normal(0, .03, 61740).astype(np.float32),
                                   tone() * 1e-5, tone(duration=.4)])
def test_absent_noisy_quiet_or_short_audio_stays_uncertain(signal):
    result = assess(signal)
    assert result["status"] == "uncertain"
    assert result["usable_windows"] == 0


def test_pitch_changes_between_fixed_windows_are_not_label_certainty():
    signal = tone(40)
    other = tone(42)
    signal[round(.8 * 44100):round(1.0 * 44100)] = other[round(.8 * 44100):round(1.0 * 44100)]
    result = assess(signal)
    assert result["status"] == "uncertain"
    assert "pitch_changes_between_windows" in result["reasons"]


def test_missing_fundamental_is_reported_as_ambiguous_even_with_a_strong_period():
    result = assess(tone(40, partials=(0, 1, .8)))
    assert result["status"] == "uncertain"
    assert all(window["periodicity"] > .98 for window in result["windows"])
    assert all("fundamental_weak_missing_or_ambiguous" in window["reasons"] for window in result["windows"])


@pytest.mark.parametrize("rate", [44100, 48000, 96000, 192000])
@pytest.mark.parametrize("partial", [2, 3])
@pytest.mark.parametrize("fundamental_amplitude", [.1, .05, .01])
def test_weak_fundamental_and_dominant_partial_never_prove_an_octave_or_harmonic_label_error(
        rate, partial, fundamental_amplitude):
    partials = [fundamental_amplitude] + [0] * (partial - 2) + [1]
    signal = tone(40, rate, partials=partials)
    fundamental_label = assess(signal, nominal=40, rate=rate)
    dominant_label = assess(signal, nominal=round(40 + 12 * np.log2(partial)), rate=rate)
    assert fundamental_label["windows"] == dominant_label["windows"]
    assert fundamental_label["status"] == dominant_label["status"] == "uncertain"
    assert all("lower_fundamental_or_harmonic_identity_ambiguous" in window["reasons"]
               for window in fundamental_label["windows"])
    assert all(window["lower_period_alternatives"] for window in fundamental_label["windows"])


def test_half_semitone_boundary_does_not_claim_label_error():
    result = assess(tone(40.5))
    assert result["status"] == "uncertain"
    assert "difference_in_conservative_boundary_region" in result["reasons"]


def test_one_window_cannot_claim_stable_reference_agreement():
    result = assess(tone(), windows=((.6, .8),))
    assert result["status"] == "uncertain"
    assert "not_enough_independent_windows" in result["reasons"]


def test_one_clear_window_does_not_conceal_missing_requested_evidence():
    result = assess(tone(duration=.85))
    assert result["status"] == "uncertain"
    assert result["usable_windows"] == 1
    assert "not_all_windows_have_clear_pitch_evidence" in result["reasons"]


@pytest.mark.parametrize("windows", [[], [(0, .2)] * 17, [(0, .2), (.1, .3)],
                                    [(.5, .7), (.2, .4)], [(0, float("nan"))], [(False, .2)],
                                    [(0, float("inf"))], [(0, 2)], [(-1, 0)], [(0, .01)],
                                    [(599.9, 600.1)], [(0, .2, .4)]])
def test_invalid_or_repeated_windows_are_rejected(windows):
    with pytest.raises(ValueError):
        checker.validate_windows(windows)


def test_explicit_cli_windows_are_parsed_and_hashed_deterministically():
    windows = checker.parse_windows("0.6:0.8,0.8:1.0,1.0:1.2")
    assert windows == checker.validate_windows(checker.DEFAULT_WINDOWS)
    assert checker.canonical_hash(windows) == checker.canonical_hash(copy.deepcopy(windows))
    assert checker.canonical_hash(windows) != checker.canonical_hash([[.5, .7], [.8, 1], [1, 1.2]])


def write_pcm(path, signal, rate=44100):
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(rate)
        output.writeframes(np.rint(np.clip(signal, -1, 1) * 32767).astype("<i2").tobytes())


@pytest.fixture
def corpus(tmp_path):
    audio = tmp_path / "single.wav"
    sequence = tmp_path / "sequence.wav"
    write_pcm(audio, tone(39))
    write_pcm(sequence, np.zeros(44100, np.float32))
    files = [{"path": path.name, "sha256": checker.benchmark.digest(path), "profile": "bass_guitar",
              "kind": kind, "expected": expected} for path, kind, expected in (
        (audio, "single", [{"midi": 40}]),
        (sequence, "sequence", [{"midi": 28, "onset_s": .5}]))]
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"schema_version": 1, "files": files}), encoding="utf-8")
    return manifest, files


def test_complete_report_is_read_only_and_never_runs_detector(corpus, tmp_path, monkeypatch):
    manifest, files = corpus
    paths = [manifest, *(manifest.parent / entry["path"] for entry in files)]
    originals = {path: path.read_bytes() for path in paths}

    def forbidden(*args, **kwargs):
        pytest.fail("Reference checker must not call the app detector or score its output")

    monkeypatch.setattr(checker.benchmark, "detect", forbidden)
    monkeypatch.setattr(checker.benchmark, "score", forbidden)
    output = tmp_path / "evidence.json"
    result = checker.run(manifest, output)
    assert result["summary"] == {"files": 2, "consistent": 0, "suspect": 1, "uncertain": 0, "skipped": 1}
    assert result["records"][0]["expected"] == [{"midi": 40}]
    assert result["records"][1]["status"] == "skipped"
    assert "metrics" not in result["records"][0]
    assert result["manifest_sha256"] == checker.benchmark.digest(manifest)
    assert result["windows_sha256"] == checker.canonical_hash(result["windows_s"])
    assert result["settings_sha256"] == checker.canonical_hash(result["settings"])
    assert set(result["source_sha256"]) == {"check-audio-references.py", "benchmark-audio.py"}
    assert {path: path.read_bytes() for path in paths} == originals
    assert json.loads(output.read_text(encoding="utf-8")) == result
    saved = output.read_bytes()
    with pytest.raises(ValueError, match="already exists"):
        checker.run(manifest, output)
    assert output.read_bytes() == saved


def test_unsafe_manifest_reuses_loader_rejection(corpus, tmp_path):
    manifest, _ = corpus
    document = json.loads(manifest.read_text(encoding="utf-8"))
    document["files"][0]["path"] = "../outside.wav"
    manifest.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ValueError, match="within the manifest"):
        checker.run(manifest, tmp_path / "result.json")
    assert not (tmp_path / "result.json").exists()


def test_audio_changed_during_analysis_prevents_a_verified_report(corpus, tmp_path, monkeypatch):
    manifest, _ = corpus
    original = checker.check_single

    def mutate(*args, **kwargs):
        result = original(*args, **kwargs)
        write_pcm(tmp_path / "single.wav", tone(42))
        return result

    monkeypatch.setattr(checker, "check_single", mutate)
    with pytest.raises(ValueError, match="changed during"):
        checker.run(manifest, tmp_path / "result.json")
    assert not (tmp_path / "result.json").exists()


def test_oversized_manifest_is_rejected_before_parsing_or_unbounded_hashing(tmp_path, monkeypatch):
    manifest = tmp_path / "large.json"
    manifest.write_bytes(b" " * (4 * 1024 * 1024 + 1))

    def forbidden(*args, **kwargs):
        pytest.fail("Oversized manifest must be rejected during its bounded initial hash read")

    monkeypatch.setattr(checker.benchmark, "load_manifest", forbidden)
    with pytest.raises(ValueError, match="exceeds"):
        checker.run(manifest, tmp_path / "result.json")
    assert not (tmp_path / "result.json").exists()
