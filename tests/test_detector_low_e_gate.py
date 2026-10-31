"""A periodic RMS minimum must not erase an otherwise valid low-E attack."""
from types import SimpleNamespace
import wave

import numpy as np
import pytest

pytest.importorskip("aubio")

from pickhero import instruments
from pickhero.audio.detector import PitchDetector
from pickhero.audio.note_utils import midi_to_freq
from tests._recordings import open_strings


def known_pitch_and_first_attack(rate=44100):
    detector = PitchDetector(instrument=instruments.BASS_UKULELE, sample_rate=rate)
    detector._pitch = SimpleNamespace(process=lambda block: (midi_to_freq(28), 1.0))
    detector._pitch_check = None
    calls = iter([True] + [False] * 100)
    detector._onset = lambda block: next(calls)
    return detector


@pytest.mark.parametrize("rate", [44100, 48000])
def test_overtone_rms_trough_does_not_erase_known_attack(rate):
    # Isolate gate/strike state from pitch tracking. The summed partials have
    # constant amplitudes, yet their per-hop RMS dips beneath -45 dB.
    detector = known_pitch_and_first_attack(rate)
    time = np.arange(30 * detector.hop_size) / rate
    signal = sum(weight * np.sin(2 * np.pi * midi_to_freq(28) * harmonic * time + .6)
                 for harmonic, weight in enumerate([.15, .35, 1, .8, .6], start=1))
    signal = (signal / np.sqrt(np.mean(signal ** 2)) * 10 ** (-43 / 20)).astype(np.float32)
    notes, levels = [], []
    for block in signal.reshape(-1, detector.hop_size):
        note = detector.process(block)
        levels.append(detector.last_signal_db)
        if note is not None and note.is_onset:
            notes.append(note)
    assert min(levels) < detector.noise_gate_db < max(levels)
    assert [note.midi_note for note in notes] == [28]


def test_sustained_room_noise_ends_pending_attack_before_a_later_note():
    detector = known_pitch_and_first_attack()
    detector.process(np.full(detector.hop_size, .1, np.float32))
    assert detector._onset_pending
    # Real recording noise, not exact digital zero, must still end the note
    # promptly instead of keeping an old strike alive across the pause.
    noise = np.random.default_rng(12).normal(0, .0005, (5, detector.hop_size)).astype(np.float32)
    for block in noise:
        assert detector.process(block) is None
    assert not detector._onset_pending
    detector._pitch = SimpleNamespace(process=lambda block: (55, 1.0))
    later = [detector.process(np.full(detector.hop_size, .1, np.float32)) for _ in range(8)]
    assert all(note is None or not note.is_onset for note in later)


def test_a_new_attack_still_flushes_votes_after_one_quiet_hop():
    detector = known_pitch_and_first_attack()
    detector.process(np.full(detector.hop_size, .1, np.float32))
    detector._onset_votes = [28, 28]
    assert detector.process(np.full(detector.hop_size, .0005, np.float32)) is None
    assert detector._onset_pending
    detector._onset = lambda block: True
    note = detector.process(np.full(detector.hop_size, .1, np.float32))
    assert note is not None and note.is_onset and note.midi_note == 28
    assert detector._onset_pending
    assert not detector._onset_votes


def test_changed_gate_discards_quiet_history_measured_against_old_threshold():
    detector = known_pitch_and_first_attack()
    detector.set_noise_gate_db(-40)
    detector.process(np.full(detector.hop_size, .1, np.float32))
    for _ in range(2):
        detector.process(np.full(detector.hop_size, 10 ** (-50 / 20), np.float32))
    assert detector._onset_pending
    detector.set_noise_gate_db(-60)
    quiet = np.full(detector.hop_size, 10 ** (-70 / 20), np.float32)
    detector.process(quiet)
    assert detector._onset_pending
    # Reapplying the same setting must not indefinitely prolong a quiet note.
    detector.set_noise_gate_db(-60)
    detector.process(quiet)
    detector.process(quiet)
    assert not detector._onset_pending


@pytest.mark.parametrize("gain", [.5, .25])
def test_recorded_open_strings_keep_quiet_low_e_without_extra_strikes(gain):
    # Uniform attenuation of an existing recording is a gate sensitivity
    # regression, not an independent musician or a new recording.
    with wave.open(str(open_strings()), "rb") as source:
        rate = source.getframerate()
        signal = np.frombuffer(source.readframes(source.getnframes()), "<i2").astype(np.float32) / 32768
    detector = PitchDetector(instrument=instruments.BASS_UKULELE, sample_rate=rate)
    signal *= gain
    found = []
    for offset in range(0, len(signal) - detector.hop_size + 1, detector.hop_size):
        note = detector.process(signal[offset:offset + detector.hop_size])
        if note is not None and note.is_onset:
            at = ((offset + detector.hop_size - note.onset_age_hops * detector.hop_size)
                  / rate * 1000 - detector.onset_latency_ms)
            found.append((at, note.midi_note))
    assert [midi for _, midi in found] == [28] * 3 + [33] * 3 + [38] * 3 + [43] * 3
    for index, (at, _) in enumerate(found):
        assert abs(at - index * 500) <= 60
