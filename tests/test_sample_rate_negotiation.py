"""Negotiate usable capture rates without opening hardware or saving preferences."""
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from pickhero.audio import input as audio_input
from pickhero.config import Config


class FakeInputDevice:
    def __init__(self, default_rate, supported):
        self.default_rate = default_rate
        self.supported = set(supported)
        self.queries = []
        self.checks = []

    def query(self, device):
        self.queries.append(device)
        return {"default_samplerate": self.default_rate}

    def check(self, **kwargs):
        self.checks.append(kwargs)
        if kwargs["samplerate"] not in self.supported:
            raise ValueError("Sample rate unsupported by this fake device")

    @property
    def checked_rates(self):
        return [entry["samplerate"] for entry in self.checks]


def fake_device(monkeypatch, default_rate, supported):
    device = FakeInputDevice(default_rate, supported)
    monkeypatch.setattr(audio_input.sd, "query_devices", device.query)
    monkeypatch.setattr(audio_input.sd, "check_input_settings", device.check)
    return device


@pytest.mark.parametrize("preferred,default,supported,expected,checked", [
    (44100, 192000, {48000, 192000}, 48000, [44100, 48000]),
    (48000, 96000, {44100, 96000}, 44100, [48000, 44100]),
    (32000, 192000, {48000, 192000}, 48000, [32000, 48000]),
    (44100, 96000, {96000}, 96000, [44100, 48000, 96000]),
    (44100, 192000, {192000}, 192000, [44100, 48000, 192000]),
    (44100, 96000, {22050}, 22050, [44100, 48000, 96000, 32000, 22050]),
    (44100, 384000, {16000, 384000}, 16000, [44100, 48000, 32000, 22050, 16000]),
    (384000, 384000, {48000, 384000}, 48000, [48000]),
])
def test_fallback_prefers_standard_rates_but_retains_high_only_and_low_rate_devices(
        monkeypatch, preferred, default, supported, expected, checked):
    device = fake_device(monkeypatch, default, supported)
    assert audio_input.negotiate_samplerate(7, preferred) == expected
    assert device.checked_rates == checked
    assert all(entry["device"] == 7 and entry["channels"] == 1 and entry["dtype"] == "float32"
               for entry in device.checks)


@pytest.mark.parametrize("preferred", [16000, 44100, 48000, 96000, 192000])
def test_explicit_supported_preference_is_always_honored(monkeypatch, preferred):
    device = fake_device(monkeypatch, 192000, {preferred, 48000})
    assert audio_input.negotiate_samplerate(7, preferred) == preferred
    assert device.checked_rates == [preferred]


def test_duplicate_candidates_are_checked_only_once(monkeypatch):
    device = fake_device(monkeypatch, 48000, {32000})
    assert audio_input.negotiate_samplerate(7, 48000) == 32000
    assert device.checked_rates == [48000, 44100, 32000]


@pytest.mark.parametrize("default", [None, float("nan"), float("inf"), "invalid", -1, 384000])
def test_unusable_device_metadata_cannot_prevent_a_standard_rate_fallback(monkeypatch, default):
    device = fake_device(monkeypatch, default, {48000})
    assert audio_input.negotiate_samplerate(7, 44100) == 48000
    assert device.checked_rates == [44100, 48000]


def test_missing_device_metadata_still_checks_supported_rates(monkeypatch):
    device = fake_device(monkeypatch, 192000, {48000})

    def unavailable(*args, **kwargs):
        raise ValueError("Metadata unavailable")

    monkeypatch.setattr(audio_input.sd, "query_devices", unavailable)
    assert audio_input.negotiate_samplerate(7, 44100) == 48000
    assert device.checked_rates == [44100, 48000]


def test_system_default_is_queried_without_replacing_the_stream_device_choice(monkeypatch):
    device = fake_device(monkeypatch, 192000, {48000})
    monkeypatch.setattr(audio_input.sd, "default", SimpleNamespace(device=(7, -1)))
    assert audio_input.negotiate_samplerate(None, 44100) == 48000
    assert device.queries == [7]
    assert all(entry["device"] is None for entry in device.checks)


class FakeStream:
    latency = .005

    def __init__(self, **kwargs):
        self.settings = kwargs
        self.starts = 0
        self.stops = 0
        self.closed = False
        self.active = False

    def start(self):
        self.starts += 1
        self.active = True

    def stop(self):
        self.stops += 1
        self.active = False

    def close(self):
        self.closed = True


def capture_with_fake_streams(monkeypatch, preferred, supported, default_rate=192000):
    device = fake_device(monkeypatch, default_rate, supported)
    streams = []

    def open_stream(**kwargs):
        stream = FakeStream(**kwargs)
        streams.append(stream)
        return stream

    def no_save(*args, **kwargs):
        pytest.fail("Negotiation must not save user configuration")

    monkeypatch.setattr(audio_input.sd, "InputStream", open_stream)
    monkeypatch.setattr(Config, "save", no_save)
    config = Config(instrument="bass_guitar")
    config.audio.device_index = 7
    config.audio.sample_rate = preferred
    return audio_input.AudioCapture(config), device, streams


def test_start_uses_actual_48khz_detector_and_retains_requested_44100_for_a_later_restart(monkeypatch):
    capture, device, streams = capture_with_fake_streams(monkeypatch, 44100, {48000, 192000})
    original_settings = asdict(capture.config.audio)
    original_detector = capture.detector
    assert original_detector.sample_rate == 44100
    try:
        capture.start()
        assert capture.sample_rate == capture.detector.sample_rate == 48000
        assert capture.detector is not original_detector
        assert streams[0].settings["samplerate"] == 48000
        assert streams[0].settings["callback"].__self__ is capture
        assert streams[0].starts == 1
        assert capture.timing_diagnostics()["sample_rate"] == 48000
        assert asdict(capture.config.audio) == original_settings

        # The device may later accept the preferred rate. Restart must use
        # the retained request, rather than persisting the previous fallback.
        fallback_detector = capture.detector
        device.supported.add(44100)
        capture.start()
        assert streams[0].closed and streams[0].stops == 1
        assert capture.sample_rate == capture.detector.sample_rate == 44100
        assert capture.detector is not fallback_detector
        assert streams[1].settings["samplerate"] == 44100
        assert asdict(capture.config.audio) == original_settings
    finally:
        capture.stop()
    assert all(stream.closed for stream in streams)


@pytest.mark.parametrize("preferred", [96000, 192000])
def test_start_preserves_explicit_supported_high_rate(monkeypatch, preferred):
    capture, device, streams = capture_with_fake_streams(monkeypatch, preferred, {preferred, 48000})
    original_detector = capture.detector
    try:
        capture.start()
        assert capture.sample_rate == capture.detector.sample_rate == preferred
        assert capture.detector is original_detector
        assert capture.config.audio.sample_rate == preferred
        assert streams[0].settings["samplerate"] == preferred
        assert device.checked_rates == [preferred]
    finally:
        capture.stop()


def test_unsupported_device_fails_without_opening_stream_or_changing_configuration(monkeypatch):
    capture, device, streams = capture_with_fake_streams(monkeypatch, 44100, set())
    original_settings = asdict(capture.config.audio)
    with pytest.raises(RuntimeError, match="No supported mono input sample rate"):
        capture.start()
    assert capture._stream is None and streams == []
    assert device.checked_rates == [44100, 48000, 192000, 32000, 22050, 16000]
    assert asdict(capture.config.audio) == original_settings
