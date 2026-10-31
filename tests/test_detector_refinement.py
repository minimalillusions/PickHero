"""Streaming/DSP regressions, using transforms of an existing recording.

Transforms exercise sensitivity to timing and tuning; they are not additional
independent recordings. No microphone or private user files are required.
"""
from types import SimpleNamespace
import wave

import numpy as np
import pytest

pytest.importorskip("aubio")

from pickhero import instruments
from pickhero.audio.detector import DetectedNote, PitchDetector
from tests._recordings import open_strings


def fixture_audio():
    path = open_strings()
    with wave.open(str(path), "rb") as audio:
        return (np.frombuffer(audio.readframes(audio.getnframes()), "<i2")
                .astype(np.float32) / 32768, audio.getframerate())


def change_rate(signal, ratio):
    return np.interp(np.arange(0, len(signal) - 1, ratio),
                     np.arange(len(signal)), signal).astype(np.float32)


def strikes(signal, rate, instrument=instruments.BASS_UKULELE):
    detector = PitchDetector(sample_rate=rate, instrument=instrument)
    found = []
    for offset in range(0, len(signal) - detector.hop_size + 1, detector.hop_size):
        note = detector.process(signal[offset:offset + detector.hop_size])
        if note is not None and note.is_onset:
            at = ((offset + detector.hop_size) / rate * 1000
                  - note.onset_age_hops * detector.hop_size / rate * 1000
                  - detector.onset_latency_ms)
            found.append((at, note.midi_note))
    return found


@pytest.mark.parametrize("cents", [-25, -10, 0, 10, 25])
def test_slight_detuning_keeps_all_recorded_low_e_plucks(cents):
    audio, rate = fixture_audio()
    ratio = 2 ** (cents / 1200)
    found = strikes(change_rate(audio, ratio), rate)
    low_e = [(at, midi) for at, midi in found if midi == 28]
    assert len(low_e) == 3
    for index, (at, _) in enumerate(low_e):
        assert abs(at - index * 500 / ratio) <= 60
    # One weak G remains unresolved in two transforms; do not disguise that
    # limitation as perfect general recognition. No invented extra is allowed.
    assert 11 <= len(found) <= 12


def passage(interval_ms, cents, alternating):
    audio, rate = fixture_audio()
    ratio = 2 ** (cents / 1200)
    signal = np.zeros(round((.5 + interval_ms * .007 + 1) * rate), np.float32)
    expected = []
    for count in range(8):
        string = count % 2 if alternating else 0
        take = string * 3 + count % 3
        grain = audio[round(take * .5 * rate):round((take * .5 + .4) * rate)]
        grain = change_rate(grain, ratio)
        fade = round(rate * .01)
        grain[-fade:] *= np.linspace(1, 0, fade)
        start = round((.5 + count * interval_ms / 1000) * rate)
        signal[start:start + len(grain)] += grain
        expected.append((start / rate * 1000, 33 if string else 28))
    return signal, rate, expected


@pytest.mark.parametrize("cents", [-25, 0, 25])
@pytest.mark.parametrize("alternating", [False, True])
def test_repeated_or_alternating_plucks_have_one_report_each(cents, alternating):
    signal, rate, expected = passage(500, cents, alternating)
    found = strikes(signal, rate)
    assert len(found) == len(expected)
    for actual, want in zip(found, expected):
        assert actual[1] == want[1]
        assert abs(actual[0] - want[0]) <= 60


@pytest.mark.parametrize("cents", [-25, 0, 25])
def test_band_attacks_recover_notes_masked_by_a_ringing_neighbour(cents):
    signal, rate, expected = passage(375, cents, True)
    found = strikes(signal, rate)
    remaining = list(expected)
    matched = []
    for actual in found:
        candidates = [want for want in remaining
                      if actual[1] == want[1] and abs(actual[0] - want[0]) <= 100]
        if candidates:
            want = min(candidates, key=lambda item: abs(actual[0] - item[0]))
            remaining.remove(want)
            matched.append(actual)
    # Six of eight vs three/four with the old broadband gate. Quieter plucks
    # still mask each other; asserting eight would conceal that limitation.
    assert len(matched) >= 6
    assert len(found) == len(matched)


def test_pitch_history_advances_through_silence_and_flushed_strikes():
    detector = PitchDetector(instrument=instruments.BASS_UKULELE)
    seen = []
    detector._pitch = SimpleNamespace(process=lambda block: (seen.append(block.copy()) or (55, .9)))
    detector._pitch_check = None
    detector._onset = lambda block: False
    detector.process(np.full(detector.hop_size, .1, np.float32))
    detector._onset_pending = True
    detector._onset_votes = [33, 33]
    detector._strike_confirmed = True
    assert detector.process(np.zeros(detector.hop_size, np.float32)).is_onset
    detector.process(np.zeros(detector.hop_size, np.float32))
    assert len(seen) == 3
    assert not seen[-1].any() and not seen[-2].any()


