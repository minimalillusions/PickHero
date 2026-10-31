"""Quiet-repeat evidence must belong to an audible, isolated pitch candidate."""
from dataclasses import replace

import numpy as np
import pytest

pytest.importorskip("aubio")
from pickhero.audio.attack import AttackOrigin, PendingAttack
from pickhero.audio.detector import PitchDetector
from pickhero.audio.note_utils import midi_to_freq
from pickhero.audio.quiet_repeats import (
    ComplexPulse, CompetingPitch, QuietRepeatCorroborator, RepeatSpectrum,
    inspect_repeat_spectrum,
)
from pickhero.instruments import BASS_UKULELE, BASS_GUITAR, GUITAR
from tests.test_audio_trace import tracing
from tests.test_detector_refinement import passage


def prepared():
    helper = QuietRepeatCorroborator(None, 44100, 512)
    attack = PendingAttack(AttackOrigin(7, 22, 10000, .5), pending=True,
                           confirmed=True, votes=[28], age_hops=9)
    helper.pulses.append(ComplexPulse(10100, 11264))
    for hop in (30, 31):
        helper.observe(attack, hop, midi_to_freq(28), midi_to_freq(28), .88,
                       4096, 4, True, lambda f: True)
    return helper, attack


def spectrum(native, midi, hop):
    return RepeatSpectrum(9728, hop * 512, ())


def test_single_use_evidence_survives_later_uncertainty_and_uses_first_pair():
    helper, attack = prepared()
    helper.observe(attack, 32, 0., 0., .1, 4096, 4, True, lambda f: True)
    helper.observe(attack, 35, midi_to_freq(28), midi_to_freq(28), .87, 4096, 4, True, lambda f: True)
    evidence = helper.corroborate(attack, 28, spectrum)
    assert evidence.native_sample == 10000 and evidence.complex_sample == 10100
    assert [p.observed_hop for p in evidence.pitch] == [30, 31, 35]
    assert evidence.spectrum.observed_end_sample == 31 * 512
    assert evidence.confidence == .87
    assert helper.corroborate(attack, 28, spectrum) is None


@pytest.mark.parametrize("invalid", ["quiet", "uncertain", "disagreement", "nan", "incomplete", "unknown_window", "early", "no_fundamental"])
def test_unusable_pitch_frames_cannot_complete_the_evidence(invalid):
    helper, attack = prepared()
    helper.agreements.pop()
    primary = periodicity = midi_to_freq(28)
    confidence, window, audible, fundamental = .88, 4096, True, True
    if invalid == "quiet": audible = False
    elif invalid == "uncertain": confidence = .84
    elif invalid == "disagreement": periodicity = midi_to_freq(33)
    elif invalid == "nan": confidence = float("nan")
    elif invalid == "incomplete": window = 10000
    elif invalid == "unknown_window": window = 0
    elif invalid == "early": attack.age_hops = 0
    elif invalid == "no_fundamental": fundamental = False
    helper.observe(attack, 31, primary, periodicity, confidence, window, 4,
                   audible, lambda f: fundamental)
    assert helper.corroborate(attack, 28, spectrum) is None


@pytest.mark.parametrize("invalid", ["closed", "unconfirmed", "zero_votes", "two_votes", "owner", "different_pitch", "missing_pulse", "consumed", "remote_pulse", "nonconsecutive", "missing_spectrum", "competing_pitch"])
def test_incomplete_or_borrowed_evidence_cannot_rescue_a_candidate(invalid):
    helper, attack = prepared()
    inspect = spectrum
    if invalid == "closed": attack.close("timeout", "insufficient_votes")
    elif invalid == "unconfirmed": attack.confirmed = False
    elif invalid == "zero_votes": attack.votes.clear()
    elif invalid == "two_votes": attack.votes.append(28)
    elif invalid == "owner": attack.origin = replace(attack.origin, sequence=8)
    elif invalid == "different_pitch": helper.agreements[1] = replace(helper.agreements[1], midi_note=33)
    elif invalid == "missing_pulse": helper.pulses.clear()
    elif invalid == "consumed": helper.pulses[0].consumed = True
    elif invalid == "remote_pulse": helper.pulses[0].sample = 14000
    elif invalid == "nonconsecutive": helper.agreements[1] = replace(helper.agreements[1], observed_hop=33)
    elif invalid == "missing_spectrum": inspect = lambda *a: None
    elif invalid == "competing_pitch":
        inspect = lambda *a: RepeatSpectrum(9728,15872,(CompetingPitch(33,(2,5),(1e-7,1e-7),(1e-4,1e-4)),))
    assert helper.corroborate(attack, 28, inspect) is None
    if helper.pulses and invalid != "consumed": assert not helper.pulses[0].consumed


def test_new_owner_clears_previous_pitch_evidence():
    helper, attack = prepared()
    attack.origin = replace(attack.origin, sequence=8)
    helper.observe(attack, 32, 0., 0., 0., 4096, 4, False, lambda f: True)
    assert not helper.agreements


def test_spectral_veto_uses_only_the_designated_window_and_disjoint_partials():
    bins = np.arange(0, 2500, 2.)
    reference = np.full(len(bins), 1e-12)
    current = reference.copy()
    for harmonic in (2, 5):
        current[abs(bins - midi_to_freq(33) * harmonic) <= 4] = 1e-4
    history = [(8192, reference), (15360, current), (15872, reference)]
    result = inspect_repeat_spectrum(history,bins,9000,28,15360,4096,44100,28,48,8,14,-45)
    assert result.reference_end_sample == 8192
    assert any(item.midi_note == 33 for item in result.competing)
    assert inspect_repeat_spectrum(history,bins,9000,28,15872,4096,44100,28,48,8,14,-45).competing == ()
    assert inspect_repeat_spectrum(history,bins,9000,28,14848,4096,44100,28,48,8,14,-45) is None
    shared = reference.copy()
    for harmonic in (4, 8): shared[abs(bins-midi_to_freq(28)*harmonic)<=4] = 1e-4
    result = inspect_repeat_spectrum([(8192,reference),(15360,shared)],bins,9000,28,15360,4096,44100,28,48,8,14,-45)
    assert not any(item.midi_note == 33 for item in result.competing)


