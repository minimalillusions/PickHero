"""An early onset may precede usable amplitude, without bypassing the gate."""
from dataclasses import replace
from types import SimpleNamespace

import numpy as np
import pytest

pytest.importorskip("aubio")

from pickhero.audio.detector import PitchDetector
from pickhero.audio.note_utils import midi_to_freq
from pickhero.instruments import BASS_GUITAR


class OneAttack:
    def __init__(self, hop_size, at=4):
        self.hop_size = hop_size
        self.at = at
        self.calls = 0
        self.sample = 0

    def __call__(self, block):
        fired = self.calls == self.at
        self.calls += 1
        if fired:
            self.sample = (self.calls - .25) * self.hop_size
        return fired

    def get_last(self):
        return self.sample


def known_pitch(rate, *, confidence=1.0, quiet_attacks=True, gate=-45):
    profile = BASS_GUITAR if quiet_attacks else replace(BASS_GUITAR, transient_rise_db=0)
    detector = PitchDetector(instrument=profile, sample_rate=rate, noise_gate_db=gate)
    detector._pitch = SimpleNamespace(process=lambda block: (midi_to_freq(33), confidence))
    detector._pitch_check = None
    detector._onset = OneAttack(detector.hop_size)
    return detector


def block(detector, index, db):
    if db is None:
        return np.zeros(detector.hop_size, np.float32)
    samples = np.arange(detector.hop_size) + index * detector.hop_size
    signal = np.sin(2 * np.pi * midi_to_freq(33) * samples / detector.sample_rate)
    signal *= 10 ** (db / 20) / np.sqrt(np.mean(signal ** 2))
    return signal.astype(np.float32)


def feed(detector, levels):
    found = []
    for index, level in enumerate(levels):
        note = detector.process(block(detector, index, level))
        if note is not None and note.is_onset:
            time = ((index + 1 - note.onset_age_hops) * detector.hop_size / detector.sample_rate
                    - detector.onset_latency_ms / 1000)
            found.append((time, note.midi_note))
    return found


@pytest.mark.parametrize("rate", [44100, 48000, 96000])
@pytest.mark.parametrize("gate", [-45, -33])
def test_quiet_leading_onset_survives_until_pitch_crosses_the_saved_gate(rate, gate):
    detector = known_pitch(rate, gate=gate)
    result = feed(detector, [None] * 4 + [gate - 6] + [gate + 12] * 25)
    assert len(result) == 1
    assert result[0][1] == 33
    assert result[0][0] == pytest.approx(detector._onset.sample / rate)
    assert detector.noise_gate_db == gate
    assert detector.onset_latency_ms == BASS_GUITAR.onset_latency_ms


def test_other_profile_settings_keep_their_previous_gate_behavior():
    detector = known_pitch(44100, quiet_attacks=False)
    assert feed(detector, [None] * 4 + [-51] + [-33] * 25) == []


def test_quiet_attack_never_scores_without_audible_pitch():
    detector = known_pitch(44100)
    assert feed(detector, [None] * 4 + [-51] * 25) == []
    assert not detector._onset_pending


def test_expired_quiet_attack_cannot_score_a_later_unrelated_note():
    detector = known_pitch(44100)
    assert feed(detector, [None] * 4 + [-51] * 8 + [-33] * 25) == []


def test_quiet_attack_still_requires_confident_pitch_votes():
    detector = known_pitch(44100, confidence=.1)
    assert feed(detector, [None] * 4 + [-51] + [-33] * 25) == []


def test_reset_discards_a_quiet_pending_attack():
    detector = known_pitch(44100)
    assert feed(detector, [None] * 4 + [-51]) == []
    assert detector._onset_pending
    detector._pitch.reset = lambda: None
    detector.reset()
    detector._onset = OneAttack(detector.hop_size, at=1000)
    assert feed(detector, [-33] * 25) == []
