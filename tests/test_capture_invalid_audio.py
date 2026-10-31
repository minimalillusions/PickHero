"""Corrupt PCM is lost input, never silence or a clean practice measure."""

import json
from types import SimpleNamespace

import numpy as np
import pytest

from pickhero.audio import input as audio_input
from pickhero.audio.detector import DetectedNote
from pickhero.config import Config


class RecordingDetector:
    hop_size = 10
    sample_rate = 1000
    onset_latency_ms = 2.5
    last_signal_db = -20.0
    last_freq = 440.0
    last_confidence = 1.0

    def __init__(self):
        self.operations = []

    def process(self, chunk):
        assert chunk.dtype == np.float32 and np.isfinite(chunk).all()
        self.operations.append(chunk.copy())
        return DetectedNote(69, 440.0, 1.0, "A4", True, 1.25)

    def reset(self):
        self.operations.append("reset")


@pytest.fixture
def capture(monkeypatch):
    monkeypatch.setattr(audio_input.AudioCapture, "_make_detector",
                        lambda self, rate: RecordingDetector())
    clock = [100.1]
    monkeypatch.setattr(audio_input.time, "perf_counter", lambda: clock[0])
    result = audio_input.AudioCapture(Config())
    result._start_time = 100.0
    result._input_latency_ms = 100.0
    result._stream = SimpleNamespace(active=True, stop=lambda: None, close=lambda: None)
    result.test_clock = clock
    return result


def feed(capture, samples, *, offset=0, adc=False, status=False):
    info = (SimpleNamespace(inputBufferAdcTime=4 + offset / 1000,
                            currentTime=4 + capture.test_clock[0] - 100)
            if adc else None)
    values = np.asarray(samples)
    capture._audio_callback(values[:, None], len(values), info, status)


def valid_chunks(capture):
    return [entry for entry in capture.detector.operations if isinstance(entry, np.ndarray)]


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -float("inf"), 1e100])
def test_bad_full_hop_is_discarded_reset_and_diagnosed(capture, bad):
    capture._signal_db = -20.0
    capture._tuner_freq, capture._tuner_confidence = 440.0, 1.0
    capture._clipped_duration = .09
    feed(capture, [bad] * 10)
    assert capture.detector.operations == ["reset"]
    assert capture.get_notes() == []
    assert capture.get_signal_db() == -120.0
    assert capture.get_tuner_data() == (0.0, 0.0)
    assert capture._clipped_duration == 0.0
    assert capture._last_clipping_time is None
    assert capture.health_issue() == "input_overflow"
    assert capture.dropped_blocks == 1
    assert capture.last_status == "Invalid audio samples"
    assert capture._next_input_ms == pytest.approx(10)
    assert capture.diagnostic_snapshot()["context"]["health_issue"] == "input_overflow"
    json.dumps(capture.timing_diagnostics(), allow_nan=False)


@pytest.mark.parametrize("prefix", [0, 10, 20])
def test_partial_bad_tail_is_discarded_immediately_after_valid_prefix(capture, prefix):
    data = np.full(prefix + 6, .1, np.float32)
    data[-2] = np.nan
    feed(capture, data)
    assert len(valid_chunks(capture)) == prefix // 10
    assert capture.detector.operations[-1] == "reset"
    assert capture._tail.size == 0
    assert capture.health_issue() == "input_overflow"
    assert capture.dropped_blocks == 1
    assert len(capture.get_notes()) == prefix // 10
    # The six discarded samples still occupy six milliseconds in the clock.
    capture.test_clock[0] += .4
    feed(capture, np.full(10, .2, np.float32))
    assert capture.get_notes()[0].timestamp_ms == pytest.approx(prefix + 1)
    np.testing.assert_array_equal(valid_chunks(capture)[-1], np.full(10, .2, np.float32))


