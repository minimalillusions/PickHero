"""Audio timing and recovery regressions, without opening any real devices."""

# ruff: noqa: E402 -- SDL drivers must be selected before importing pygame.

import os
from types import SimpleNamespace
from unittest.mock import Mock, call

import numpy as np
import pytest

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
import pygame

from pickhero.audio import input as audio_input
from pickhero.audio.detector import DetectedNote
from pickhero.audio.midi_playback import (
    BackingTrack, MidiEvent, MidiPlayer, NOTE_OFF, NOTE_ON, PROGRAM_CHANGE,
)
from pickhero.config import Config, StringCalibration
from pickhero.ui import calibration_menu


class SampleDetector:
    """Each ten-sample hop yields one event, with inspectable sample order."""

    hop_size = 10
    sample_rate = 1000
    onset_latency_ms = 0.0
    last_signal_db = -20.0
    last_freq = 440.0
    last_confidence = 1.0
    onset_age = 0

    def __init__(self):
        self.chunks = []
        self.resets = 0

    def process(self, chunk):
        self.chunks.append(chunk.copy())
        return DetectedNote(69, 440.0, 1.0, "A4", True, self.onset_age)

    def reset(self):
        self.resets += 1


@pytest.fixture
def capture(monkeypatch):
    monkeypatch.setattr(audio_input.AudioCapture, "_make_detector",
                        lambda self, rate: SampleDetector())
    result = audio_input.AudioCapture(Config())
    result._start_time = 100.0
    return result


def feed(capture, count, adc=None, current=None, samples=None, status=False):
    info = (SimpleNamespace(inputBufferAdcTime=adc, currentTime=current)
            if adc is not None else None)
    data = np.zeros(count, dtype=np.float32) if samples is None else samples
    capture._audio_callback(data.reshape(-1, 1), count, info, status)


def stamps(capture):
    return [note.timestamp_ms for note in capture.get_notes()]


def test_adc_hops_keep_sample_spacing_despite_processing_delay(capture, monkeypatch):
    clock = Mock(side_effect=[100.05, 100.25])
    monkeypatch.setattr(audio_input.time, "perf_counter", clock)
    # Driver latency has already been accounted for by the ADC timestamp.
    capture._input_latency_ms = 500.0
    feed(capture, 30, adc=9.96, current=10.0)
    feed(capture, 30, adc=9.99, current=10.2)
    assert stamps(capture) == pytest.approx([20, 30, 40, 50, 60, 70])
    assert clock.call_count == 2


def test_variable_blocks_keep_tail_samples_and_their_original_clock(capture, monkeypatch):
    clock = Mock(side_effect=[100.1, 100.2, 100.4, 100.5])
    monkeypatch.setattr(audio_input.time, "perf_counter", clock)
    offset = 0
    for size, current in zip([6, 17, 2, 15], [4.1, 4.2, 4.4, 4.5]):
        feed(capture, size, adc=4 + offset / 1000, current=current,
             samples=np.arange(offset, offset + size, dtype=np.float32))
        offset += size
    assert stamps(capture) == pytest.approx([10, 20, 30, 40])
    np.testing.assert_array_equal(np.concatenate(capture.detector.chunks),
                                  np.arange(40, dtype=np.float32))
    assert capture.detector.resets == 0


@pytest.mark.parametrize("info", [None, SimpleNamespace(),
    SimpleNamespace(inputBufferAdcTime=0, currentTime=0),
    SimpleNamespace(inputBufferAdcTime=float("nan"), currentTime=3),
    SimpleNamespace(inputBufferAdcTime=4, currentTime=3)])
def test_missing_adc_uses_latency_then_sample_clock(capture, monkeypatch, info):
    monkeypatch.setattr(audio_input.time, "perf_counter",
                        Mock(side_effect=[100.1, 100.8]))
    capture._input_latency_ms = 20.0
    capture._audio_callback(np.zeros((25, 1), np.float32), 25, info, False)
    capture._audio_callback(np.zeros((5, 1), np.float32), 5, info, False)
    # Reported input latency already reaches the FIRST sample: 100-20=80ms.
    assert stamps(capture) == pytest.approx([90, 100, 110])


