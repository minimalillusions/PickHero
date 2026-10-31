"""Keep known sparse-partial bass deficiencies explicit while preserving gates.

Generated signals are sensitivity regressions, not independent recordings or
an accuracy claim. A candidate recovered these signals but increased extra
attacks on an original-recording regression set, so it was not shipped.
"""
from types import SimpleNamespace

import numpy as np
import pytest

pytest.importorskip("aubio")

from pickhero.audio.detector import PitchDetector
from pickhero.audio.note_utils import midi_to_freq
from pickhero.instruments import BASS_GUITAR, BASS_UKULELE


def pluck(rate, midi, cents=0):
    time = np.arange(round(rate * .5)) / rate
    frequency = midi_to_freq(midi) * 2 ** (cents / 1200)
    signal = sum(amplitude * np.sin(2 * np.pi * frequency * (i + 1) * time)
                 for i, amplitude in enumerate((1, .25, .125, .06)))
    signal *= .15 * np.exp(-3 * time)
    fade = round(.005 * rate)
    signal[:fade] *= np.linspace(0, 1, fade)
    signal[-fade:] *= np.linspace(1, 0, fade)
    return np.concatenate([np.zeros(round(rate * .5)), signal,
                           np.zeros(round(rate * .5))]).astype(np.float32)


def strikes(detector, signal):
    found = []
    for offset in range(0, len(signal) - detector.hop_size + 1, detector.hop_size):
        note = detector.process(signal[offset:offset + detector.hop_size])
        if note is not None and note.is_onset:
            onset = ((offset + detector.hop_size) / detector.sample_rate
                     - note.onset_age_hops * detector.hop_size / detector.sample_rate
                     - detector.onset_latency_ms / 1000)
            found.append((note.midi_note, onset))
    return found


@pytest.mark.parametrize("midi,rate", [
    (midi, rate)
    for midi in (28, 33, 38, 45) for rate in (44100, 48000)
])
def test_fundamental_dominated_pluck_is_one_correct_strike(rate, midi):
    detector = PitchDetector(instrument=BASS_GUITAR, sample_rate=rate)
    found = strikes(detector, pluck(rate, midi))
    assert len(found) == 1
    assert found[0][0] == midi
    assert found[0][1] == pytest.approx(.5, abs=.06)


class Periodicity:
    def __init__(self, frequency, confidence):
        self.frequency, self.confidence = frequency, confidence

    def __call__(self, block):
        return [self.frequency]

    def get_confidence(self):
        return self.confidence


def evidence(*, primary=55, confidence=.1, periodic=110, periodic_confidence=.99,
             peak=110, instrument=BASS_GUITAR):
    detector = PitchDetector(instrument=instrument)
    detector._pitch = SimpleNamespace(process=lambda block: (primary, confidence),
                                      dominant_frequency=peak)
    detector._pitch_check = Periodicity(periodic, periodic_confidence)
    detector._onset = lambda block: False
    return detector


@pytest.mark.parametrize("case,expected", [
    ({"primary": 40}, 40),  # A disagreeing backend alone cannot replace a pitch.
    ({"peak": 330}, 55),  # An octave relationship alone is insufficient.
    ({"primary": 70, "peak": 330}, 70),  # No spectral or octave corroboration.
    ({"primary": 110, "periodic": 55, "peak": 330}, 110),
    ({"periodic_confidence": .979}, 55),
    ({"confidence": .3}, 55),  # Never overturn an accepted primary estimate.
    ({"instrument": BASS_UKULELE}, 55),  # Separate rubber-string strategy.
])
def test_disagreement_requires_strong_independent_evidence(case, expected):
    detector = evidence(**case)
    detector.process(pluck(detector.sample_rate, 45)[round(.52 * detector.sample_rate):
                     round(.52 * detector.sample_rate) + detector.hop_size])
    assert detector.last_freq == pytest.approx(expected)


def test_clear_pitch_without_an_attack_never_becomes_a_scored_note():
    detector = evidence(primary=110)
    assert strikes(detector, pluck(detector.sample_rate, 45)) == []


def test_independent_pitch_cannot_bypass_the_volume_gate():
    detector = evidence(primary=110)
    detector._onset = lambda block: True
    assert strikes(detector, pluck(detector.sample_rate, 45) * .0001) == []


def test_independent_pitch_cannot_turn_a_tail_retrigger_into_a_pluck():
    detector = evidence(primary=110)
    detector._onset = lambda block: detector._hops_seen == 30
    # The artificial onset happens after an already sounding tone has filled
    # every attack-reference window; there is no new broadband or band rise.
    time = np.arange(detector.sample_rate) / detector.sample_rate
    signal = (.15 * np.sin(2 * np.pi * 110 * time) * np.exp(-time)).astype(np.float32)
    assert strikes(detector, signal) == []


def test_corroborated_pitch_keeps_the_fractional_onset_sample():
    detector = evidence(primary=110)

    class Attack:
        def __call__(self, block):
            return detector._hops_seen == 45

        def get_last(self):
            return 44.375 * detector.hop_size

    detector._onset = Attack()
    found = strikes(detector, pluck(detector.sample_rate, 45))
    assert len(found) == 1
    assert found[0][0] == 45
    assert found[0][1] == pytest.approx(44.375 * detector.hop_size / detector.sample_rate)
