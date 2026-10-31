"""Old ringing energy must not masquerade as a freshly plucked uke string."""
from collections import deque
from dataclasses import replace

import numpy as np
import pytest

pytest.importorskip("aubio")

from pickhero.audio.attack import AttackEvidence, AttackOrigin
from pickhero.audio.detector import DetectedNote, PitchDetector
from pickhero.audio.note_utils import midi_to_freq
from pickhero.instruments import BASS_GUITAR, BASS_UKULELE, GUITAR


def returning():
    detector = PitchDetector(instrument=BASS_UKULELE)
    detector._hops_seen = 32
    detector.last_signal_db = -30.
    detector._last_report_note = 33
    detector._last_report_hop = 3
    detector._reported_uke_pitches.update((28, 33))
    attack = detector._attack
    attack.origin = AttackOrigin(2, 20, 18 * detector.hop_size + 128, .75)
    attack.age_hops = 12
    attack.votes = [28] * 8
    attack.pending, attack.confirmed = True, True
    attack.evidence.append(AttackEvidence("bands", 25, 12, 19, (-70., -60.), (-50., -40.), (0, 2)))
    reference = np.zeros_like(detector._partial_bins)
    for h, total in ((2, 1e-5), (6, 1e-6)):
        mask = abs(detector._partial_bins - midi_to_freq(28) * h) <= detector.sample_rate / detector.buf_size / 2
        reference[mask] = total / mask.sum()
    detector._partial_history.extend(((18 * detector.hop_size, reference),
                                     (32 * detector.hop_size, reference * .5)))
    note = DetectedNote(28, midi_to_freq(28), .98, "E1", True, 12.75)
    detector._close_attack("votes_complete", "accepted")
    detector._prepare_report(note)
    return detector, note, attack


def test_decay_veto_keeps_origin_and_report_history_on_the_correct_owner():
    detector, note, owner = returning()
    origin, seen = owner.origin, detector._reported_uke_pitches.copy()
    old_hop, old_pitch = detector._last_report_hop, detector._last_report_note
    # Reporting an old candidate must still use that owner after a replacement.
    detector._attack = type(owner)(AttackOrigin(3, 32, 31 * detector.hop_size, .5))
    assert detector._report(note) is None
    assert owner.origin is origin and owner.report_outcome == "sustain_filtered"
    assert detector._last_report_hop == old_hop and detector._last_report_note == old_pitch
    assert detector._reported_uke_pitches == seen and detector._report_candidate is None
    assert detector._attack.report_outcome is None and not detector._attack.evidence
    evidence = owner.evidence[-1]
    assert evidence.kind == "decaying_partials" and evidence.bands == (2, 6)
    assert evidence.reference_last_hop * detector.hop_size <= origin.onset_sample
    assert all(now < before for now, before in zip(evidence.observed, evidence.reference))


@pytest.mark.parametrize("reason", [
    "not_previously_heard", "same_last_pitch", "level_rise", "no_band_rise", "no_owner",
    "no_native", "no_history", "incomplete_reference", "reference_after_native", "incomplete_post",
    "one_partial", "below_absolute_floor", "below_relative_floor", "rising_partial", "flat_partial",
])
def test_insufficient_decay_evidence_preserves_the_original_report(reason):
    detector, note, owner = returning()
    entries = list(detector._partial_history)
    reference, current = [power for _, power in entries]
    mask = abs(detector._partial_bins - midi_to_freq(28) * 6) <= detector.sample_rate / detector.buf_size / 2
    if reason == "not_previously_heard": detector._reported_uke_pitches.remove(28)
    elif reason == "same_last_pitch": detector._last_report_note = 28
    elif reason == "level_rise": owner.evidence.append(AttackEvidence("level", 23, 12, 19, (-50.,), (-30.,)))
    elif reason == "no_band_rise": owner.evidence.clear()
    elif reason == "no_owner": detector._report_candidate = None
    elif reason == "no_native": owner.origin = replace(owner.origin, onset_sample=None)
    elif reason == "no_history": entries.clear()
    elif reason == "incomplete_reference": entries[0] = (detector.buf_size - 1, reference)
    elif reason == "reference_after_native": entries[0] = (int(owner.origin.onset_sample) + 1, reference)
    elif reason == "incomplete_post": entries[-1] = (int(owner.origin.onset_sample + detector.buf_size) - 1, current)
    elif reason == "one_partial": reference[mask] = current[mask] = 0.
    elif reason == "below_absolute_floor":
        detector.last_signal_db = -80.
        reference *= .01
        current *= .01
    elif reason == "below_relative_floor": detector.last_signal_db = -5.
    elif reason == "rising_partial": current[mask] = reference[mask] * 2
    elif reason == "flat_partial": current[mask] = reference[mask]
    detector._partial_history = deque(entries)
    assert detector._report(note) is note
    assert owner.report_outcome != "sustain_filtered"


