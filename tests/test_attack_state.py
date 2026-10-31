"""A pending strike owns its origin, evidence and eventual report outcome.

Controlled backend readings test ownership and lifecycle only. They do not
change admission thresholds or claim improved acoustic recognition.
"""
from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import numpy as np
import pytest

pytest.importorskip("aubio")
from pickhero.audio.detector import PitchDetector
from pickhero.audio.note_utils import midi_to_freq
from pickhero.instruments import BASS_GUITAR, BASS_UKULELE


class Secondary:
    buf_size = 4096

    def __init__(self, midi=28):
        self.midi = midi

    def __call__(self, block):
        return [midi_to_freq(self.midi)]

    def get_confidence(self):
        return .98


class NativeOnset:
    def __init__(self, detector, fire=True):
        self.detector, self.fire = detector, fire

    def __call__(self, block):
        return self.fire

    def get_last(self):
        return (self.detector._hops_seen - .25) * self.detector.hop_size


def block(detector, value=.1):
    return np.full(detector.hop_size, value, np.float32)


def opened(instrument=BASS_GUITAR):
    detector = PitchDetector(instrument=instrument)
    detector._pitch = SimpleNamespace(process=lambda signal: (midi_to_freq(40), .7))
    detector._pitch_check = Secondary()
    detector._fundamental_present = lambda frequency: True
    detector._hops_seen = 30
    detector._onset = NativeOnset(detector)
    assert detector.process(block(detector)) is None
    detector._onset = NativeOnset(detector, fire=False)
    assert detector._attack.pending
    return detector


def armed(*, votes=(40, 40)):
    detector = opened()
    detector._onset_age = 9
    detector._hops_seen += 9
    detector._onset_votes = list(votes)
    detector._bass_pitch_votes = [(28, .98), (28, .97)]
    detector._periodicity_votes = [40, 40]
    detector._strike_confirmed = True
    return detector


def inferred_sample(detector, note):
    return ((detector._hops_seen - note.onset_age_hops) * detector.hop_size
            - detector.onset_latency_ms * detector.sample_rate / 1000)


def test_native_onset_creates_an_immutable_fractional_origin():
    detector = opened()
    origin = detector._attack.origin
    assert origin.opened_hop == 31
    assert origin.onset_sample == 30.75 * detector.hop_size
    assert origin.backdate_hops == pytest.approx(
        .25 - detector.onset_latency_ms * detector.sample_rate / (1000 * detector.hop_size))
    with pytest.raises(FrozenInstanceError):
        origin.onset_sample = 0
    with pytest.raises(FrozenInstanceError):
        origin.opened_hop = 0


def test_new_onset_closes_corrected_old_strike_without_sharing_evidence():
    detector = armed()
    old, origin = detector._attack, detector._attack.origin
    old_reference = old.band_reference.copy()
    detector._pitch_check = Secondary(33)
    detector._onset = NativeOnset(detector)
    note = detector.process(block(detector))
    current = detector._attack
    assert note.is_onset and note.midi_note == 28 and note.original_midi_note == 40
    assert inferred_sample(detector, note) == pytest.approx(origin.onset_sample)
    assert current is not old and detector._last_closed_attack is old
    assert current.origin.sequence > origin.sequence and current.origin.opened_hop == 41
    assert current.pending and not old.pending
    assert old.origin is origin and old.close_reason == "replaced"
    assert old.closed_age_hops == pytest.approx(10 + origin.backdate_hops)
    assert old.decided_votes == (40, 40)
    assert old.decision == "accepted" and old.report_outcome == "reported"
    assert current.close_reason is current.decision is current.report_outcome is None
    assert current.votes == current.periodicity_votes == current.bass_pitch_votes == []
    for name in ("votes", "periodicity_votes", "bass_pitch_votes", "band_reference"):
        assert getattr(old, name) is not getattr(current, name)
    current.votes.append(45)
    current.periodicity_votes.append(45)
    current.bass_pitch_votes.append((45, .99))
    current.band_reference.fill(123)
    assert old.votes == [] and old.decided_votes == (40, 40)
    np.testing.assert_array_equal(old.band_reference, old_reference)


@pytest.mark.parametrize("votes", [(), (40,)])
def test_replaced_attack_without_a_result_is_still_closed_and_isolated(votes):
    detector = armed(votes=votes)
    old = detector._attack
    detector._onset = NativeOnset(detector)
    assert detector.process(block(detector)) is None
    assert detector._attack is not old and detector._attack.pending
    assert detector._last_closed_attack is old and not old.pending
    assert old.close_reason == "replaced" and old.decision == "insufficient_votes"
    assert old.decided_votes == votes and old.report_outcome is None
    assert detector._attack.votes == detector._attack.bass_pitch_votes == []


