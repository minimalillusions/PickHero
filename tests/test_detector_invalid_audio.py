"""Invalid audio breaks analysis continuity without inventing quiet samples."""
from dataclasses import asdict

import numpy as np
import pytest

pytest.importorskip("aubio")

from pickhero.audio.detector import PitchDetector
from pickhero.instruments import BASS_GUITAR, BASS_UKULELE, GUITAR


class ForbiddenDSP:
    def __call__(self, block):
        pytest.fail("Invalid audio reached a DSP backend")

    process = __call__


def tone(detector, hops):
    time = np.arange(hops * detector.hop_size) / detector.sample_rate
    return (.1 * np.sin(2 * np.pi * 110 * time)).astype(np.float32)


@pytest.mark.parametrize("profile", [GUITAR, BASS_GUITAR, BASS_UKULELE])
def test_invalid_input_rebuilds_real_dsp_and_releases_pending_attack(profile):
    for invalid in (np.nan, np.inf, -np.inf):
        detector = PitchDetector(instrument=profile)
        signal = tone(detector, 32)
        for offset in range(0, len(signal), detector.hop_size):
            detector.process(signal[offset:offset + detector.hop_size])
        assert np.any(detector._probe_ring)
        # A confirmed but undecided event must not cross a missing block.
        old = type(detector._attack)(detector._attack.origin, pending=True,
                                    confirmed=True, votes=[45, 45])
        detector._attack = old
        original_backends = (detector._pitch, detector._pitch_check, detector._onset)
        blocked = ForbiddenDSP()
        detector._pitch = detector._pitch_check = detector._onset = blocked
        corrupted = np.zeros(detector.hop_size, np.float32)
        corrupted[len(corrupted) // 2] = invalid
        assert detector.process(corrupted) is None
        assert old.close_reason == "reset" and old.report_outcome is None
        assert detector._attack is not old and not detector._attack.pending
        assert not detector._attack.votes and not detector._attack.bass_pitch_votes
        assert detector._report_candidate is None
        assert detector._hops_seen == detector._quiet_hops == 0
        assert not detector._db_ring and not detector._band_db_ring
        assert not detector._phase_power_ring and not detector._phase_power_history
        assert not np.any(detector._probe_ring)
        assert detector.last_signal_db == -120 and detector.last_freq == 0
        for rebuilt, original in zip((detector._pitch, detector._pitch_check, detector._onset),
                                     original_backends):
            assert rebuilt is not blocked
            assert original is None or rebuilt is not original

        # Compare actual rebuilt pitch/onset DSP with a genuinely fresh instance.
        fresh = PitchDetector(instrument=profile)
        subsequent = np.concatenate((np.zeros(4 * detector.hop_size, np.float32),
                                     tone(detector, 36)))
        for offset in range(0, len(subsequent), detector.hop_size):
            block = subsequent[offset:offset + detector.hop_size]
            recovered, expected = detector.process(block), fresh.process(block)
            assert (asdict(recovered) if recovered else None) == (asdict(expected) if expected else None)
            assert detector.last_signal_db == fresh.last_signal_db
            assert detector.last_freq == fresh.last_freq
            assert detector.last_confidence == fresh.last_confidence


def test_finite_float64_that_overflows_float32_is_rejected_before_dsp():
    detector = PitchDetector(instrument=BASS_GUITAR)
    detector._pitch = detector._pitch_check = detector._onset = ForbiddenDSP()
    block = np.full(detector.hop_size, 1e100, np.float64)
    assert np.isfinite(block).all()
    with np.errstate(over="ignore"):
        assert detector.process(block) is None
    assert detector._hops_seen == detector._quiet_hops == 0
    assert not detector._attack.pending and not np.any(detector._probe_ring)


class Controlled(PitchDetector):
    """Stable pitch and fractional native origin isolate timestamp arithmetic."""

    def _build(self):
        owner = self

        class Pitch:
            def process(self, block):
                assert np.isfinite(block).all()
                return 110., .99

        class Onset:
            def __call__(self, block):
                assert np.isfinite(block).all()
                return owner._hops_seen == 3

            def get_last(self):
                return (owner._hops_seen - .375) * owner.hop_size

        self._pitch, self._pitch_check, self._onset = Pitch(), None, Onset()

    def _fundamental_present(self, frequency):
        return True


@pytest.mark.parametrize("rate", [44100, 48000])
def test_fractional_onset_after_invalid_gap_uses_external_chunk_clock(rate):
    detector = Controlled(instrument=BASS_GUITAR, sample_rate=rate)
    hop = detector.hop_size
    signal = np.full(20 * hop, .1, np.float32)
    signal[:2 * hop] = 0
    signal[3 * hop:4 * hop] = np.nan
    signal[4 * hop:6 * hop] = 0
    found = []
    for offset in range(0, len(signal), hop):
        note = detector.process(signal[offset:offset + hop])
        if note is not None and note.is_onset:
            # Capture time advances across the invalid fourth hop. The DSP
            # starts a fresh segment; its age still refers to this chunk.
            timestamp = (1000 + (offset + hop) * 1000 / rate
                         - note.onset_age_hops * hop * 1000 / rate
                         - detector.onset_latency_ms)
            found.append((note.midi_note, timestamp))
    assert len(found) == 1 and found[0][0] == 45
    assert found[0][1] == pytest.approx(1000 + (4 + 2.625) * hop * 1000 / rate, abs=1e-8)
    assert detector._hops_seen == 16
