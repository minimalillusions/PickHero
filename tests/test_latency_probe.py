"""Known offline returns and fake duplex streams; never open real devices."""

import json
import time
from types import SimpleNamespace

import numpy as np
import pytest

from pickhero.audio import latency_probe as probe_module
from pickhero.audio.latency_probe import (
    DURATION_SECONDS, LatencyProbe, OUTPUT_PEAK, _probe_signal, analyse_roundtrip,
)


def _returned_signal(rate=16000, delays=(125, 125, 125), gain=0.6, noise=0.0):
    output, pulse, starts = _probe_signal(rate)
    signal = np.random.default_rng(42).normal(0, noise, len(output)).astype(np.float32)
    for start, delay in zip(starts, delays):
        offset = start + round(delay * rate / 1000)
        signal[offset:offset + len(pulse)] += pulse * gain
    return signal


@pytest.mark.parametrize("rate", [8000, 16000, 44100, 48000, 96000])
@pytest.mark.parametrize("delay", [0, 7.3, 125, 480])
def test_known_signal_measures_roundtrip_without_modifying_pcm_or_calibrating(rate, delay):
    capture = _returned_signal(rate, (delay,) * 3, noise=0.0001)
    original = capture.copy()
    result = analyse_roundtrip(capture, rate)
    assert result["status"] == "complete"
    assert result["roundtrip_ms"] == pytest.approx(delay, abs=1000 / rate)
    assert result["spread_ms"] == 0
    assert min(result["correlations"]) > 0.99
    assert result["reference"] == "output_buffer_to_input_buffer"
    assert result["automatic_offset_applied"] is False
    assert set(result["unmeasured"]) == {"input_latency_alone", "output_latency_alone",
                                         "display_latency", "midi_synthesizer_latency"}
    np.testing.assert_array_equal(capture, original)
    json.dumps(result, allow_nan=False)


def test_polarity_reversed_return_is_still_measured():
    result = analyse_roundtrip(_returned_signal(gain=-0.8), 16000)
    assert result["status"] == "complete"
    assert result["roundtrip_ms"] == 125


def test_filtered_speaker_return_is_measured_with_small_route_group_delay():
    capture = _returned_signal()
    capture = np.convolve(capture, np.array([0.6, 0.3, 0.1]), mode="full")[:len(capture)]
    result = analyse_roundtrip(capture, 16000)
    assert result["status"] == "complete"
    assert result["roundtrip_ms"] == pytest.approx(125, abs=1)


def test_silence_and_an_absent_third_return_do_not_claim_a_measurement():
    silent = np.zeros(round(DURATION_SECONDS * 16000), dtype=np.float32)
    assert analyse_roundtrip(silent, 16000)["reason"] == "no_signal"
    capture = _returned_signal()
    capture[round(3 * 16000):] = 0
    result = analyse_roundtrip(capture, 16000)
    assert result["status"] == "failed"
    assert result["reason"] == "weak_correlation"
    assert result["roundtrip_ms"] is None
    assert result["pulse_delays_ms"] == []


def test_unrelated_noise_is_not_a_return():
    capture = _returned_signal(gain=0, noise=0.1)
    result = analyse_roundtrip(capture, 16000)
    assert result["reason"] == "weak_correlation"
    assert result["roundtrip_ms"] is None


def test_two_equally_strong_paths_are_ambiguous():
    capture = _returned_signal() + _returned_signal(delays=(210, 210, 210))
    result = analyse_roundtrip(capture, 16000)
    assert result["reason"] == "ambiguous"
    assert result["roundtrip_ms"] is None


def test_inconsistent_three_returns_have_no_offset():
    result = analyse_roundtrip(_returned_signal(delays=(100, 140, 180)), 16000)
    assert result["reason"] == "inconsistent"
    assert result["roundtrip_ms"] is None


def test_search_boundary_is_rejected():
    result = analyse_roundtrip(_returned_signal(delays=(500, 500, 500)), 16000)
    assert result["reason"] == "out_of_range"
    assert result["roundtrip_ms"] is None


@pytest.mark.parametrize("value,reason", [(1.0, "clipping"), (-1.0, "clipping"),
                                         (np.nan, "invalid_capture"), (np.inf, "invalid_capture")])
