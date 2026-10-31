"""A spectral retrigger must not steal an unfinished ukulele attack."""
from collections import deque
from dataclasses import replace

import numpy as np
import pytest

pytest.importorskip("aubio")

from pickhero.audio.attack import AttackOrigin
from pickhero.audio.detector import PitchDetector
from pickhero.audio.note_utils import midi_to_freq
from pickhero.instruments import BASS_GUITAR, BASS_UKULELE, GUITAR


def unfinished():
    detector = PitchDetector(instrument=BASS_UKULELE)
    hop = detector.hop_size
    origin = 16 * hop + hop / 4
    native = origin + 6 * hop
    detector._hops_seen = 26
    detector.last_signal_db = -30.
    detector._last_report_note = 33
    attack = detector._attack
    attack.origin = AttackOrigin(1, 19, origin, 0.75)
    attack.pending, attack.confirmed, attack.age_hops = True, False, 6
    reference = np.zeros_like(detector._partial_bins)
    before_second, current = reference.copy(), reference.copy()
    for harmonic in (2, 6):
        mask = abs(detector._partial_bins - midi_to_freq(28) * harmonic) <= detector.sample_rate / detector.buf_size / 2
        reference[mask] = 1e-7 / mask.sum()
        current[mask] = 1e-5 / mask.sum()
        before_second[mask] = (1e-5 if harmonic == 6 else 1e-7) / mask.sum()
    detector._partial_history.extend(((16 * hop, reference), (22 * hop, before_second),
                                     (26 * hop, current)))
    return detector, native


def retain(detector, native):
    return detector._retain_pending_attack(native, midi_to_freq(28), .95)


def test_retention_preserves_owner_time_references_and_expiry_without_confirming_pitch():
    detector, native = unfinished()
    owner, origin = detector._attack, detector._attack.origin
    reference = owner.band_reference.copy()
    assert retain(detector, native)
    assert detector._attack is owner and owner.origin is origin
    assert owner.age_hops == 6 and not owner.confirmed
    assert not owner.votes and not owner.periodicity_votes
    np.testing.assert_array_equal(owner.band_reference, reference)
    decision, = owner.continuations
    assert decision.native_sample == native and decision.midi_note == 28
    assert decision.reference_end_sample <= origin.onset_sample
    assert decision.before_retrigger_end_sample <= native
    assert decision.current_end_sample - detector.buf_size >= origin.onset_sample
    assert decision.harmonics == (2, 6)
    # Continuation evidence alone never reports a note.
    assert detector._flush_votes() is None
    assert owner.decision == "insufficient_votes"


def test_retained_attack_still_requires_matching_periodicity_and_reports_original_sample():
    detector, native = unfinished()
    assert retain(detector, native)
    owner = detector._attack
    owner.votes = [28] * 8
    owner.periodicity_votes = [28, 28]
    # Set age/backdate consistently with the original native timestamp.
    owner.age_hops = detector._hops_seen - owner.origin.opened_hop
    backdate = (owner.origin.opened_hop * detector.hop_size - owner.origin.onset_sample) / detector.hop_size
    backdate -= detector.onset_latency_ms * detector.sample_rate / 1000 / detector.hop_size
    owner.origin = replace(owner.origin, backdate_hops=backdate)
    note = detector._flush_votes()
    assert note is not None and note.is_onset and note.midi_note == 28
    inferred = ((detector._hops_seen - note.onset_age_hops) * detector.hop_size
                - detector.onset_latency_ms * detector.sample_rate / 1000)
    assert inferred == pytest.approx(owner.origin.onset_sample)
    assert detector._report(note) is note
    assert detector._flush_votes() is None