@pytest.mark.parametrize("reason", ["sound_ended", "timeout"])
def test_pending_end_records_original_evidence_and_onset(reason):
    detector = armed()
    old = detector._attack
    if reason == "timeout":
        detector._hops_seen += detector.onset_hold_hops - old.age_hops
        detector._onset_age = detector.onset_hold_hops
    age = old.age_hops + 1
    note = detector.process(block(detector, 0 if reason == "sound_ended" else .1))
    assert note.is_onset and note.midi_note == 28
    assert inferred_sample(detector, note) == pytest.approx(old.origin.onset_sample)
    assert detector._last_closed_attack is old and not old.pending
    assert old.close_reason == reason
    assert old.closed_age_hops == pytest.approx(age + old.origin.backdate_hops)
    assert old.decided_votes == (40, 40) and old.votes == []
    assert old.decision == "accepted" and old.report_outcome == "reported"


@pytest.mark.parametrize("decision", ["accepted", "unconfirmed", "insufficient_votes"])
def test_normal_vote_completion_records_the_actual_decision(decision):
    detector = armed(votes=[40] * 7)
    old = detector._attack
    detector.strike_rise_db = 0  # keep the controlled confirmation value stable
    if decision == "unconfirmed":
        detector._strike_confirmed = False
        detector._transient_candidate = False
        detector._periodicity_votes = []
    elif decision == "insufficient_votes":
        detector._onset_votes = []
        detector._onset_age = detector.onset_hold_hops - 1
    note = detector.process(block(detector))
    assert detector._last_closed_attack is old and old.close_reason == "votes_complete"
    assert old.decision == decision and not old.pending and old.votes == []
    assert old.decided_votes == ((40,) if decision == "insufficient_votes" else (40,) * 8)
    assert note.is_onset is (decision == "accepted")
    assert old.report_outcome == ("reported" if decision == "accepted" else None)


@pytest.mark.parametrize("decision,votes,confirmed", [
    ("accepted", (40, 40), True), ("insufficient_votes", (40,), True),
    ("unconfirmed", (40, 40), False),
])
def test_standalone_flush_consumes_an_attack_once(decision, votes, confirmed):
    detector = armed(votes=votes)
    old = detector._attack
    detector._strike_confirmed = confirmed
    detector._transient_candidate = False
    detector._periodicity_votes = []
    first = detector._flush_votes()
    assert (first is not None) is (decision == "accepted")
    assert not old.pending and old.close_reason == "flush"
    assert old.decision == decision
    assert old.closed_age_hops == pytest.approx(9 + old.origin.backdate_hops)
    assert old.decided_votes == votes and old.votes == []
    assert detector._last_closed_attack is old
    assert detector._flush_votes() is None
    assert old.close_reason == "flush" and old.decided_votes == votes
    if first is not None:
        assert detector._report(first) is first
        assert old.report_outcome == "reported"


def test_repeat_filter_result_belongs_to_old_attack_after_new_one_is_installed():
    detector = armed()
    old = detector._attack
    detector._last_report_note = 40
    detector._last_report_hop = 30
    detector._onset = NativeOnset(detector)
    assert detector.process(block(detector)) is None
    assert detector._attack is not old and detector._attack.pending
    assert detector._last_closed_attack is old
    assert old.decision == "accepted" and old.report_outcome == "repeat_filtered"
    assert detector._attack.report_outcome is None
    assert detector._last_report_note == 40 and detector._last_report_hop == 30
    assert detector.min_note_gap_ms == BASS_GUITAR.min_note_gap_ms == 140


def test_reset_closes_pending_attack_and_allocates_fresh_idle_evidence():
    detector = armed()
    old, origin = detector._attack, detector._attack.origin
    detector.reset()
    current = detector._attack
    assert current is not old and not current.pending
    assert not old.pending and old.close_reason == "reset" and old.origin is origin
    assert old.closed_age_hops == pytest.approx(9 + origin.backdate_hops)
    assert old.decided_votes == (40, 40)
    assert current.votes == current.periodicity_votes == current.bass_pitch_votes == []
    assert current.close_reason is current.decision is current.report_outcome is None
    assert detector._flush_votes() is None
    assert old.close_reason == "reset" and old.decided_votes == (40, 40)
    detector._onset = NativeOnset(detector)
    detector.process(block(detector))
    assert detector._attack is not current and detector._attack.pending
    assert detector._attack.origin.sequence > origin.sequence
    assert detector._attack.origin.opened_hop == 1
    assert detector._attack.origin.onset_sample == .75 * detector.hop_size
    assert old.close_reason == "reset" and old.origin is origin


def test_rejected_quiet_native_onset_cannot_create_a_new_attack_identity():
    detector = opened(BASS_UKULELE)
    old = detector._attack
    detector._onset = NativeOnset(detector)
    detector._onset_votes = []
    assert detector.process(block(detector, 0)) is None
    assert detector._last_closed_attack is old and old.close_reason == "sound_ended"
    assert old.decision == "insufficient_votes"
    assert not detector._attack.pending


def test_legacy_private_views_target_current_attack_only():
    detector = armed()
    current = detector._attack
    assert detector._onset_pending is current.pending
    assert detector._onset_age == current.age_hops
    assert detector._onset_votes is current.votes
    assert detector._bass_pitch_votes is current.bass_pitch_votes
    assert detector._periodicity_votes is current.periodicity_votes
    detector._onset_votes = [38, 38]
    detector._onset_age = 4
    detector._strike_ref_db = -60
    detector._strike_confirmed = False
    assert current.votes == [38, 38] and current.age_hops == 4
    assert current.reference_db == -60 and not current.confirmed
    current.transient_candidate = True
    assert detector._transient_candidate is True


