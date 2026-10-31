"""Imported tunings must reach the same live detector that scores playback."""

from dataclasses import fields, replace
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from pickhero import instruments
from pickhero.audio.detector import PitchDetector
from pickhero.audio.input import AudioCapture
from pickhero.audio.note_utils import midi_to_freq
from pickhero.config import Config, StringCalibration
from pickhero.exercises import write_exercises
from pickhero.tabs.loader import load_gp_file
from pickhero.tabs.timeline import Timeline
from pickhero.ui.scrolling import PlayingScreen


DROP_D = {1: 64, 2: 59, 3: 55, 4: 50, 5: 45, 6: 38}
FIVE_STRING_BASS = {1: 43, 2: 38, 3: 33, 4: 28, 5: 23}


@pytest.fixture
def device(monkeypatch):
    clock = SimpleNamespace(now=100.0)
    monkeypatch.setattr("pickhero.audio.input.time.perf_counter", lambda: clock.now)
    monkeypatch.setattr("pickhero.audio.input.best_input_device", lambda: 0)
    monkeypatch.setattr("pickhero.audio.input.negotiate_samplerate", lambda device, rate: rate)
    monkeypatch.setattr("pickhero.audio.input.sd.InputStream", lambda **kwargs: Mock(latency=0.0))
    monkeypatch.setattr(PlayingScreen, "_init_midi_player", lambda *args: None)
    monkeypatch.setattr(PlayingScreen, "_log_result", lambda *args: None)
    return clock


def imported_low_note(tmp_path, base, tuning):
    # Write a real GP5 with the requested tuning, then take its first note.
    # This fixture does not use the tuning-adaptation helper being tested.
    fixture_profile = replace(base, tuning=dict(tuning))
    path = write_exercises(fixture_profile, tmp_path)[0]
    imported = load_gp_file(path, instrument=base)
    assert imported.metadata.tuning == tuning
    first = replace(imported.notes[0], timestamp_ms=500.0, duration_ms=125.0)
    return Timeline([first], imported.metadata)


@pytest.mark.parametrize("base,tuning", [
    (instruments.GUITAR, DROP_D),
    (instruments.BASS_GUITAR, FIVE_STRING_BASS),
    (instruments.BASS_UKULELE, FIVE_STRING_BASS),
])
def test_track_profile_preserves_every_measured_parameter(base, tuning):
    adapted = instruments.with_tuning(base, tuning)
    for field in fields(base):
        if field.name not in {"tuning", "buf_size"}:
            assert getattr(adapted, field.name) == getattr(base, field.name)
    assert adapted.tuning == tuning
    assert adapted.buf_size > base.buf_size
    assert base.tuning != tuning  # no global profile mutation
    assert instruments.with_tuning(base, dict(base.tuning)) is base


@pytest.mark.parametrize("base,tuning", [
    (instruments.GUITAR, DROP_D),
    (instruments.BASS_GUITAR, FIVE_STRING_BASS),
])
@pytest.mark.parametrize("rate", [44100, 48000])
def test_imported_low_string_reaches_capture_and_scores_final_note(
        tmp_path, device, base, tuning, rate):
    timeline = imported_low_note(tmp_path, base, tuning)
    cfg = Config(instrument=base.key, count_in_beats=0)
    cfg.audio.sample_rate = rate
    screen = PlayingScreen(timeline, config=cfg)
    screen.toggle_play()
    capture = screen._audio_capture
    detector = capture.detector
    assert screen._audio_enabled
    assert capture.instrument.tuning == tuning
    assert detector.midi_min == min(tuning.values())
    assert detector.onset_buf_size == base.onset_buf_size
    assert detector.onset_latency_ms == base.onset_latency_ms
    assert detector.expected_delay_ms > PitchDetector(sample_rate=rate, instrument=base).expected_delay_ms
    assert screen._end_ms >= timeline.duration_ms + detector.expected_delay_ms

    # A bounded synthetic pluck: silent lead-in, then the fundamental and
    # its first four harmonics. No microphone, recording, or output device.
    samples = np.arange(int(rate * 2.0), dtype=np.float64) / rate
    frequency = midi_to_freq(min(tuning.values()))
    strike = np.maximum(samples - 0.5, 0.0)
    envelope = np.where((samples >= 0.5) & (samples < 1.25),
                        np.minimum(strike / 0.005, 1.0) * np.exp(-strike * 2), 0.0)
    wave = sum(np.sin(2 * np.pi * frequency * harmonic * samples) / harmonic
               for harmonic in range(1, 5))
    wave = (wave * envelope * 0.25).astype(np.float32)
    hop = detector.hop_size
    for offset in range(0, len(wave) - hop + 1, hop):
        device.now = 100.0 + (offset + hop) / rate
        driver = SimpleNamespace(inputBufferAdcTime=100.0 + offset / rate,
                                 currentTime=device.now)
        capture._audio_callback(wave[offset:offset + hop, None], hop, driver, False)
        screen.update()
        if screen._song_completed:
            break
    assert screen._song_completed
    stats = screen._matcher.get_statistics()
    assert stats["hits"] == 1 and stats["misses"] == 0, stats
    assert cfg.get_instrument() is base
    assert cfg.audio.buf_size is None


