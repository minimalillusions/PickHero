"""State and admission boundaries of the additive bass phase veto.

Synthetic backend readings isolate admission/state ownership. The existing
phase-attack tests separately cover the ringing waveform and real rises.
"""
from types import SimpleNamespace

import numpy as np
import pytest

pytest.importorskip("aubio")

from pickhero.audio.detector import PitchDetector
from pickhero.audio.note_utils import midi_to_freq
from pickhero.instruments import BASS_GUITAR, BASS_UKULELE, GUITAR


class NativeOnset:
    def __init__(self, detector):
        self.detector = detector
        self.fire = False

    def __call__(self, block):
        return self.fire

    def get_last(self):
        return (self.detector._hops_seen - .25) * self.detector.hop_size


class Periodicity:
    buf_size = 4096

    def __call__(self, block):
        return [midi_to_freq(40)]

    def get_confidence(self):
        return .99


def controlled(instrument=BASS_GUITAR, backend=PitchDetector, warm=True):
    detector = backend(instrument=instrument)
    detector._pitch = (SimpleNamespace(process=lambda block: (midi_to_freq(40), .99))
                       if instrument.pitch_method == "harmonic" else Periodicity())
    detector._pitch_check = None
    detector._fundamental_present = lambda frequency: True
    detector._onset = NativeOnset(detector)
    block = np.full(detector.hop_size, .01, np.float32)
    if warm:
        for _ in range(2 * detector._phase_windows[-1] + detector._rise_lookback_hops):
            detector.process(block)
    return detector, block


@pytest.mark.parametrize("instrument", [BASS_GUITAR, BASS_UKULELE], ids=lambda item: item.key)
def test_power_growth_does_not_replace_the_raw_threshold(instrument):
    class Observed(PitchDetector):
        phase_calls = 0

        def _raw_phase_rise_supported(self):
            self.phase_calls += 1
            return super()._raw_phase_rise_supported()

    detector, block = controlled(instrument, backend=Observed)
    detector._onset.fire = True
    detector.process(block * 2.5)  # Eight dB; both power averages grow.
    detector._onset.fire = False
    for _ in range(3):
        detector.process(block * 2.5)
    assert not detector._attack.confirmed
    assert detector.phase_calls == 0  # Raw threshold precedes this veto.
    detector.process(block * 10)
    assert detector.phase_calls == 1 and detector._attack.confirmed
    assert [e.kind for e in detector._attack.evidence] == ["level"]


def test_guitar_never_applies_the_phase_veto():
    detector, _ = controlled(GUITAR)
    detector._attack.phase_ready = True
    detector._attack.phase_reference = (1., 1.)
    detector._phase_powers = (0., 0.)
    assert detector._raw_phase_rise_supported()
    assert not detector._phase_power_ring and not detector._phase_power_history


@pytest.mark.parametrize("instrument", [BASS_GUITAR, BASS_UKULELE], ids=lambda item: item.key)
def test_full_historical_window_is_required_before_vetoing(instrument):
    detector, block = controlled(instrument, warm=False)
    ready_hop = detector._phase_windows[-1] + detector._rise_lookback_hops
    for _ in range(ready_hop - 2):
        detector.process(block)
    detector._onset.fire = True
    detector.process(block)
    before = detector._attack
    assert before.pending and not before.phase_ready
    assert detector._raw_phase_rise_supported()
    detector.process(block)
    after = detector._attack
    assert after is not before and after.phase_ready
    assert not detector._raw_phase_rise_supported()


@pytest.mark.parametrize("instrument", [BASS_GUITAR, BASS_UKULELE], ids=lambda item: item.key)
def test_replaced_candidate_keeps_its_own_frozen_power_reference(instrument):
    detector, block = controlled(instrument)
    detector._onset.fire = True
    detector.process(block)
    old = detector._attack
    reference = old.phase_reference
    detector._onset.fire = False
    detector.process(block * 1.1)
    detector.process(block * 1.1)
    assert old.phase_reference is reference
    detector._onset.fire = True
    detector.process(block * 1.1)
    assert detector._attack is not old
    assert old.phase_reference is reference and old.close_reason == "replaced"
    assert detector._attack.phase_reference is not reference