@pytest.mark.parametrize("adc", [True, False])
def test_multi_hop_gap_keeps_good_samples_and_fractional_external_clock(capture, adc):
    original = np.arange(50, dtype=np.float32) / 100
    feed(capture, original[:6], adc=adc)
    assert capture.detector.operations == []
    corrupt = original[6:40].copy()
    corrupt[5] = np.inf
    capture.test_clock[0] = 100.75
    feed(capture, corrupt, offset=6, adc=adc)
    capture.test_clock[0] = 100.9
    feed(capture, original[40:], offset=40, adc=adc)
    operations = capture.detector.operations
    assert len(operations) == 5 and operations[1] == "reset"
    for actual, expected in zip(valid_chunks(capture),
                                [original[:10], original[20:30], original[30:40], original[40:]]):
        np.testing.assert_array_equal(actual, expected)
    assert [item.timestamp_ms for item in capture.get_notes()] == pytest.approx([-5, 15, 25, 35])
    assert capture._next_input_ms == pytest.approx(50)
    assert capture.timing_diagnostics()["clock_source"] == ("adc" if adc else "fallback")
    assert capture.dropped_blocks == 1


@pytest.mark.parametrize("tail_bad", [True, False])
def test_multiple_invalid_hops_count_one_bad_callback_and_preserve_only_valid_tail(capture, tail_bad):
    values = np.full(26, .2, np.float32)
    values[0] = np.nan
    values[15] = -np.inf
    if tail_bad:
        values[-1] = np.nan
    feed(capture, values)
    assert capture.dropped_blocks == 1
    assert capture.get_notes() == []
    assert capture.detector.operations == ["reset"] * (3 if tail_bad else 2)
    assert capture._tail.size == (0 if tail_bad else 6)
    capture.test_clock[0] += .01
    feed(capture, np.full(10 if tail_bad else 4, .3, np.float32))
    assert capture.get_notes()[0].timestamp_ms == pytest.approx(21 if tail_bad else 15)
    expected = [.3] * 10 if tail_bad else [.2] * 6 + [.3] * 4
    np.testing.assert_array_equal(valid_chunks(capture)[0], np.array(expected, np.float32))


def test_real_driver_overflow_and_invalid_pcm_are_counted_once(capture):
    feed(capture, np.full(6, .2, np.float32))
    capture.test_clock[0] = 100.5
    feed(capture, np.full(10, np.nan, np.float32),
         status=SimpleNamespace(input_overflow=True))
    assert capture.dropped_blocks == 1
    assert "input_overflow=True" in capture.last_status
    assert capture.last_status.endswith("Invalid audio samples")
    assert capture._tail.size == 0
    # Only the actual driver overflow reanchors the fallback clock.
    feed(capture, np.full(10, .2, np.float32))
    assert capture.get_notes()[0].timestamp_ms == pytest.approx(405)


def test_corrupt_pcm_remains_unreliable_for_scored_attempt_but_not_next_one(capture):
    capture.set_health_window(0)
    feed(capture, [np.nan])
    capture.test_clock[0] += 10.0
    feed(capture, np.zeros(10, np.float32))
    assert capture.health_issue() == "input_overflow"
    capture.set_health_window((capture.test_clock[0] - capture._start_time) * 1000)
    assert capture.health_issue() is None
    capture.test_clock[0] += .02
    feed(capture, [np.inf])
    assert capture.health_issue() == "input_overflow"


def test_corrupt_count_in_is_excluded_from_later_assessment(capture):
    capture.set_health_window(1000)
    feed(capture, [np.nan])
    capture.test_clock[0] = 101.1
    feed(capture, np.zeros(10, np.float32))
    assert capture.health_issue() is None


def test_start_resets_invalid_diagnostics_and_releases_prior_stream(capture, monkeypatch):
    feed(capture, [np.inf])
    monkeypatch.setattr(audio_input, "best_input_device", lambda: 0)
    monkeypatch.setattr(audio_input, "negotiate_samplerate", lambda *args: 1000)
    stream = SimpleNamespace(active=True, latency=.1, start=lambda: None,
                             stop=lambda: None, close=lambda: None)
    monkeypatch.setattr(audio_input.sd, "InputStream", lambda **kwargs: stream)
    capture.start()
    assert capture.health_issue() is None
    assert capture.dropped_blocks == 0 and capture.last_status == ""
    assert capture._tail.size == 0
    assert capture._next_input_ms is None
    capture.stop()
