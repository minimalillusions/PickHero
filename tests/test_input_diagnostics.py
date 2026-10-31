"""A score investigation needs the actual capture settings, without raw audio."""
import json

from pickhero.audio.input import AudioCapture
from pickhero.config import Config


def test_diagnostics_follow_effective_gate_and_input_failures():
    config = Config(instrument="bass_ukulele")
    config.audio.noise_gate_db = -33
    capture = AudioCapture(config)
    capture.device_index = 7
    # A live detector setting may differ from the saved configuration.
    capture.detector.set_noise_gate_db(-39)
    capture.dropped_blocks = 3
    capture.last_status = "input overflow"
    data = capture.timing_diagnostics()
    assert data["noise_gate_db"] == -39
    assert config.audio.noise_gate_db == -33
    assert data["device_index"] == 7
    assert data["dropped_blocks"] == 3
    assert data["input_status"] == "input overflow"
    assert all(value is None or isinstance(value, (str, int, float)) for value in data.values())
    json.dumps(data)


def test_callback_metrics_do_not_shift_note_timestamps(monkeypatch):
    from types import SimpleNamespace
    import numpy as np
    import pytest
    from pickhero.audio.detector import DetectedNote
    from pickhero.audio import input as module

    capture = AudioCapture(Config())
    capture._start_time = 100.0
    capture.detector = SimpleNamespace(hop_size=10, sample_rate=1000,
        last_signal_db=-20, last_freq=440, last_confidence=1,
        onset_latency_ms=2,
        process=lambda chunk: DetectedNote(69, 440, 1, "A4", True, 1))
    monkeypatch.setattr(module.time, "perf_counter", lambda: 100.05)
    # Three detections, callback finish, then three queue removals.
    values = iter([0, 1_000_000, 2_000_000, 3_000_000, 4_000_000,
                   11_000_000, 12_000_000, 13_000_000])
    monkeypatch.setattr(module.time, "perf_counter_ns", lambda: next(values))
    capture._audio_callback(np.zeros((30, 1), dtype=np.float32), 30,
        SimpleNamespace(inputBufferAdcTime=9.96, currentTime=10), False)
    notes = capture.get_notes()
    assert [note.timestamp_ms for note in notes] == pytest.approx([8, 18, 28])
    report = capture.diagnostic_snapshot()
    assert report["clock"]["counts"]["adc"] == 1
    metrics = report["metrics"]
    assert metrics["callback_processing_ms"]["last"] == 4
    assert metrics["callback_budget_ms"]["last"] == 30
    assert metrics["note_report_lag_ms"]["median"] == pytest.approx(34)
    assert metrics["queue_wait_ms"]["median"] == 10
    assert report["rejected_samples"] == 0
    assert "roundtrip_latency" in report["unmeasured"]
    json.dumps(report, allow_nan=False)