def test_variable_pitch_latency_does_not_merge_distinct_repeated_strikes():
    detector = PitchDetector(instrument=instruments.BASS_UKULELE)
    # First strike took 15 hops to settle; the next needed only five. Their
    # report times are close, but their actual attacks are 232 ms apart.
    first = DetectedNote(28, 41.2, .9, "E1", True, 15)
    second = DetectedNote(28, 41.2, .9, "E1", True, 5)
    detector._hops_seen = 25
    assert detector._report(first) is first
    detector._hops_seen = 35
    assert detector._report(second) is second


def test_backend_onset_timestamp_is_preserved_at_fractional_hop_precision():
    detector = PitchDetector(instrument=instruments.BASS_UKULELE)
    detector._pitch = SimpleNamespace(process=lambda block: (55, .9))
    detector._pitch_check = None
    detector._hops_seen = 20

    class Onset:
        def __call__(self, block):
            return True

        def get_last(self):
            return 9031

    detector._onset = Onset()
    detector.process(np.full(detector.hop_size, .1, np.float32))
    detector._onset_votes = [33, 33]
    note = detector._flush_votes()
    inferred_sample = ((detector._hops_seen - note.onset_age_hops) * detector.hop_size
                       - detector.onset_latency_ms * detector.sample_rate / 1000)
    assert inferred_sample == pytest.approx(9031)


def test_reset_discards_previous_spectrum_and_live_readings():
    detector = PitchDetector(instrument=instruments.BASS_UKULELE)
    detector.process(np.full(detector.hop_size, .1, np.float32))
    detector.reset()
    assert not detector._probe_ring.any()
    assert not detector._band_db_ring
    assert detector.last_freq == detector.last_confidence == 0


def test_optional_fft_corroborator_failure_keeps_primary_backend(monkeypatch):
    import pickhero.audio.detector as module

    def unavailable(*args, **kwargs):
        raise RuntimeError("FFT implementation unavailable")

    monkeypatch.setattr(module.aubio, "pitch", unavailable)
    detector = PitchDetector(instrument=instruments.BASS_UKULELE)
    assert detector._pitch_check is None
    assert detector.process(np.zeros(detector.hop_size, np.float32)) is None


@pytest.mark.parametrize("rate", [96000, 192000])
def test_high_rate_guitar_uses_fast_yin_and_retains_low_e(monkeypatch, rate):
    import pickhero.audio.detector as module
    real_pitch = module.aubio.pitch
    calls = []

    def recording_pitch(method, window, hop, sample_rate):
        calls.append((method, window, hop, sample_rate))
        return real_pitch(method, window, hop, sample_rate)

    monkeypatch.setattr(module.aubio, "pitch", recording_pitch)
    detector = PitchDetector(sample_rate=rate, instrument=instruments.GUITAR)
    assert calls[0][0] == "yinfast"
    window = calls[0][1]
    assert window & (window - 1) == 0
    time = np.arange(round(rate * .3)) / rate
    signal = (.4 * np.sin(2 * np.pi * 82.4069 * time)).astype(np.float32)
    for offset in range(0, len(signal) - detector.hop_size + 1, detector.hop_size):
        detector.process(signal[offset:offset + detector.hop_size])
    assert detector.last_freq == pytest.approx(82.4069, rel=.005)
    assert detector.last_confidence >= .9


def test_device_rates_above_analysis_limit_are_negotiated_without_changing_config(monkeypatch):
    from pickhero.audio import input as audio_input
    from pickhero.config import Config
    config = Config()
    config.audio.sample_rate = 384000
    capture = audio_input.AudioCapture(config)
    assert capture.detector.sample_rate == 192000
    assert config.audio.sample_rate == 384000
    monkeypatch.setattr(audio_input.sd, "query_devices",
                        lambda device: {"default_samplerate": 384000})
    checked = []
    monkeypatch.setattr(audio_input.sd, "check_input_settings",
                        lambda **kwargs: checked.append(kwargs["samplerate"]))
    assert audio_input.negotiate_samplerate(0, 384000) == 48000
    assert checked == [48000]


def test_device_without_supported_analysis_rate_reports_an_error(monkeypatch):
    from pickhero.audio import input as audio_input

    def unsupported(**kwargs):
        raise RuntimeError("Device accepts only 384 kHz")

    monkeypatch.setattr(audio_input.sd, "query_devices",
                        lambda device: {"default_samplerate": 384000})
    monkeypatch.setattr(audio_input.sd, "check_input_settings", unsupported)
    with pytest.raises(RuntimeError, match="No supported mono input sample rate"):
        audio_input.negotiate_samplerate(0, 384000)
    with pytest.raises(ValueError, match="up to 192000"):
        PitchDetector(sample_rate=384000)