def test_fast_quiet_repeat_has_owned_trace_evidence_and_native_timestamp():
    signal,rate,truth = passage(137,25,False)
    detector = tracing.TracedDetector(instrument=BASS_UKULELE,sample_rate=rate)
    events = tracing.stream(detector,signal)
    quiet = [r for r in detector.trace if r['kind']=='report' and r['outcome']=='accepted_quiet_repeat']
    assert len(events) == 8 and len(quiet) == 1
    evidence = quiet[0]['attack']['quiet_repeat_evidence']
    assert not evidence['spectrum']['competing']
    assert evidence['spectrum']['observed_end_sample'] <= quiet[0]['hop']*detector.hop_size
    assert quiet[0]['note']['onset_s']+.5 == pytest.approx(truth[1][0]/1000,abs=.05)


def test_quiet_a_attack_cannot_borrow_the_ringing_e_pitch():
    signal,rate,truth = passage(137,19,True)
    signal = np.r_[np.zeros(644,np.float32),signal*.8]
    detector = PitchDetector(instrument=BASS_UKULELE,sample_rate=rate)
    events = tracing.stream(detector,signal)
    # Both A attacks have independent evidence now. The earlier one must
    # name A, never borrow the E that still rings under it.
    assert [event['midi_note'] for event in events] == [28, 33, 33, 28]
    for event,index in zip(events,(0,1,3,6),strict=True):
        assert event['onset_s'] + .5 == pytest.approx(truth[index][0]/1000 + 644/rate, abs=.05)


def test_tremolo_trough_cannot_supply_inaudible_pitch_votes():
    rate = 44100
    t = np.arange(round(rate*1.5))/rate
    phases = np.random.default_rng(10009).uniform(0,2*np.pi,24)[16:]
    signal = np.zeros_like(t)
    for h,phase in enumerate(phases,1):
        signal += np.exp(-t*2)/h*(np.sin(2*np.pi*midi_to_freq(28)*h*t+phase)
                   +.7*np.sin(2*np.pi*midi_to_freq(28)*h*t-phase))
    signal *= .55+.45*np.cos(2*np.pi*7*t)
    signal *= np.minimum(t/.004,1)*np.minimum((1.5-t)/.02,1)
    signal = (signal/np.max(abs(signal))*.2).astype(np.float32)
    signal = np.r_[np.zeros(rate//2,np.float32),signal,np.zeros(rate//2,np.float32)]
    detector = tracing.TracedDetector(instrument=BASS_UKULELE)
    assert len(tracing.stream(detector,signal)) == 3  # Existing modulation errors, no new fourth.
    assert not any(r['kind']=='report' and r['outcome']=='accepted_quiet_repeat' for r in detector.trace)


@pytest.mark.parametrize("profile", [GUITAR,BASS_GUITAR])
def test_other_profiles_do_not_build_a_quiet_repeat_backend(profile):
    assert PitchDetector(instrument=profile)._quiet_repeat_corroborator is None


@pytest.mark.parametrize("invalid", [None,float("nan"),float("inf")])
def test_restart_or_invalid_audio_discards_auxiliary_evidence(invalid):
    detector = PitchDetector(instrument=BASS_UKULELE)
    old = detector._quiet_repeat_corroborator
    old.pulses.append(ComplexPulse(0,512))
    if invalid is None: detector.reset()
    else: detector.process(np.full(detector.hop_size,invalid,np.float32))
    assert detector._quiet_repeat_corroborator is not old
    assert not detector._quiet_repeat_corroborator.pulses
    assert not detector._quiet_repeat_corroborator.agreements


def test_optional_complex_backend_failure_preserves_primary_detector(monkeypatch):
    from pickhero.audio import detector as module
    original = module.aubio.onset
    def failing(method,*args):
        if method == 'complex': raise RuntimeError('Unavailable')
        return original(method,*args)
    monkeypatch.setattr(module.aubio,'onset',failing)
    detector = PitchDetector(instrument=BASS_UKULELE)
    assert detector._quiet_repeat_corroborator is None
    assert detector._onset is not None and detector._repeat_corroborator is not None


def test_pulses_and_pitch_storage_remain_bounded_and_future_pulses_are_ignored():
    class Onset:
        sample = 0
        def __call__(self,block): self.sample += 512; return True
        def get_last(self): return self.sample
    helper = QuietRepeatCorroborator(Onset(),44100,512)
    for hop in range(1000): helper.process(None,(hop+1)*512)
    assert len(helper.pulses) == 16
    assert all(p.sample >= helper.onset.sample-44100*.5 for p in helper.pulses)
    class BadOnset:
        sample = float('nan')
        def __call__(self,block): return True
        def get_last(self): return self.sample
    helper.onset = BadOnset()
    for invalid in (float('nan'),float('inf'),-1,600000):
        helper.onset.sample = invalid
        helper.process(None,512000)
    assert len(helper.pulses) == 16
    helper, attack = prepared()
    for hop in range(32,132):
        helper.observe(attack,hop,midi_to_freq(28),midi_to_freq(28),.9,4096,4,True,lambda f: True)
    assert len(helper.agreements) == 8