@pytest.mark.parametrize("reason", ["no_yin", "one_yin", "different_pitch_with_level_rise"])
def test_retention_cannot_bypass_final_pitch_and_attack_confirmation(reason):
    detector, native = unfinished()
    assert retain(detector, native)
    detector._attack.votes = [28, 28]
    if reason == "one_yin":
        detector._attack.periodicity_votes = [28]
    elif reason == "different_pitch_with_level_rise":
        detector._attack.votes = [33, 33]
        detector._attack.periodicity_votes = [33, 33]
        detector._attack.confirmed = True
    assert detector._flush_votes() is None
    assert detector._attack.decision == "unconfirmed"


@pytest.mark.parametrize("reason", [
    "no_earlier_growth", "only_one_growing_partial", "inaudible", "same_previous_pitch",
    "already_confirmed", "enough_votes", "expired", "closed", "not_pending",
    "no_native", "short_post_window", "no_pre_window", "already_other_pitch",
])
def test_close_onsets_alone_never_justify_retention(reason):
    detector, native = unfinished()
    owner = detector._attack
    reference, middle, current = [power for _, power in detector._partial_history]
    if reason == "no_earlier_growth": middle[:] = reference
    elif reason == "only_one_growing_partial": current[:] = middle
    elif reason == "inaudible":
        for _, power in detector._partial_history: power *= .001
    elif reason == "same_previous_pitch": detector._last_report_note = 28
    elif reason == "already_confirmed": owner.confirmed = True
    elif reason == "enough_votes": owner.votes = [28, 28]
    elif reason == "expired": owner.age_hops = detector.onset_hold_hops
    elif reason == "closed": owner.close("replaced", "insufficient_votes")
    elif reason == "not_pending": owner.pending = False
    elif reason == "no_native": owner.origin = replace(owner.origin, onset_sample=None)
    elif reason == "short_post_window":
        items = list(detector._partial_history)
        items[-1] = (int(owner.origin.onset_sample + detector.buf_size) - 1, current)
        detector._partial_history = deque(items)
    elif reason == "no_pre_window": detector._partial_history.popleft()
    elif reason == "already_other_pitch":
        assert retain(detector, native)
        owner.continuations[0] = replace(owner.continuations[0], midi_note=38)
    count = len(owner.continuations)
    assert not retain(detector, native)
    assert len(owner.continuations) == count


@pytest.mark.parametrize("gap_ms", [0, -1, 93, 137])
def test_later_independent_candidates_are_not_suppressed(gap_ms):
    detector, _ = unfinished()
    native = detector._attack.origin.onset_sample + gap_ms * detector.sample_rate / 1000
    detector._hops_seen = 40  # all proposed native times are in the past
    assert not retain(detector, native)


@pytest.mark.parametrize("field", ["native", "frequency", "confidence"])
@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), -float("inf")])
def test_nonfinite_values_never_retain_an_attack(field, invalid):
    detector, native = unfinished()
    args = dict(native=native, frequency=midi_to_freq(28), confidence=.95)
    args[field] = invalid
    assert not detector._retain_pending_attack(**args)


@pytest.mark.parametrize("profile", [GUITAR, BASS_GUITAR], ids=lambda value: value.key)
def test_other_profiles_never_use_ukulele_continuation(profile):
    detector = PitchDetector(instrument=profile)
    assert not detector._retain_pending_attack(1000., midi_to_freq(28), .95)


def test_reset_drops_retention_history_but_preserves_closed_snapshot():
    detector, native = unfinished()
    assert retain(detector, native)
    owner = detector._attack
    detector.reset()
    assert owner.close_reason == "reset" and len(owner.continuations) == 1
    assert not detector._attack.continuations and not detector._partial_history


@pytest.mark.parametrize("cents", [-25, 0, 25])
def test_all_eight_known_overlapping_plucks_keep_their_own_native_times(cents):
    from tests.test_detector_refinement import passage, strikes
    signal, rate, expected = passage(375, cents, True)
    found = strikes(signal, rate)
    assert len(found) == len(expected) == 8
    for (actual_time, actual_pitch), (expected_time, expected_pitch) in zip(found, expected):
        assert actual_pitch == expected_pitch
        assert abs(actual_time - expected_time) <= 50