@pytest.mark.parametrize("first_block", [25, 100, 400])
def test_fallback_input_latency_reaches_first_sample_independent_of_buffer_size(
        capture, monkeypatch, first_block):
    # PortAudio defines actual input latency as ideal callback time minus
    # FIRST sample's ADC time, so a second buffer-duration subtraction is
    # incorrect. Reference: PortAudio's BufferingLatencyAndTimingImplementationGuidelines.
    monkeypatch.setattr(audio_input.time, "perf_counter", lambda: 100.5)
    capture._input_latency_ms = 400
    feed(capture, first_block)
    expected = [100 + (hop + 1) * 10 for hop in range(first_block // 10)]
    assert stamps(capture) == pytest.approx(expected)


def test_sample_timestamp_backdates_onset_age_and_detector_latency(capture, monkeypatch):
    monkeypatch.setattr(audio_input.time, "perf_counter", lambda: 100.03)
    capture.detector.onset_age = 2
    capture.detector.onset_latency_ms = 3.0
    feed(capture, 30, adc=5.0, current=5.03)
    assert stamps(capture) == pytest.approx([-13, -3, 7])


def test_adc_gap_discards_partial_hop_and_resets_old_onset(capture, monkeypatch):
    monkeypatch.setattr(audio_input.time, "perf_counter",
                        Mock(side_effect=[100.02, 100.06]))
    feed(capture, 6, adc=5.0, current=5.02,
         samples=np.ones(6, np.float32))
    feed(capture, 10, adc=5.05, current=5.06,
         samples=np.full(10, 2.0, np.float32), status="input overflow")
    assert stamps(capture) == pytest.approx([60])
    assert capture.detector.resets == 1
    assert capture.dropped_blocks == 1
    assert capture.last_status == "input overflow"
    np.testing.assert_array_equal(capture.detector.chunks[0], np.full(10, 2.0))


@pytest.mark.parametrize("info", [None, SimpleNamespace(),
    SimpleNamespace(inputBufferAdcTime=0, currentTime=0)])
def test_input_overflow_without_adc_reanchors_and_discards_old_tail(capture, monkeypatch, info):
    monkeypatch.setattr(audio_input.time, "perf_counter",
                        Mock(side_effect=[100.02, 100.26, 100.50]))
    capture._input_latency_ms = 20.0
    capture._audio_callback(np.ones((6, 1), np.float32), 6, info, False)
    capture._audio_callback(np.full((10, 1), 2, np.float32), 10, info,
                            SimpleNamespace(input_overflow=True))
    capture._audio_callback(np.full((10, 1), 3, np.float32), 10, info, False)
    assert capture.detector.resets == 1
    assert capture.dropped_blocks == 1
    assert stamps(capture) == pytest.approx([250, 260])
    np.testing.assert_array_equal(capture.detector.chunks[0], np.full(10, 2.0))
    np.testing.assert_array_equal(capture.detector.chunks[1], np.full(10, 3.0))


def test_input_overflow_keeps_valid_adc_clock_even_when_callback_is_late(capture, monkeypatch):
    monkeypatch.setattr(audio_input.time, "perf_counter",
                        Mock(side_effect=[100.02, 100.50, 100.60]))
    capture._input_latency_ms = 80.0
    feed(capture, 6, adc=5.0, current=5.02, samples=np.ones(6, np.float32))
    # The status confirms lost input even when the host's timestamp appears
    # contiguous. Discard the tail but retain the already mapped ADC clock.
    feed(capture, 10, adc=5.006, current=5.50,
         samples=np.full(10, 2, np.float32), status=SimpleNamespace(input_overflow=True))
    feed(capture, 10, adc=5.016, current=5.60, samples=np.full(10, 3, np.float32))
    assert capture.detector.resets == 1
    assert stamps(capture) == pytest.approx([16, 26])
    np.testing.assert_array_equal(capture.detector.chunks[0], np.full(10, 2.0))


def test_output_underflow_does_not_reset_input_or_reanchor_fallback(capture, monkeypatch):
    monkeypatch.setattr(audio_input.time, "perf_counter",
                        Mock(side_effect=[100.02, 100.26]))
    feed(capture, 6, samples=np.ones(6, np.float32))
    feed(capture, 10, samples=np.full(10, 2, np.float32),
         status=SimpleNamespace(input_overflow=False, output_underflow=True))
    assert capture.detector.resets == 0
    assert stamps(capture) == pytest.approx([30])
    np.testing.assert_array_equal(capture.detector.chunks[0], [1] * 6 + [2] * 4)
    np.testing.assert_array_equal(capture._tail, np.full(6, 2.0))


def fake_streams(monkeypatch, streams):
    monkeypatch.setattr(audio_input, "best_input_device", lambda: 0)
    monkeypatch.setattr(audio_input, "negotiate_samplerate", lambda *args: 1000)
    factory = Mock(side_effect=streams)
    monkeypatch.setattr(audio_input.sd, "InputStream", factory)
    return factory


def test_restart_resets_clock_queue_tail_and_closes_previous_stream(capture, monkeypatch):
    first, second = Mock(latency=0.02), Mock(latency=0.03)
    fake_streams(monkeypatch, [first, second])
    clock = Mock(side_effect=[99.99, 100.0, 100.0, 100.04,
                              199.99, 200.0, 200.0, 200.01])
    monkeypatch.setattr(audio_input.time, "perf_counter", clock)
    capture.start()
    feed(capture, 16, adc=4.0, current=4.04)
    capture.start()
    assert capture.get_notes() == []
    assert capture._tail.size == 0
    feed(capture, 10, adc=80.0, current=80.01)
    assert stamps(capture) == pytest.approx([10])
    first.stop.assert_called_once()
    first.close.assert_called_once()
    assert capture._input_latency_ms == 30.0
    capture.stop()
    assert capture.capture_elapsed_ms() == 0.0


def test_failed_stream_start_closes_it_and_allows_retry(capture, monkeypatch):
    broken, healthy = Mock(latency=0.01), Mock(latency=0.01)
    broken.start.side_effect = OSError("device busy")
    broken.stop.side_effect = OSError("already stopped")
    fake_streams(monkeypatch, [broken, healthy])
    with pytest.raises(OSError, match="device busy"):
        capture.start()
    broken.close.assert_called_once()
    assert capture._stream is None
    capture.start()
    healthy.start.assert_called_once()
    capture.stop()


def test_timing_diagnostics_measure_full_open_and_activation_without_recording_audio(capture, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(audio_input.time, "perf_counter", lambda: clock[0])

    def choose_device():
        clock[0] += .080
        return 0

    def negotiate(*args):
        clock[0] += .100
        return 1000

    def activate():
        clock[0] += .050

    stream = Mock(latency=.020)
    stream.start.side_effect = activate

    def open_stream(**kwargs):
        clock[0] += .120
        return stream

    monkeypatch.setattr(audio_input, "best_input_device", choose_device)
    monkeypatch.setattr(audio_input, "negotiate_samplerate", negotiate)
    monkeypatch.setattr(audio_input.sd, "InputStream", open_stream)
    capture.start()
    metadata = capture.timing_diagnostics()
    assert metadata["stream_start_duration_ms"] == pytest.approx(350)
    assert metadata["stream_activation_duration_ms"] == pytest.approx(50)
    assert metadata["input_latency_ms"] == pytest.approx(20)
    assert metadata["clock_source"] == "not_started"
    feed(capture, 6)
    clock[0] += .020
    feed(capture, 14)
    metadata = capture.timing_diagnostics()
    assert metadata["clock_source"] == "fallback"
    assert (metadata["callback_frames_min"], metadata["callback_frames_max"],
            metadata["callback_frames_last"]) == (6, 14, 14)
    # The snapshot contains scalar timing/device metadata only.
    import json
    json.dumps(metadata)
    capture.stop()


def test_stop_is_idempotent_even_when_device_disappears(capture):
    stream = Mock()
    stream.stop.side_effect = OSError("unplugged")
    stream.close.side_effect = OSError("unplugged")
    capture._stream = stream
    capture.stop()
    capture.stop()
    stream.stop.assert_called_once()
    stream.close.assert_called_once()
    assert capture._stream is None


def test_backing_mute_keeps_click_and_track_position():
    player = MidiPlayer(BackingTrack([
        MidiEvent(0, 0, NOTE_ON, 40, 90),
        MidiEvent(10, 0, NOTE_OFF, 40, 0),
        MidiEvent(20, 0, NOTE_ON, 45, 90),
    ]))
    output = player._output = Mock()
    player.set_muted(True)
    output.reset_mock()
    player.update(10)
    output.write_short.assert_not_called()
    player.play_click(72)
    output.write_short.assert_called_once_with(NOTE_ON | 9, 76, 72)
    player.set_muted(False)
    output.reset_mock()
    player.update(20)
    output.write_short.assert_called_once_with(NOTE_ON, 45, 90)


def test_instrument_changes_during_mute_survive_unmute_and_seek():
    player = MidiPlayer(BackingTrack([MidiEvent(0, 2, PROGRAM_CHANGE, 33)]))
    output = player._output = Mock()
    player.set_muted(True)
    output.reset_mock()
    player.update(1)
    output.write_short.assert_called_once_with(PROGRAM_CHANGE | 2, 33, 0)
    output.reset_mock()
    player.seek(5)
    assert output.write_short.call_args == call(PROGRAM_CHANGE | 2, 33, 0)


@pytest.mark.parametrize("backing_muted", [False, True])
@pytest.mark.parametrize("metronome", [False, True])
def test_screen_controls_metronome_and_count_in_independently_of_backing(
        monkeypatch, backing_muted, metronome):
    from pickhero.tabs.timeline import NoteEvent, Timeline
    from pickhero.ui.scrolling import PlayingScreen

    monkeypatch.setattr(PlayingScreen, "_init_midi_player", lambda *args: None)
    screen = PlayingScreen(Timeline([NoteEvent(0, 2000, 40, 1, 0)]),
                           config=Config(count_in_beats=2, metronome=metronome))
    player = screen._midi_player = MidiPlayer(BackingTrack())
    output = player._output = Mock()
    player.set_muted(backing_muted)
    output.reset_mock()
    screen._audio_enabled = False
    for previous, now in [(-1000, -750), (-750, -250), (-250, 0)]:
        screen._playback_ms = now
        screen._count_in_tick(previous)
    clicks = [entry.args for entry in output.write_short.call_args_list
              if entry.args[0] == NOTE_ON | 9]
    assert clicks == [(NOTE_ON | 9, 76, 100)] * 2 + (
        [(NOTE_ON | 9, 76, 96)] if metronome else [])


@pytest.mark.parametrize("operation", ["click", "update", "seek"])
def test_disappearing_midi_device_cannot_crash_playback(operation):
    player = MidiPlayer(BackingTrack([
        MidiEvent(0, 0, PROGRAM_CHANGE, 33),
        MidiEvent(1, 0, NOTE_ON, 40, 90),
    ]))
    output = player._output = Mock()
    output.write_short.side_effect = OSError("device disconnected")
    {"click": player.play_click, "update": lambda: player.update(10),
     "seek": lambda: player.seek(10)}[operation]()
    assert player._output is None
    output.close.assert_called_once()
    player.close()


@pytest.mark.parametrize("no_device", [True, False])
def test_midi_open_failure_releases_initialized_midi(monkeypatch, no_device):
    import pygame.midi
    init, quit_midi = Mock(), Mock()
    monkeypatch.setattr(pygame.midi, "init", init)
    monkeypatch.setattr(pygame.midi, "quit", quit_midi)
    monkeypatch.setattr(pygame.midi, "get_default_output_id", lambda: -1 if no_device else 0)
    monkeypatch.setattr(pygame.midi, "Output", Mock(side_effect=OSError("busy")))
    player = MidiPlayer(BackingTrack())
    assert player.open() is False
    assert not player._opened
    init.assert_called_once()
    quit_midi.assert_called_once()


@pytest.fixture
def tuner_surface():
    pygame.font.init()
    yield pygame.Surface((1280, 720))


def key(screen, code):
    return screen.handle_event(pygame.event.Event(pygame.KEYDOWN, key=code))


def test_calibration_failure_renders_and_retries_without_advancing(monkeypatch, tuner_surface):
    broken, healthy = Mock(), Mock()
    broken.start.side_effect = OSError("device busy: " + "The selected input cannot be opened right now. " * 5)
    healthy.get_tuner_data.return_value = (0.0, 0.0)
    monkeypatch.setattr(calibration_menu, "AudioCapture", Mock(side_effect=[broken, healthy]))
    screen = calibration_menu.CalibrationMenuScreen(Config())
    key(screen, pygame.K_RETURN)
    assert screen._error and screen._state == "intro"
    assert screen._capture is None
    broken.stop.assert_called_once()
    # Any further key used to execute rendering code in the event handler.
    key(screen, pygame.K_SPACE)
    assert screen._state == "intro"
    # Observe the real shared renderer so wrapping still paints each line.
    # Error recovery must not depend on a private, obsolete drawing helper.
    shared = calibration_menu.secondary
    original_paragraph, original_text = shared.paragraph, shared.text
    drawn = {}
    active_lines = None

    def paragraph(surface, value, rect, **kwargs):
        nonlocal active_lines
        active_lines = drawn.setdefault(value, [])
        try:
            return original_paragraph(surface, value, rect, **kwargs)
        finally:
            active_lines = None

    def text(surface, value, rect, **kwargs):
        if active_lines is not None:
            active_lines.append(value)
        return original_text(surface, value, rect, **kwargs)

    monkeypatch.setattr(shared, "paragraph", paragraph)
    monkeypatch.setattr(shared, "text", text)
    screen.render(tuner_surface)
    assert calibration_menu.t("tuner.mic_failed") in drawn
    error_lines = drawn[screen._error]
    assert len(error_lines) > 3
    assert all(len(line) <= 60 for line in error_lines)
    assert "".join("".join(error_lines).split()) == "".join(screen._error.split())
    key(screen, pygame.K_RETURN)
    assert screen._error is None and screen._state == "noise"
    healthy.start.assert_called_once()
    screen.close()
    healthy.stop.assert_called_once()


def test_repeated_calibration_failures_remain_recoverable(monkeypatch):
    monkeypatch.setattr(calibration_menu, "AudioCapture", Mock(side_effect=OSError("missing")))
    screen = calibration_menu.CalibrationMenuScreen(Config())
    for code in [pygame.K_RETURN, pygame.K_RETURN, pygame.K_r, pygame.K_SPACE]:
        key(screen, code)
        screen.update()
        assert screen._state == "intro" and screen._error is not None
    assert key(screen, pygame.K_ESCAPE) == "back"


def test_calibration_cancel_survives_device_stop_failure():
    screen = calibration_menu.CalibrationMenuScreen(Config())
    capture = screen._capture = Mock()
    capture.stop.side_effect = OSError("unplugged")
    assert key(screen, pygame.K_ESCAPE) == "back"
    screen.close()
    capture.stop.assert_called_once()


def test_calibration_discards_unused_note_events_and_stale_retry_result():
    screen = calibration_menu.CalibrationMenuScreen(Config())
    capture = screen._capture = Mock()
    screen.update()
    capture.get_notes.assert_called_once()
    screen._results[screen._current_string] = StringCalibration(69, 440.0, -60.0)
    screen._state = "confirm"
    screen._timed_out = True
    key(screen, pygame.K_r)
    assert screen._state == "waiting"
    assert not screen._timed_out
    screen._finish_listen()
    assert screen._current_string not in screen.get_results()
