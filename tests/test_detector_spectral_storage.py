"""Discarding unused spectrum must preserve every playable partial exactly."""
from dataclasses import replace

import numpy as np
import pytest

pytest.importorskip("aubio")

from pickhero.audio.detector import PitchDetector
from pickhero.audio.note_utils import midi_to_freq
from pickhero.instruments import BASS_UKULELE, with_tuning


@pytest.mark.parametrize("rate", [44100, 48000, 96000, 192000])
@pytest.mark.parametrize("semitones", [-2, 0, 12])
def test_every_playable_partial_keeps_its_complete_original_neighborhood(rate, semitones):
    profile = with_tuning(BASS_UKULELE, {s:m+semitones for s,m in BASS_UKULELE.tuning.items()})
    detector = PitchDetector(instrument=profile, sample_rate=rate)
    complete = np.fft.rfftfreq(detector._partial_nfft, 1. / rate)
    retained = detector._partial_bins
    assert retained.base is None and len(retained) < len(complete)
    np.testing.assert_array_equal(retained, complete[:len(retained)])
    resolution = rate / detector.buf_size
    for midi in range(profile.midi_min, profile.midi_max + 1):
        for harmonic in range(1, profile.harmonics + 1):
            centre = midi_to_freq(midi) * harmonic
            if centre + resolution / 2 >= rate / 2:
                continue
            np.testing.assert_array_equal(
                complete[abs(complete - centre) <= resolution / 2],
                retained[abs(retained - centre) <= resolution / 2])
    # The first omitted bin is outside every neighborhood, including the
    # highest fret's eighth harmonic. Do not trim by open-string range alone.
    assert complete[len(retained)] > midi_to_freq(profile.midi_max) * profile.harmonics + resolution / 2


@pytest.mark.parametrize("rate", [44100, 48000, 96000, 192000])
def test_saved_power_owns_compact_storage_and_preserves_values_hop_by_hop(rate):
    detector = PitchDetector(instrument=BASS_UKULELE, sample_rate=rate)
    rng = np.random.default_rng(8606)
    for _ in range(detector._partial_history.maxlen + 2):
        detector.process(rng.normal(0, .01, detector.hop_size).astype(np.float32))
        end, actual = detector._partial_history[-1]
        block = (detector._partial_ring - detector._partial_ring.mean()) * detector._partial_window
        full = np.abs(np.fft.rfft(block, n=detector._partial_nfft)) ** 2 / (detector._partial_nfft * detector.buf_size)
        np.testing.assert_array_equal(actual, full[:len(actual)])
        assert actual.base is None and actual.flags.owndata
        assert end == detector._hops_seen * detector.hop_size
    assert len(detector._partial_history) == detector._partial_history.maxlen
    assert sum(power.nbytes for _, power in detector._partial_history) < 1024 * 1024
    # Clearing references must retain the compact layout on the next stream.
    detector.reset()
    assert not detector._partial_history
    detector.process(np.zeros(detector.hop_size, np.float32))
    assert len(detector._partial_history[-1][1]) == len(detector._partial_bins)


def test_high_tuning_clamps_to_nyquist_without_changing_the_fft_grid():
    profile = replace(BASS_UKULELE, tuning={1:100, 2:96, 3:91, 4:86})
    detector = PitchDetector(instrument=profile)
    complete = np.fft.rfftfreq(detector._partial_nfft, 1. / detector.sample_rate)
    np.testing.assert_array_equal(detector._partial_bins, complete)
    assert detector._partial_bins.base is None