def test_clipping_and_nonfinite_capture_are_rejected(value, reason):
    capture = _returned_signal()
    capture[5] = value
    result = analyse_roundtrip(capture, 16000)
    assert result["reason"] == reason
    assert result["roundtrip_ms"] is None


@pytest.mark.parametrize("rate", [True, None, 0, 7999, 96001, 48000.0])
def test_invalid_sample_rate_has_no_measurement(rate):
    result = analyse_roundtrip(np.zeros(10), rate)
    assert result["reason"] == "unsupported_format"


@pytest.mark.parametrize("capture", [None, [], np.zeros(12), np.zeros((72000, 1)),
                                      np.array(["audio"])])
def test_capture_is_strictly_bounded_and_mono(capture):
    result = analyse_roundtrip(capture, 16000)
    assert result["reason"] == "invalid_capture"


def test_generated_probe_has_exactly_three_quiet_tapered_bursts():
    output, pulse, starts = _probe_signal(48000)
    assert output.dtype == pulse.dtype == np.float32
    assert len(output) == 4.5 * 48000
    assert float(np.max(np.abs(output))) <= OUTPUT_PEAK + 1e-8
    assert pulse[0] == pulse[-1] == 0
    expected = np.zeros_like(output)
    for start in starts:
        expected[start:start + len(pulse)] = pulse
    np.testing.assert_array_equal(output, expected)


class _FakeStream:
    """Runs bounded virtual audio inside the probe worker, never in PortAudio."""

    instances = []
    mode = "success"
    abort_raises = False
    close_raises = False
    close_then_raises = False

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.latency = (0.01, 0.025)
        self.active = False
        self.aborted = False
        self.closed = False
        self.sent_peak = 0
        self.audio_buffer = None
        self.__class__.instances.append(self)

    def start(self):
        if self.mode == "start_failure":
            raise RuntimeError("private device error")
        self.active = True
        if self.mode == "stalled":
            return
        rate = self.kwargs["samplerate"]
        capture = _returned_signal(rate)
        if self.mode == "silence":
            capture.fill(0)
        elif self.mode == "no_callback":
            self.active = False
            self.kwargs["finished_callback"]()
            return
        cursor = 0
        sizes = (128, 511, 256, 1024)
        index = 0
        while cursor < len(capture):
            frames = sizes[index % len(sizes)]
            count = min(frames, len(capture) - cursor)
            indata = np.zeros((frames, 1), dtype=np.float32)
            indata[:count, 0] = capture[cursor:cursor + count]
            outdata = np.full((frames, self.kwargs["channels"][1]), np.nan, dtype=np.float32)
            try:
                self.kwargs["callback"](indata, outdata, frames, SimpleNamespace(),
                                        self.mode == "status_error")
            except (probe_module.sd.CallbackStop, probe_module.sd.CallbackAbort):
                self.active = False
            assert np.all(np.isfinite(outdata))
            self.sent_peak = max(self.sent_peak, float(np.max(np.abs(outdata))))
            cursor += count
            index += 1
            if not self.active:
                break
        self.active = False
        self.kwargs["finished_callback"]()

    def abort(self):
        self.aborted = True
        self.active = False
        self.kwargs["finished_callback"]()
        if self.abort_raises:
            raise RuntimeError("abort failed")

    def close(self):
        if self.close_raises:
            raise RuntimeError("close failed")
        self.closed = True
        if self.close_then_raises:
            raise RuntimeError("error after closing")


@pytest.fixture
def fake_devices(monkeypatch):
    # These fakes supersede conftest's hardware prohibition, including duplex.
    _FakeStream.instances = []
    _FakeStream.mode = "success"
    _FakeStream.abort_raises = False
    _FakeStream.close_raises = False
    _FakeStream.close_then_raises = False
    devices = {
        2: {"max_input_channels": 1, "max_output_channels": 0, "hostapi": 1,
            "default_samplerate": 16000},
        5: {"max_input_channels": 0, "max_output_channels": 2, "hostapi": 1,
            "default_samplerate": 16000},
    }
    monkeypatch.setattr(probe_module.sd, "query_devices", lambda device: devices[device])
    monkeypatch.setattr(probe_module.sd, "query_hostapis", lambda host: {"default_output_device": 5})
    monkeypatch.setattr(probe_module.sd, "check_input_settings", lambda **kwargs: None)
    monkeypatch.setattr(probe_module.sd, "check_output_settings", lambda **kwargs: None)
    monkeypatch.setattr(probe_module.sd, "Stream", _FakeStream)
    return devices


