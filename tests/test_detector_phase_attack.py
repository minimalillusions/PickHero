"""A ringing bass waveform is not a fresh strike on every RMS recovery."""
from types import SimpleNamespace

import numpy as np
import pytest

pytest.importorskip("aubio")

from pickhero.audio.detector import PitchDetector
from pickhero.audio.note_utils import midi_to_freq
from pickhero.instruments import BASS_GUITAR, BASS_UKULELE


def scenario(rate, phase, attacks=(), instrument=BASS_GUITAR):
    detector = PitchDetector(instrument=instrument, sample_rate=rate)
    frequency = midi_to_freq(28)
    detector._pitch = SimpleNamespace(process=lambda block: (frequency, .99))
    detector._pitch_check = None
    onset_hops = [round(time * rate / detector.hop_size) for time in (attacks or (.6,))]

    class Onset:
        def __call__(self, block):
            return detector._hops_seen in onset_hops

        def get_last(self):
            return (detector._hops_seen - .25) * detector.hop_size

    detector._onset = Onset()
    time = np.arange(round(1.3 * rate)) / rate
    angle = 2 * np.pi * frequency * time + phase
    # A few aligned partials produce a narrow crest and a deep trough each
    # cycle. With no attacks, the waveform has a constant amplitude envelope.
    signal = np.sin(angle) * ((1 + np.cos(angle)) / 2) ** 4
    amplitude = np.full_like(time, .03 if attacks else .3)
    for hop in onset_hops if attacks else ():
        start = (hop - 1) * detector.hop_size / rate
        amplitude[(time >= start) & (time < start + .13)] = .3
    signal = (signal * amplitude).astype(np.float32)
    found = []
    for offset in range(0, len(signal) - detector.hop_size + 1, detector.hop_size):
        block = signal[offset:offset + detector.hop_size]
        note = detector.process(block)
        expected_db = 20 * np.log10(np.sqrt(np.mean(block ** 2)))
        assert detector.last_signal_db == pytest.approx(expected_db)
        if note is not None and note.is_onset:
            found.append((note.midi_note,
                          (offset + detector.hop_size - note.onset_age_hops * detector.hop_size) / rate
                          - detector.onset_latency_ms / 1000))
    return detector, found, [(hop - .25) * detector.hop_size / rate for hop in onset_hops]


@pytest.mark.parametrize("rate", [44100, 48000])
@pytest.mark.parametrize("phase", [0., np.pi / 2])
@pytest.mark.parametrize("instrument", [BASS_GUITAR, BASS_UKULELE], ids=lambda item: item.key)
def test_continuous_low_bass_does_not_retrigger_at_standard_input_rates(rate, phase, instrument):
    # Synthetic causal regression, not an independent recording. A fake
    # spectral onset isolates the amplitude guard from aubio's peak picker.
    _, found, _ = scenario(rate, phase, instrument=instrument)
    assert found == []


@pytest.mark.parametrize("rate", [44100, 48000, 96000, 192000])
@pytest.mark.parametrize("phase", [0., np.pi / 2])
def test_real_energy_rises_still_report_separate_strikes_at_original_timestamps(rate, phase):
    detector, found, expected = scenario(rate, phase, (.6, .85))
    assert [midi for midi, _ in found] == [28, 28]
    assert [time for _, time in found] == pytest.approx(expected, abs=1e-9)
    assert detector.noise_gate_db == BASS_GUITAR.noise_gate_db
    assert detector.onset_latency_ms == BASS_GUITAR.onset_latency_ms


@pytest.mark.parametrize("rate,phase", [
    (44100, 0.), (44100, np.pi / 2), (48000, 0.), (48000, np.pi / 2),
    (96000, 0.), (96000, np.pi / 2),
    (192000, 0.),
    (192000, np.pi / 2),
])
def test_ukulele_real_energy_rises_keep_their_original_timestamps(rate, phase):
    detector, found, expected = scenario(rate, phase, (.6, .85), instrument=BASS_UKULELE)
    assert [midi for midi, _ in found] == [28, 28]
    assert [time for _, time in found] == pytest.approx(expected, abs=1e-9)
    assert detector.noise_gate_db == BASS_UKULELE.noise_gate_db
    assert detector.onset_latency_ms == BASS_UKULELE.onset_latency_ms


@pytest.mark.parametrize("rate", [44100, 48000])
@pytest.mark.parametrize("phase", [0., np.pi / 2])
def test_ukulele_early_attack_survives_before_complete_phase_history(rate, phase):
    _, found, expected = scenario(rate, phase, (.08,), instrument=BASS_UKULELE)
    assert [midi for midi, _ in found] == [28]
    assert [time for _, time in found] == pytest.approx(expected, abs=1e-9)