def energy_candidate(monkeypatch, *, reference_db=-40, reference_band=-30,
                     observed_band=-20):
    """Control measured energy while retaining the real admission/lifecycle path."""
    detector = opened()
    detector._pitch = SimpleNamespace(process=lambda signal: (0, 0))
    detector._pitch_check = None
    # Feed the quiet reference as actual audio before controlling its exact
    # dB/band readings. The phase guard must see the same quiet history rather
    # than the louder initialization hop used to open the previous candidate.
    for _ in range(2 * detector._rise_lookback_hops + detector._gate_release_hops):
        detector.process(block(detector, 10 ** (reference_db / 20)))
    detector._attack_bands = [np.array([0]), np.array([1])]
    detector._db_ring.clear()
    detector._db_ring.extend([reference_db] * detector._transient_hops)
    references = [np.full(2, reference_band, dtype=float)
                  for _ in range(detector._transient_hops)]
    detector._band_db_ring.clear()
    detector._band_db_ring.extend(references)
    measured_band = [observed_band]
    monkeypatch.setattr(
        np.fft, "rfft",
        lambda signal: np.full(2, detector._PROBE_NFFT * 10 ** (measured_band[0] / 20)),
    )
    detector._onset = NativeOnset(detector)
    assert detector.process(block(detector, .01)) is None
    detector._onset = NativeOnset(detector, fire=False)
    return detector, measured_band, references


def test_later_level_confirmation_keeps_the_original_reference_interval(monkeypatch):
    detector, _, _ = energy_candidate(
        monkeypatch, reference_db=-45, reference_band=0, observed_band=0,
    )
    attack = detector._attack
    opened_hop = attack.origin.opened_hop
    expected_span = (opened_hop - detector._transient_hops, opened_hop - 1)
    assert attack.reference_span == expected_span
    assert not attack.confirmed and attack.evidence == []
    detector.process(block(detector, .02))
    assert not attack.confirmed
    detector.process(block(detector, .1))
    assert attack.confirmed and len(attack.evidence) == 1
    evidence = attack.evidence[0]
    assert evidence.kind == "level" and evidence.observed_hop == opened_hop + 2
    assert (evidence.reference_first_hop, evidence.reference_last_hop) == expected_span
    assert evidence.reference == (-45,)
    assert evidence.observed == pytest.approx((-20,), abs=1e-5)
    assert attack.reference_span == expected_span


def test_transient_evidence_is_frozen_at_the_observed_rise(monkeypatch):
    detector, measured_band, references = energy_candidate(monkeypatch)
    old = detector._attack
    assert old.transient_candidate and not old.confirmed
    assert len(old.evidence) == 1
    evidence = old.evidence[0]
    assert evidence.kind == "transient"
    assert evidence.observed_hop == old.origin.opened_hop
    reference_hop = evidence.observed_hop - detector._transient_hops
    assert evidence.reference_first_hop == evidence.reference_last_hop == reference_hop
    assert evidence.reference == (-30, -30)
    assert evidence.observed == pytest.approx((-20, -20))
    assert evidence.bands == (0, 1)
    with pytest.raises(FrozenInstanceError):
        evidence.observed_hop = 0
    measured_band[0] = -5
    detector._onset = NativeOnset(detector)
    detector.process(block(detector, .01))
    assert detector._attack is not old and old.close_reason == "replaced"
    assert detector._transient_evidence is not evidence
    assert detector._transient_evidence.observed_hop == evidence.observed_hop + 1
    assert detector._attack.evidence is not old.evidence
    for reference in references:
        reference.fill(123)
    detector._attack.band_reference.fill(456)
    assert old.evidence == [evidence]
    assert evidence.reference == (-30, -30)
    assert evidence.observed == pytest.approx((-20, -20))


@pytest.mark.parametrize("confirmation", ["level", "bands"])
def test_energy_evidence_stays_bounded_through_later_frames_and_close(monkeypatch, confirmation):
    detector, measured_band, _ = energy_candidate(monkeypatch)
    attack = detector._attack
    first = attack.evidence[0]
    # Repeated eligible transient observations must not append frame history.
    for _ in range(2):
        detector.process(block(detector, .01))
        assert attack.evidence == [first]
    measured_band[0] = -5 if confirmation == "bands" else -20
    value = .1 if confirmation == "level" else .01
    detector.process(block(detector, value))
    assert attack.confirmed
    assert [item.kind for item in attack.evidence] == ["transient", confirmation]
    frozen = tuple(attack.evidence)
    for _ in range(detector.onset_hold_hops + 4):
        detector.process(block(detector, value))
        assert tuple(attack.evidence) == frozen
    assert not attack.pending and attack.close_reason == "timeout"
    assert attack.decision == "insufficient_votes"