@pytest.mark.parametrize("instrument", [BASS_GUITAR, BASS_UKULELE], ids=lambda item: item.key)
def test_silent_hops_update_history_and_reset_removes_stale_references(instrument):
    detector, block = controlled(instrument)
    detector._onset.fire = True
    detector.process(block)
    retired = detector._attack
    reference = retired.phase_reference
    detector._onset.fire = False
    for _ in range(detector._phase_windows[-1] + detector._rise_lookback_hops):
        detector.process(np.zeros_like(block))
    assert detector._phase_powers == (0., 0.)
    assert all(pair == (0., 0.) for pair in detector._phase_power_history)
    detector.reset()
    assert not detector._phase_power_ring and not detector._phase_power_history
    assert detector._phase_powers == (0., 0.)
    assert not detector._attack.phase_ready and detector._attack.phase_reference == (0., 0.)
    assert retired.phase_reference is reference
    fresh, _ = controlled(instrument, warm=False)
    for owner in (detector, fresh):
        owner._onset = NativeOnset(owner)
        for _ in range(owner._phase_windows[-1] + owner._rise_lookback_hops):
            owner.process(block)
        owner._onset.fire = True
        owner.process(block)
    assert detector._attack.phase_reference == fresh._attack.phase_reference
    assert detector._attack.phase_ready == fresh._attack.phase_ready
    assert detector._raw_phase_rise_supported() == fresh._raw_phase_rise_supported()


@pytest.mark.parametrize("instrument", [BASS_GUITAR, BASS_UKULELE], ids=lambda item: item.key)
def test_power_storage_stays_bounded_over_many_windows(instrument):
    detector, block = controlled(instrument)
    for _ in range(100 * detector._rise_lookback_hops):
        detector.process(block)
    assert len(detector._phase_power_ring) == detector._phase_windows[-1]
    assert detector._phase_power_ring.maxlen == detector._phase_windows[-1]
    assert len(detector._phase_power_history) == detector._rise_lookback_hops
    assert detector._phase_power_history.maxlen == detector._rise_lookback_hops


@pytest.mark.parametrize("instrument,scale,corroborate,evidence", [
    (BASS_GUITAR, 10., False, "bands"), (BASS_UKULELE, 10., False, "bands"),
    (BASS_GUITAR, 4., True, "transient"),
])
def test_independent_band_and_transient_paths_survive_a_raw_veto(instrument, scale, corroborate, evidence):
    class Denied(PitchDetector):
        def _raw_phase_rise_supported(self):
            return False

        def _record_level_evidence(self, db):
            raise AssertionError("The raw veto must remain binding")

    detector, _ = controlled(instrument, backend=Denied, warm=False)
    detector._pitch_check = Periodicity() if corroborate else None
    onset_hop = round(.6 * detector.sample_rate / detector.hop_size)
    time = np.arange(round(.9 * detector.sample_rate)) / detector.sample_rate
    angle = 2 * np.pi * midi_to_freq(40) * time
    signal = .01 * (np.sin(angle) + .5 * np.sin(2 * angle) + .25 * np.sin(3 * angle))
    signal[(onset_hop - 1) * detector.hop_size:] *= scale
    signal = signal.astype(np.float32)
    notes = []
    for offset in range(0, len(signal) - detector.hop_size + 1, detector.hop_size):
        detector._onset.fire = offset // detector.hop_size + 1 == onset_hop
        note = detector.process(signal[offset:offset + detector.hop_size])
        if note is not None and note.is_onset:
            notes.append(note)
    assert [note.midi_note for note in notes] == [40]
    attack = detector._last_closed_attack
    assert any(item.kind == evidence for item in attack.evidence)
    if evidence == "transient":
        assert not attack.confirmed
        assert attack.periodicity_votes.count(40) >= detector.onset_votes_minimum