def test_newly_audible_partial_prevents_veto_even_when_other_partials_decay():
    detector, note, owner = returning()
    detector._last_report_note = 43  # G2 leaves several E1 partials separated.
    reference, current = [power for _, power in detector._partial_history]
    reference.fill(0.)
    current.fill(0.)
    for harmonic in (3, 6):
        mask = abs(detector._partial_bins - midi_to_freq(28) * harmonic) <= detector.sample_rate / detector.buf_size / 2
        reference[mask] = 1e-5 / mask.sum()
        current[mask] = 5e-6 / mask.sum()
    mask = abs(detector._partial_bins - midi_to_freq(28) * 4) <= detector.sample_rate / detector.buf_size / 2
    current[mask] = 1e-5 / mask.sum()  # absent in the reference, now clearly present
    assert detector._report(note) is note
    assert owner.report_outcome == "reported"


@pytest.mark.parametrize("profile", [GUITAR, BASS_GUITAR], ids=lambda item: item.key)
def test_other_profiles_do_not_track_ukulele_returns(profile):
    detector = PitchDetector(instrument=profile)
    assert detector._reported_uke_pitches is None
    assert detector._report(DetectedNote(40, midi_to_freq(40), .99, "E2", True)) is not None
    assert detector._reported_uke_pitches is None


@pytest.mark.parametrize("invalid", [None, float("nan"), float("inf")])
def test_reset_or_invalid_audio_discards_previously_reported_pitch_history(invalid):
    detector, note, owner = returning()
    if invalid is None:
        detector.reset()
    else:
        block = np.zeros(detector.hop_size, np.float32)
        block[0] = invalid
        assert detector.process(block) is None
    assert detector._reported_uke_pitches == set() and not detector._partial_history
    assert detector._report_candidate is None


def unmasking_signal(rate, phase=0., repluck=None):
    signal = np.zeros(round(3.5 * rate), np.float32)
    components = [(28, .5, 2.5, .02, 3., phase), (33, .9, .6, .12, .11, phase)]
    if repluck is not None:
        onset, new_phase = repluck
        components.append((28, onset, .5, .005, .75, new_phase))
    for midi, onset, duration, amplitude, decay, angle_offset in components:
        time = np.arange(round(duration * rate)) / rate
        angle = 2 * np.pi * midi_to_freq(midi) * time + angle_offset
        grain = sum(weight * np.sin(harmonic * angle)
                    for harmonic, weight in enumerate((.15, 1., .7, .4, .2, .1, .07, .05), 1))
        grain *= amplitude * np.exp(-time / decay)
        attack, release = round(.005 * rate), round(.02 * rate)
        grain[:attack] *= np.linspace(0, 1, attack)
        grain[-release:] *= np.linspace(1, 0, release)
        start = round(onset * rate)
        signal[start:start + len(grain)] += grain.astype(np.float32)
    return signal


def strikes(signal, rate):
    detector = PitchDetector(instrument=BASS_UKULELE, sample_rate=rate)
    events = []
    for offset in range(0, len(signal) - detector.hop_size + 1, detector.hop_size):
        note = detector.process(signal[offset:offset + detector.hop_size])
        if note is not None and note.is_onset:
            at = ((offset + detector.hop_size - note.onset_age_hops * detector.hop_size) / rate
                  - detector.onset_latency_ms / 1000)
            events.append((at, note.midi_note))
    return events


@pytest.mark.parametrize("rate", [44100, 48000, 96000])
@pytest.mark.parametrize("phase", [0., np.pi / 2])
def test_return_of_old_e_does_not_report_a_late_new_attack(rate, phase):
    found = strikes(unmasking_signal(rate, phase), rate)
    assert any(pitch == 28 and abs(at - .5) < .15 for at, pitch in found)
    assert any(pitch == 33 and abs(at - .9) < .15 for at, pitch in found)
    assert not [(at, pitch) for at, pitch in found if pitch == 28 and at > .9]
    # The separate early-onset regressions cover borrowed A confirmation.


@pytest.mark.parametrize("onset", [1.2, 1.4])
@pytest.mark.parametrize("phase", [0., np.pi, 3 * np.pi / 2])
def test_genuine_quiet_e_replucks_survive_over_the_ringing_old_e(onset, phase):
    found = strikes(unmasking_signal(96000, repluck=(onset, phase)), 96000)
    for expected_time, expected_pitch in ((.5, 28), (.9, 33), (onset, 28)):
        assert any(pitch == expected_pitch and abs(at - expected_time) < .15 for at, pitch in found)
