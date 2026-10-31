"""The offline audio audit must retain false alarms and immutable evidence."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import wave

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("audio_benchmark", ROOT / "scripts/benchmark-audio.py")
benchmark = importlib.util.module_from_spec(spec)
spec.loader.exec_module(benchmark)


def write_pcm(path, values, width=2, channels=1, rate=44100):
    values = np.asarray(values, dtype=np.int64)
    if width == 3:
        data = np.array([(value >> shift) & 255 for value in values for shift in (0, 8, 16)], np.uint8).tobytes()
    elif width == 1:
        data = values.astype(np.uint8).tobytes()
    else:
        data = values.astype(f"<i{width}").tobytes()
    with wave.open(str(path), "wb") as output:
        output.setnchannels(channels)
        output.setsampwidth(width)
        output.setframerate(rate)
        output.writeframes(data)


@pytest.fixture
def corpus(tmp_path):
    audio = tmp_path / "note.wav"
    write_pcm(audio, [0] * 100)
    entry = {"path": "note.wav", "sha256": benchmark.digest(audio),
             "profile": "bass_guitar", "kind": "single", "expected": [{"midi": 28}],
             "groups": {"style": "finger", "fret": 0}}
    document = {"schema_version": 1, "files": [entry]}
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path, document


@pytest.mark.parametrize("width", [1, 2, 3, 4])
def test_pcm_signs_and_amplitude_are_preserved(tmp_path, width):
    limit = 2 ** (8 * width - 1)
    values = [0, limit // 4, -limit // 2, limit - 1, -limit]
    stored = [value + 128 for value in values] if width == 1 else values
    path = tmp_path / "test.wav"
    write_pcm(path, stored, width)
    signal, rate = benchmark.load_pcm(path, benchmark.digest(path))
    np.testing.assert_allclose(signal, np.array(values) / limit, atol=1e-7)
    assert rate == 44100


def test_stereo_is_averaged_without_peak_normalization(tmp_path):
    path = tmp_path / "test.wav"
    write_pcm(path, [8192, 0, -8192, 8192], channels=2)
    signal, _ = benchmark.load_pcm(path, benchmark.digest(path))
    np.testing.assert_array_equal(signal, [.125, 0])


@pytest.mark.parametrize("field,value", [
    ("profile", "unknown"), ("kind", "unknown"), ("sha256", "0" * 64),
    ("path", "../outside.wav"), ("expected", []),
    ("expected", [{"midi": True}]), ("expected", [{"midi": 128}]),
    ("expected", [{"midi": 28}, {"midi": 33}]), ("groups", []),
])
def test_bad_manifest_is_rejected_before_detection(corpus, field, value):
    path, document = corpus
    document["files"][0][field] = value
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ValueError):
        benchmark.load_manifest(path)


def test_duplicate_paths_are_not_counted_as_separate_evidence(corpus):
    path, document = corpus
    document["files"].append(copy.deepcopy(document["files"][0]))
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ValueError, match="Duplicate"):
        benchmark.load_manifest(path)


@pytest.mark.parametrize("notes", [
    [{"midi": 28}], [{"midi": 28, "onset_s": float("nan")}],
    [{"midi": 28, "onset_s": -1}], [{"midi": 28, "onset_s": True}],
    [{"midi": 28, "onset_s": 1}, {"midi": 28, "onset_s": .5}],
])
def test_sequence_annotations_need_valid_ordered_timestamps(corpus, notes):
    path, document = corpus
    document["files"][0].update(kind="sequence", expected=notes)
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ValueError, match="onset"):
        benchmark.load_manifest(path)


def test_recording_mutation_between_validation_and_read_is_detected(corpus):
    path, document = corpus
    benchmark.load_manifest(path)
    audio = path.parent / "note.wav"
    write_pcm(audio, [100] * 100)
    with pytest.raises(ValueError, match="changed"):
        benchmark.load_pcm(audio, document["files"][0]["sha256"])


def test_oversized_manifest_is_rejected_before_initial_hash_or_detection(tmp_path, monkeypatch):
    path = tmp_path / "large.json"
    path.write_bytes(b" " * (4 * 1024 * 1024 + 1))

    def forbidden(*args, **kwargs):
        pytest.fail("Oversized manifest must not reach decoding or detection")

    monkeypatch.setattr(benchmark, "load_manifest", forbidden)
    with pytest.raises(ValueError, match="size limit"):
        benchmark.run(path, tmp_path / "result.json")
    assert not (tmp_path / "result.json").exists()


def test_pcm_size_limit_also_applies_after_manifest_was_validated(corpus, monkeypatch):
    path, document = corpus
    benchmark.load_manifest(path)
    audio = path.parent / "note.wav"
    monkeypatch.setattr(benchmark, "MAX_FILE_BYTES", audio.stat().st_size)
    # File grows between manifest validation and the guarded PCM read.
    with audio.open("ab") as output:
        output.write(b"x")
    with pytest.raises(ValueError, match="size limit"):
        benchmark.load_pcm(audio, document["files"][0]["sha256"])


def event(midi, onset=0):
    return {"midi": midi, "onset_s": onset}


@pytest.mark.parametrize("pitches,correct,extra,strict", [
    ([], 0, 0, 0), ([28], 1, 0, 1), ([40], 0, 1, 0),
    ([28, 28], 1, 1, 0), ([40, 28], 1, 1, 0), ([28, 47, 48], 1, 2, 0),
])
def test_single_note_metric_never_hides_double_or_wrong_notes(pitches, correct, extra, strict):
    result = benchmark.score({"kind": "single", "expected": [{"midi": 28}]},
                             [event(midi) for midi in pitches])
    assert result == {"notes": 1, "correct": correct, "missed": 1 - correct,
                      "extra": extra, "exactly_one_correct": strict}


def test_sequence_match_is_one_to_one_and_not_greedy():
    expected = [event(28, 0), event(28, .11)]
    detected = [event(28, .08), event(28, -.1)]
    assert benchmark.match_events(expected, detected, .1) == {0: 1, 1: 0}
    assert len(benchmark.match_events(expected, detected[:1], .15)) == 1
    assert benchmark.match_events(expected, [event(40, 0)], .15) == {}


def test_sequence_windows_and_error_sign_are_separate():
    result = benchmark.score({"kind": "sequence", "expected": [event(28, 1)]},
                             [event(28, 1.1), event(40, 1)])
    assert result["50"]["correct"] == 0 and result["50"]["extra"] == 2
    assert result["150"]["correct"] == 1 and result["150"]["extra"] == 1
    assert result["150"]["onset_errors_ms"] == pytest.approx([100])


def test_grouped_results_keep_hard_categories_and_denominators():
    base = {"profile": "bass_guitar", "kind": "single"}
    records = [{**base, "groups": {"style": "finger"}, "metrics": benchmark.score(
        {"kind": "single", "expected": [event(28)]}, found)} for found in ([event(28)], [])]
    records.append({**base, "groups": {"style": "mute"}, "metrics": benchmark.score(
        {"kind": "single", "expected": [event(28)]}, [event(40)])})
    totals = benchmark.summarize(records)
    all_notes = next(item for item in totals if item["group"] == "all")
    muted = next(item for item in totals if item["value"] == "mute")
    assert all_notes["notes"] == 3 and all_notes["correct"] == 1 and all_notes["extra"] == 1
    assert muted["notes"] == 1 and muted["correct"] == 0 and muted["extra"] == 1


def test_end_to_end_real_detector_preserves_files_and_refuses_report_overwrite(tmp_path):
    pytest.importorskip("aubio")
    rate = 44100
    time = np.arange(rate // 2) / rate
    tone = sum(a * np.sin(2 * np.pi * 41.203445 * (i + 1) * time)
               for i, a in enumerate((1, .25, .125, .06))) * .15 * np.exp(-3 * time)
    tone[:221] *= np.linspace(0, 1, 221)
    tone[-221:] *= np.linspace(1, 0, 221)
    signal = np.concatenate([np.zeros(rate // 2), tone, np.zeros(rate // 2)])
    path = tmp_path / "pluck.wav"
    write_pcm(path, np.rint(signal * 32768).astype(np.int16))
    original = path.read_bytes()
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"schema_version": 1, "files": [{
        "path": "pluck.wav", "sha256": hashlib.sha256(original).hexdigest(),
        "profile": "bass_guitar", "kind": "sequence", "expected": [event(28, .5)]}]}), encoding="utf-8")
    output = tmp_path / "result.json"
    result = benchmark.run(manifest, output)
    assert result["records"][0]["metrics"]["150"]["correct"] == 1
    assert result["records"][0]["metrics"]["150"]["extra"] == 0
    assert path.read_bytes() == original
    saved = output.read_bytes()
    with pytest.raises(ValueError, match="already exists"):
        benchmark.run(manifest, output)
    assert output.read_bytes() == saved