def _final(probe):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        result = probe.poll()
        if result is not None:
            return result
        time.sleep(0.002)
    probe.close()
    pytest.fail("Probe worker did not finish")


def test_constructor_does_not_open_devices_and_success_releases_them_before_poll(fake_devices):
    probe = LatencyProbe(2, 16000)
    assert probe.update()["status"] == "idle"
    assert probe.poll() is None and not probe.is_running
    assert _FakeStream.instances == []
    assert probe.start()
    result = _final(probe)
    stream, = _FakeStream.instances
    assert stream.closed and stream.aborted
    assert probe.safe_to_resume
    assert stream.kwargs["device"] == (2, 5)
    assert stream.kwargs["channels"] == (1, 2)
    assert stream.kwargs["blocksize"] == 0
    assert stream.kwargs["latency"] == "low"
    assert stream.sent_peak <= OUTPUT_PEAK + 1e-8
    assert result["status"] == "complete"
    assert result["roundtrip_ms"] == 125
    assert result["driver_input_latency_ms"] == 10
    assert result["driver_output_latency_ms"] == 25
    assert probe.update()["progress"] == 1
    assert probe.update()["running"] is False
    result["pulse_delays_ms"].append(999)
    assert probe.poll()["pulse_delays_ms"] == [125, 125, 125]
    json.dumps(probe.update(), allow_nan=False)


def test_capture_and_output_closure_buffers_are_erased_after_measurement(fake_devices):
    probe = LatencyProbe(2, 16000)
    probe.start()
    assert _final(probe)["status"] == "complete"
    callback = _FakeStream.instances[0].kwargs["callback"]
    buffers = [cell.cell_contents for cell in callback.__closure__
               if isinstance(cell.cell_contents, np.ndarray)]
    assert len(buffers) >= 2
    assert all(np.all(buffer == 0) for buffer in buffers)


@pytest.mark.parametrize("mode,reason", [("start_failure", "device_error"),
                                        ("status_error", "stream_error"),
                                        ("silence", "no_signal"),
                                        ("no_callback", "invalid_capture")])
def test_stream_failures_are_closed_and_never_return_an_offset(fake_devices, mode, reason):
    _FakeStream.mode = mode
    probe = LatencyProbe(2, 16000)
    probe.start()
    report = _final(probe)
    assert report["status"] == "failed"
    assert report["reason"] == reason
    assert report["roundtrip_ms"] is None
    assert _FakeStream.instances[0].closed
    assert "private" not in json.dumps(report)


def test_cancellation_is_nonblocking_prevents_overlapping_start_and_cleans_up(fake_devices):
    _FakeStream.mode = "stalled"
    probe = LatencyProbe(2, 16000)
    assert probe.start()
    assert not probe.start()
    deadline = time.monotonic() + 2
    while not _FakeStream.instances and time.monotonic() < deadline:
        time.sleep(0.002)
    probe.stop()
    report = _final(probe)
    assert report["status"] == "cancelled"
    assert report["roundtrip_ms"] is None
    assert _FakeStream.instances[0].closed
    assert probe.close()


def test_cancel_before_worker_opens_stream_has_no_output(fake_devices, monkeypatch):
    original = probe_module._select_devices
    probe = LatencyProbe(2, 16000)

    def cancel_during_selection(device):
        probe.stop()
        return original(device)

    monkeypatch.setattr(probe_module, "_select_devices", cancel_during_selection)
    assert probe.start()
    assert _final(probe)["status"] == "cancelled"
    assert _FakeStream.instances == []


def test_timeout_closes_a_driver_which_never_produces_a_callback(fake_devices, monkeypatch):
    _FakeStream.mode = "stalled"
    monkeypatch.setattr(probe_module, "TIMEOUT_PADDING_SECONDS", -DURATION_SECONDS + 0.01)
    probe = LatencyProbe(2, 16000)
    probe.start()
    report = _final(probe)
    assert report["reason"] == "timeout"
    assert _FakeStream.instances[0].closed


