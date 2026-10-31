"""Causal bounds and power units for note-specific ukulele attack evidence."""
from collections import deque

import numpy as np
import pytest

pytest.importorskip("aubio")

from pickhero.audio.attack import AttackOrigin
from pickhero.audio.detector import PitchDetector
from pickhero.audio.note_utils import midi_to_freq
from pickhero.instruments import BASS_GUITAR, BASS_UKULELE, GUITAR


def armed():
    detector = PitchDetector(instrument=BASS_UKULELE)
    reference_end = 2 * detector.buf_size
    native = reference_end + detector.hop_size / 4
    current_end = int(np.ceil((native + detector.buf_size) / detector.hop_size)) * detector.hop_size
    detector._hops_seen = current_end // detector.hop_size
    detector.last_signal_db = -30.
    detector._last_report_note = 33
    detector._attack.origin = AttackOrigin(1, int(np.ceil(native / detector.hop_size)), native, .25)
    detector._attack.pending = True
    detector._attack.confirmed = False
    detector._attack.votes = [28, 28]
    detector._attack.periodicity_votes = [28, 28]
    power = np.zeros_like(detector._partial_bins)
    reference = np.zeros_like(power)
    # Two separate E harmonics, away from the earlier A's harmonics. Their
    # 20 dB rises and summed power qualify; H2 alone is below the absolute floor.
    for harmonic, total in ((2, 1e-6), (6, 1e-5)):
        mask = abs(detector._partial_bins - midi_to_freq(28) * harmonic) <= detector.sample_rate / detector.buf_size / 2
        power[mask] = total / mask.sum()
        reference[mask] = total / 100 / mask.sum()
    detector._partial_history.extend(((reference_end, reference), (current_end, power)))
    return detector, native


@pytest.mark.parametrize("instrument", [GUITAR, BASS_GUITAR], ids=lambda item: item.key)
def test_other_profiles_have_no_partial_history_or_extra_fft(instrument, monkeypatch):
    detector = PitchDetector(instrument=instrument)
    assert detector._partial_ring is None
    assert not hasattr(detector, "_partial_history")
    detector._pitch = type("Pitch", (), {
        "process": lambda self, data: (0., 0.), "__call__": lambda self, data: [0.],
        "get_confidence": lambda self: 0.,
    })()
    detector._pitch_check = detector._onset = None
    detector.strike_rise_db = 0
    monkeypatch.setattr(np.fft, "rfft", lambda *a, **k: pytest.fail("Unexpected extra FFT"))
    detector.process(np.zeros(detector.hop_size, np.float32))


@pytest.mark.parametrize("rate", [44100, 48000, 96000, 192000])
def test_partial_power_is_correctly_normalized_and_history_is_bounded(rate):
    detector = PitchDetector(instrument=BASS_UKULELE, sample_rate=rate)
    time = np.arange(detector.hop_size * (detector._partial_history.maxlen + 3)) / rate
    signal = (.03 * np.sin(2 * np.pi * 55 * time)).astype(np.float32)
    for offset in range(0, len(signal), detector.hop_size):
        detector.process(signal[offset:offset + detector.hop_size])
    assert len(detector._partial_history) == detector._partial_history.maxlen
    block = (detector._partial_ring - detector._partial_ring.mean()) * detector._partial_window
    power = detector._partial_history[-1][1]
    full = np.abs(np.fft.rfft(block, n=detector._partial_nfft)) ** 2 / (detector._partial_nfft * detector.buf_size)
    # The retained prefix contains only usable partial neighborhoods. Parseval
    # applies to the complete spectrum; the truncated last bin is not Nyquist.
    np.testing.assert_array_equal(power, full[:len(power)])
    weighted = 2 * full.sum() - full[0] - full[-1]
    assert weighted == pytest.approx(np.mean(block.astype(float) ** 2), rel=2e-7)
    doubled = np.abs(np.fft.rfft(block, n=detector._partial_nfft * 2)) ** 2 / (detector._partial_nfft * 2 * detector.buf_size)
    # NumPy preserves float32 here; FFT/reduction roundoff is a few ULPs.
    assert weighted == pytest.approx(2 * doubled.sum() - doubled[0] - doubled[-1],
                                     rel=8 * np.finfo(power.dtype).eps, abs=0.)
    detector.reset()
    assert not detector._partial_history and not detector._partial_ring.any()


@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), -float("inf")])
def test_invalid_input_cannot_leave_partial_reference_energy(invalid):
    detector = PitchDetector(instrument=BASS_UKULELE)
    detector.process(np.full(detector.hop_size, .01, np.float32))
    values = np.zeros(detector.hop_size, np.float32)
    values[5] = invalid
    assert detector.process(values) is None
    assert not detector._partial_history and not detector._partial_ring.any()
    assert detector._hops_seen == 0


def test_partial_confirmation_keeps_native_time_and_owned_provenance():
    detector, native = armed()
    owner, origin = detector._attack, detector._attack.origin
    assert detector._corroborated_note_energy(28)
    evidence, = owner.evidence
    assert detector._attack.origin is origin
    assert evidence.kind == "partials" and evidence.bands == (2, 6)
    assert len(evidence.observed) == len(evidence.reference) == 2
    assert evidence.reference_last_hop * detector.hop_size <= native
    detector._close_attack("votes_complete", "accepted")
    assert not detector._corroborated_note_energy(28)
    assert owner.evidence == [evidence]
    detector.reset()
    assert owner.evidence == [evidence] and not detector._attack.evidence


@pytest.mark.parametrize("kind", [
    "missing_native", "short_reference", "short_current", "future_reference_only",
    "one_periodicity_vote", "same_pitch", "already_confirmed",
])
def test_partial_confirmation_requires_complete_causal_windows_and_new_pitch(kind):
    detector, native = armed()
    entries = list(detector._partial_history)
    if kind == "missing_native":
        detector._attack.origin = AttackOrigin(1, 1, None, 0.)
    elif kind == "short_reference":
        entries[0] = (detector.buf_size - 1, entries[0][1])
    elif kind == "short_current":
        entries[-1] = (int(native + detector.buf_size) - 1, entries[-1][1])
    elif kind == "future_reference_only":
        entries[0] = (int(native) + 1, entries[0][1])
    elif kind == "one_periodicity_vote":
        detector._attack.periodicity_votes = [28]
    elif kind == "same_pitch":
        detector._last_report_note = 28
    else:
        detector._attack.confirmed = True
    detector._partial_history = deque(entries, maxlen=detector._partial_history.maxlen)
    assert not detector._corroborated_note_energy(28)
    assert not detector._attack.evidence


@pytest.mark.parametrize("cause", ["nongrowing_loud_partial", "inaudible_sum", "relative_floor"])
def test_only_individually_growing_partials_can_supply_audible_sum(cause):
    detector, _ = armed()
    reference, current = [power for _, power in detector._partial_history]
    if cause == "nongrowing_loud_partial":
        mask = abs(detector._partial_bins - midi_to_freq(28) * 6) <= detector.sample_rate / detector.buf_size / 2
        reference[mask] = current[mask]
    elif cause == "inaudible_sum":
        for _, power in detector._partial_history:
            power *= .001
    else:
        detector.last_signal_db = -5.
    assert not detector._corroborated_note_energy(28)
