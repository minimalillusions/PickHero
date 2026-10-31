"""Setup evidence belongs to current sensitivity and a healthy whole visit."""

from types import SimpleNamespace
from unittest.mock import Mock

import pygame
import pytest

from pickhero.audio.detector import DetectedNote
from pickhero.audio.input import AudioCapture, TimestampedNote
from pickhero.audio.note_utils import midi_to_freq
from pickhero.config import Config
from pickhero.ui import check_menu
from pickhero.ui.setup_guide import SetupGuideScreen


class Capture:
    def __init__(self, config, *, instrument):
        self.notes = []
        self.issue = None
        self.level = -25.0
        self.freq = midi_to_freq(28)
        self.confidence = .95
        self.detector = SimpleNamespace(noise_gate_db=-55.0)
        self.detector.set_noise_gate_db = Mock(
            side_effect=lambda value: setattr(self.detector, "noise_gate_db", value))
        self.start = Mock()
        self.stop = Mock()
        self.set_health_window = Mock()

    def get_notes(self):
        notes, self.notes = self.notes, []
        return notes

    def get_tuner_data(self):
        return self.freq, self.confidence

    def get_signal_db(self):
        return self.level

    def health_issue(self):
        return self.issue


def note(timestamp):
    return TimestampedNote(DetectedNote(28, midi_to_freq(28), .95, "E1", True), timestamp)


@pytest.fixture
def screen(monkeypatch):
    pygame.font.init()
    monkeypatch.setattr(check_menu, "AudioCapture", Capture)
    config = Config(instrument="bass_ukulele")
    config.audio.noise_gate_db = -55.0
    config.save = Mock()
    result = check_menu.CheckScreen(config, purpose="phrase")
    yield result
    result.close()


def gather_evidence(screen):
    screen._capture.notes = [note(1000), note(3000), note(5000)]
    for _ in range(check_menu.FRAMES_TO_CONFIRM):
        screen.update()
    report = screen.diagnostic_snapshot()
    assert report["confirmed"] == 1 and report["target_hits"] == 3
    assert report["has_signal"] and report["reliable"]


def assert_fresh(screen):
    report = screen.diagnostic_snapshot()
    assert report["target_hits"] == 0 and report["confirmed"] == 0
    assert not report["has_signal"]
    assert not any(screen._streak.values())
    assert screen._best_cents == {}
    assert screen._freq == 0 and screen._confidence == 0
    assert screen._strikes == 0 and screen._last_target_ms is None
    assert screen._capture.notes == []


@pytest.mark.parametrize("direction", [1.0, -1.0])
def test_gate_change_discards_prior_and_queued_evidence(screen, direction):
    gather_evidence(screen)
    screen._capture.notes = [note(7000)]
    screen._nudge_gate(direction)
    assert_fresh(screen)
    assert screen._config.audio.noise_gate_db == -55.0 + direction
    screen._config.save.assert_called_once()
    guide = SetupGuideScreen(screen._config)
    guide.record_check("phrase", screen.diagnostic_snapshot())
    assert guide.reports["phrase"]["target_hits"] == 0
    screen._capture.notes = [note(5250)]
    screen.update()
    assert screen._target_hits == 1


def test_measurement_cannot_reuse_before_measurement_evidence(screen):
    gather_evidence(screen)
    screen._begin_measurement()
    assert_fresh(screen)
    assert not screen.diagnostic_snapshot()["reliable"]
    screen._room_samples = [-60.0, -60.0, -60.0, -60.0]
    screen._capture.notes = [note(7000)]
    screen._finish_measurement()
    assert_fresh(screen)
    assert screen.diagnostic_snapshot()["reliable"]
    assert screen._config.audio.noise_gate_db == -60.0 + check_menu.GATE_MARGIN_DB
    screen._config.save.assert_called_once()


@pytest.mark.parametrize("fault", ["input_overflow", "input_clipping",
                                   "input_stalled", "input_disconnected"])
def test_recovered_fault_remains_until_capture_visit_restarts(screen, fault):
    gather_evidence(screen)
    capture = screen._capture
    capture.set_health_window.assert_called_once_with(0.0)
    capture.issue = fault
    screen.update()
    capture.issue = None
    # Neither clearing ticks nor changing sensitivity erases an audio fault.
    screen._activate("reset")
    screen._nudge_gate(-1.0)
    screen._begin_measurement()
    screen._room_samples = [-60.0]
    screen._finish_measurement()
    report = screen.diagnostic_snapshot()
    assert not report["reliable"] and report["error"] == fault
    screen._activate("retry")
    capture.stop.assert_called_once()
    assert screen.diagnostic_snapshot()["reliable"]
    assert screen.diagnostic_snapshot()["error"] is None


def test_fault_between_frames_is_retained_by_capture_window(screen, monkeypatch):
    capture = screen._capture
    # Use the real health implementation, with no physical stream, to model
    # an overflow ten seconds before the next UI frame.
    capture._stream = SimpleNamespace(active=True)
    capture._start_time = 0.0
    capture._input_latency_ms = 0.0
    capture._last_callback_time = 20.0
    capture._last_overflow_time = 10.0
    capture._last_clipping_time = None
    capture._health_since = None
    boundary = capture.set_health_window.call_args.args[0]
    AudioCapture.set_health_window(capture, boundary)
    monkeypatch.setattr("pickhero.audio.input.time.perf_counter", lambda: 20.0)
    capture.health_issue = lambda: AudioCapture.health_issue(capture)
    report = screen.diagnostic_snapshot()
    assert not report["reliable"] and report["error"] == "input_overflow"


def test_capture_without_health_window_still_latches_faults(monkeypatch):
    class LegacyCapture(Capture):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            del self.set_health_window

    monkeypatch.setattr(check_menu, "AudioCapture", LegacyCapture)
    screen = check_menu.CheckScreen(Config(instrument="bass_ukulele"))
    try:
        assert screen._error is None
        screen._capture.issue = "input_clipping"
        screen.update()
        screen._capture.issue = None
        assert not screen.diagnostic_snapshot()["reliable"]
    finally:
        screen.close()


def test_health_query_failure_cannot_certify_visit(screen):
    screen._capture.health_issue = Mock(side_effect=RuntimeError("Device gone"))
    screen.update()
    screen._capture.health_issue = lambda: None
    assert screen.diagnostic_snapshot()["error"] == "input_disconnected"
    assert not screen.diagnostic_snapshot()["reliable"]