@pytest.mark.parametrize("bad_output", [-1, 22])
def test_missing_output_does_not_fall_back_to_an_unrelated_host(fake_devices, monkeypatch, bad_output):
    fake_devices[22] = {"max_input_channels": 0, "max_output_channels": 2,
                        "hostapi": 99, "default_samplerate": 16000}
    monkeypatch.setattr(probe_module.sd, "query_hostapis",
                        lambda host: {"default_output_device": bad_output})
    probe = LatencyProbe(2, 16000)
    probe.start()
    result = _final(probe)
    assert result["reason"] == "no_compatible_output"
    assert _FakeStream.instances == []


def test_unsupported_input_or_output_format_does_not_open_a_stream(fake_devices, monkeypatch):
    def reject(**kwargs):
        raise RuntimeError("unsupported")

    monkeypatch.setattr(probe_module.sd, "check_output_settings", reject)
    probe = LatencyProbe(2, 16000)
    probe.start()
    assert _final(probe)["reason"] == "unsupported_format"
    assert _FakeStream.instances == []


def test_negotiation_uses_a_rate_supported_by_both_directions(fake_devices, monkeypatch):
    def check(**kwargs):
        if kwargs["samplerate"] != 44100:
            raise RuntimeError("rate not supported")

    monkeypatch.setattr(probe_module.sd, "check_output_settings", check)
    probe = LatencyProbe(2, 96000)
    probe.start()
    report = _final(probe)
    assert report["status"] == "complete"
    assert report["sample_rate"] == 44100


@pytest.mark.parametrize("abort_raises,close_raises", [(True, False), (False, True), (True, True)])
def test_close_is_always_attempted_even_if_abort_fails(fake_devices, abort_raises, close_raises):
    _FakeStream.abort_raises = abort_raises
    _FakeStream.close_raises = close_raises
    probe = LatencyProbe(2, 16000)
    probe.start()
    result = _final(probe)
    assert result["status"] == ("failed" if close_raises else "complete")
    assert probe.safe_to_resume is not close_raises
    assert result["stream_closed"] is not close_raises
    if close_raises:
        assert result["roundtrip_ms"] is None
        assert result["reason"] == "cleanup_failed"
        assert not probe.start()
    assert _FakeStream.instances[0].aborted
    assert _FakeStream.instances[0].closed is not close_raises


def test_failed_close_retains_handle_and_explicit_retry_releases_it(fake_devices):
    _FakeStream.close_raises = True
    probe = LatencyProbe(2, 16000)
    probe.start()
    result = _final(probe)
    assert result["reason"] == "cleanup_failed"
    assert not probe.safe_to_resume
    assert not probe.close()
    assert not probe.start()
    stream, = _FakeStream.instances
    assert not stream.closed
    _FakeStream.close_raises = False
    assert probe.close()
    assert probe.safe_to_resume
    assert stream.closed
    assert probe.poll()["status"] == "cancelled"
    assert probe.poll()["stream_closed"] is True
    assert len(_FakeStream.instances) == 1


def test_cancel_does_not_override_an_unreleased_handle(fake_devices):
    _FakeStream.mode = "stalled"
    _FakeStream.abort_raises = True
    _FakeStream.close_raises = True
    probe = LatencyProbe(2, 16000)
    probe.start()
    deadline = time.monotonic() + 2
    while not _FakeStream.instances and time.monotonic() < deadline:
        time.sleep(0.002)
    probe.stop()
    result = _final(probe)
    assert result["status"] == "failed"
    assert result["reason"] == "cleanup_failed"
    assert result["stream_closed"] is False
    assert not probe.safe_to_resume
    assert not probe.start()


def test_native_error_after_confirmed_close_is_safe_to_resume(fake_devices):
    _FakeStream.close_then_raises = True
    probe = LatencyProbe(2, 16000)
    probe.start()
    result = _final(probe)
    assert result["stream_closed"] is True
    assert probe.safe_to_resume


def test_automatic_input_matches_the_application_device_selection(fake_devices, monkeypatch):
    from pickhero.audio import input as input_module

    chosen = []

    def choose():
        chosen.append(True)
        return 2

    monkeypatch.setattr(input_module, "best_input_device", choose)
    probe = LatencyProbe(None, 16000)
    assert chosen == []
    probe.start()
    result = _final(probe)
    assert chosen == [True]
    assert result["input_device"] == 2
    assert result["status"] == "complete"


def test_idle_close_is_safe_and_does_not_open_anything(fake_devices):
    probe = LatencyProbe(2, 16000)
    assert probe.close()
    assert probe.poll() is None
    assert _FakeStream.instances == []
