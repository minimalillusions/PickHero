"""Input faults must never be confused with a player taking a rest."""
from types import SimpleNamespace

import numpy as np
import pytest

from pickhero.audio.input import AudioCapture
from pickhero.config import Config


@pytest.fixture
def live_capture(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("pickhero.audio.input.time.perf_counter", lambda: clock[0])
    capture = AudioCapture(Config())
    capture._stream = SimpleNamespace(active=True, stop=lambda: None, close=lambda: None)
    capture._start_time = clock[0]
    return capture, clock


def feed(capture, clock, amplitude=0.0, duration=0.02, overflow=False):
    frames = round(capture.detector.sample_rate * duration)
    clock[0] += duration
    capture._audio_callback(np.full((frames, 1), amplitude, np.float32), frames,
                            None, SimpleNamespace(input_overflow=overflow))


def test_startup_grace_then_missing_callbacks(live_capture):
    capture, clock = live_capture
    clock[0] += 0.7
    assert capture.health_issue() is None
    clock[0] += 0.1
    assert capture.health_issue() == "input_stalled"


def test_silence_is_valid_input_and_resume_clears_stall(live_capture):
    capture, clock = live_capture
    for _ in range(60):
        feed(capture, clock)
        assert capture.health_issue() is None
    clock[0] += 1
    assert capture.health_issue() == "input_stalled"
    feed(capture, clock)
    assert capture.health_issue() is None


def test_inactive_stream_and_intentional_stop(live_capture):
    capture, clock = live_capture
    clock[0] += 1
    capture._stream.active = False
    assert capture.health_issue() == "input_disconnected"
    capture.stop()
    assert capture.health_issue() is None


def test_overflow_remains_visible_between_ui_frames_then_recovers(live_capture):
    capture, clock = live_capture
    feed(capture, clock, overflow=True)
    assert capture.health_issue() == "input_overflow"
    for _ in range(60):
        feed(capture, clock)
    assert capture.health_issue() is None


def test_single_peak_is_not_sustained_clipping(live_capture):
    capture, clock = live_capture
    feed(capture, clock, amplitude=1.0)
    feed(capture, clock, amplitude=0.1)
    assert capture.health_issue() is None
    for _ in range(6):
        feed(capture, clock, amplitude=1.0)
    assert capture.health_issue() == "input_clipping"
    for _ in range(60):
        feed(capture, clock, amplitude=0.1)
    assert capture.health_issue() is None


def test_large_host_latency_gets_appropriate_callback_grace(live_capture):
    capture, clock = live_capture
    capture._input_latency_ms = 400
    clock[0] += 1
    assert capture.health_issue() is None
    clock[0] += 0.3
    assert capture.health_issue() == "input_stalled"


@pytest.mark.parametrize("amplitude", [0.999, 1.0])
def test_clean_full_scale_sine_is_not_clipping(live_capture, amplitude):
    capture, clock = live_capture
    rate = capture.detector.sample_rate
    frames = round(rate * .02)
    for block in range(12):
        phase = (np.arange(frames) + block * frames) / rate
        signal = (amplitude * np.sin(2 * np.pi * 110 * phase)).astype(np.float32)
        clock[0] += frames / rate
        capture._audio_callback(signal[:, None], frames, None, None)
        assert capture.health_issue() is None


def test_sustained_flattened_sine_is_clipping(live_capture):
    capture, clock = live_capture
    rate = capture.detector.sample_rate
    frames = round(rate * .02)
    for block in range(12):
        phase = (np.arange(frames) + block * frames) / rate
        signal = np.clip(1.15 * np.sin(2 * np.pi * 110 * phase), -1, 1).astype(np.float32)
        clock[0] += frames / rate
        capture._audio_callback(signal[:, None], frames, None, None)
    assert capture.health_issue() == "input_clipping"


def test_scored_fault_survives_slow_ui_frame(live_capture):
    capture, clock = live_capture
    capture.set_health_window(0)
    feed(capture, clock, overflow=True)
    # The audio thread keeps running while the UI is occupied for >1s.
    for _ in range(65):
        feed(capture, clock)
    assert capture.health_issue() == "input_overflow"


def test_recovered_count_in_fault_does_not_invalidate_later_play(live_capture):
    capture, clock = live_capture
    capture.set_health_window(1000)
    for _ in range(45):
        feed(capture, clock)
    feed(capture, clock, overflow=True)
    for _ in range(10):
        feed(capture, clock)
    assert capture.health_issue() is None
    feed(capture, clock, overflow=True)
    assert capture.health_issue() == "input_overflow"