@pytest.mark.parametrize("buffer_size", [2048, 8192])
def test_live_window_grows_only_if_needed_and_preserves_saved_overrides(buffer_size):
    cfg = Config(instrument="bass_guitar")
    cfg.audio.buf_size = buffer_size
    cfg.audio.hop_size = 256
    cfg.audio.noise_gate_db = -37.0
    cfg.audio.confidence_threshold = 0.31
    cfg.audio.onset_threshold = 0.2
    adapted = instruments.with_tuning(cfg.get_instrument(), FIVE_STRING_BASS)
    capture = AudioCapture(cfg, instrument=adapted)
    assert capture.detector.buf_size == max(buffer_size, adapted.buf_size)
    assert capture.detector.hop_size == 256
    assert capture.detector.noise_gate_db == -37.0
    assert capture.detector.confidence_threshold == 0.31
    assert capture.detector.onset_threshold == 0.2
    assert cfg.audio.buf_size == buffer_size


def calibrate(cfg, tuning):
    cfg.calibration = {"instrument": cfg.instrument}
    for string, midi in tuning.items():
        cfg.set_string_calibration(string, StringCalibration(midi, midi_to_freq(midi), -60))


def test_standard_guitar_calibration_is_not_applied_to_drop_d():
    cfg = Config()
    calibrate(cfg, instruments.GUITAR.tuning)
    adapted = instruments.with_tuning(cfg.get_instrument(), DROP_D)
    assert AudioCapture(cfg, instrument=adapted)._calibration is None
    assert AudioCapture(cfg)._calibration is cfg.calibration


def test_four_string_calibration_is_not_applied_to_five_strings():
    cfg = Config(instrument="bass_guitar")
    calibrate(cfg, instruments.BASS_GUITAR.tuning)
    adapted = instruments.with_tuning(cfg.get_instrument(), FIVE_STRING_BASS)
    assert AudioCapture(cfg, instrument=adapted)._calibration is None


def test_complete_matching_custom_calibration_is_used():
    cfg = Config()
    calibrate(cfg, DROP_D)
    adapted = instruments.with_tuning(cfg.get_instrument(), DROP_D)
    assert AudioCapture(cfg, instrument=adapted)._calibration is cfg.calibration
    cfg.calibration["instrument"] = "bass_guitar"
    assert AudioCapture(cfg, instrument=adapted)._calibration is None


def test_partial_matching_calibration_remains_usable_for_original_tuning():
    cfg = Config()
    calibrate(cfg, {1: 64})
    assert AudioCapture(cfg)._calibration is cfg.calibration
    adapted = instruments.with_tuning(cfg.get_instrument(), DROP_D)
    assert AudioCapture(cfg, instrument=adapted)._calibration is None


def test_monitoring_capture_uses_the_imported_tuning(tmp_path, device):
    timeline = imported_low_note(tmp_path, instruments.BASS_GUITAR, FIVE_STRING_BASS)
    screen = PlayingScreen(timeline, config=Config(instrument="bass_guitar"))
    screen._start_capture_only()
    assert screen._audio_capture.detector.midi_min == 23
    screen.stop_audio()
