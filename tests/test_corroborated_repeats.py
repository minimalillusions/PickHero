"""A close repetition needs its own unused energy pulse and local rise."""
from dataclasses import replace
from types import SimpleNamespace

import numpy as np
import pytest

pytest.importorskip("aubio")
from pickhero.audio.attack import AttackOrigin
from pickhero.audio.detector import DetectedNote, PitchDetector
from pickhero.audio.note_utils import midi_to_freq
from pickhero.audio.repeats import EnergyPulse, RepeatCorroborator
from pickhero.audio.waveform_prediction import WaveformHistory
from pickhero.instruments import BASS_GUITAR, BASS_UKULELE, GUITAR
from tests.test_audio_trace import tracing
from tests.test_detector_refinement import passage


def prepared():
    detector = PitchDetector(instrument=BASS_UKULELE)
    rate, native = detector.sample_rate, 10000
    end = 32 * detector.hop_size
    signal = np.sin(2*np.pi*midi_to_freq(28)*np.arange(end)/rate).astype(np.float32)
    signal[:native] *= .005
    signal[native:] *= .05
    detector._waveform_history.append(signal, end)
    detector._repeat_corroborator.pulses.append(EnergyPulse(native, 10240))
    detector._hops_seen = 32
    detector._last_report_note, detector._last_report_hop = 28, 13
    owner = detector._attack
    owner.origin = AttackOrigin(2, 22, native, 0.)
    owner.pending, owner.confirmed, owner.votes = True, True, [28]*3
    owner.close("replaced", "accepted")
    age = (end-native)/detector.hop_size - detector.onset_latency_ms*rate/1000/detector.hop_size
    note = detector._prepare_report(DetectedNote(28, midi_to_freq(28), .8, "E1", True, age))
    return detector, note, owner


def test_repeat_keeps_native_time_own_evidence_and_primary_suppression_anchor():
    detector, note, owner = prepared()
    old = detector._last_report_hop, detector._last_report_note
    detector._attack = type(owner)(AttackOrigin(3, 32, 15000, 0))
    assert detector._report(note) is note
    assert (detector._last_report_hop, detector._last_report_note) == old
    assert owner.report_outcome == "reported_repeat"
    assert detector._attack.repeat_evidence is None
    evidence = owner.repeat_evidence
    assert evidence.reference_start_sample < evidence.energy_sample < evidence.end_sample
    assert evidence.end_sample <= detector._waveform_history.end_sample
    assert evidence.rise_db > 19 and evidence.observed_sample >= evidence.energy_sample
    assert detector._report_candidate is None
    assert detector._report(note) is None  # the energy pulse is already used


@pytest.mark.parametrize("condition", ["quiet", "no_rise", "weak_pitch", "missing_pulse",
    "consumed", "wrong_time", "missing_native", "missing_history", "incomplete_after", "unaccepted"])
def test_incomplete_or_borrowed_evidence_keeps_the_repeat_filter(condition):
    detector, note, owner = prepared()
    pulse = detector._repeat_corroborator.pulses[0]
    if condition == "quiet": detector.noise_gate_db = -10
    elif condition == "no_rise":
        end = detector._waveform_history.end_sample
        detector._waveform_history.reset()
        time = np.arange(end)/detector.sample_rate
        detector._waveform_history.append((.05*np.sin(2*np.pi*midi_to_freq(28)*time)).astype(np.float32),end)
    elif condition == "weak_pitch":
        note = replace(note, confidence=.1); detector._report_candidate = note, owner
    elif condition == "missing_pulse": detector._repeat_corroborator.pulses.clear()
    elif condition == "consumed": pulse.consumed = True
    elif condition == "wrong_time": pulse.sample -= 5000
    elif condition == "missing_native": owner.origin = replace(owner.origin,onset_sample=None)
    elif condition == "missing_history": detector._waveform_history.reset()
    elif condition == "incomplete_after":
        detector._waveform_history.reset()
        detector._waveform_history.append(np.full(11000,.05,np.float32),11000)
    elif condition == "unaccepted": owner.decision = "insufficient_votes"
    assert detector._report(note) is None
    assert owner.report_outcome == "repeat_filtered" and owner.repeat_evidence is None


def test_ordinary_report_consumes_its_energy_pulse_without_creating_an_extra():
    detector, note, owner = prepared()
    detector._last_report_hop = -100
    assert detector._report(note) is note
    assert owner.report_outcome == "reported" and owner.repeat_evidence is None
    assert detector._repeat_corroborator.pulses[0].consumed
    assert detector._repeat_corroborator.corroborate(owner.origin.onset_sample,
        note.frequency, detector._waveform_history, detector.noise_gate_db) is None


@pytest.mark.parametrize("invalid", [None, float("nan"), float("inf")])
def test_reset_or_bad_audio_discards_old_energy_and_waveform_history(invalid):
    detector, _, _ = prepared()
    old = detector._repeat_corroborator
    if invalid is None: detector.reset()
    else: detector.process(np.full(detector.hop_size, invalid, np.float32))
    assert detector._repeat_corroborator is not old
    assert not detector._repeat_corroborator.pulses
    assert detector._waveform_history.filled == 0
    assert detector._attack.repeat_evidence is None


@pytest.mark.parametrize("profile", [GUITAR, BASS_GUITAR])
def test_other_instruments_have_no_additional_energy_detector(profile):
    assert PitchDetector(instrument=profile)._repeat_corroborator is None


def test_optional_energy_backend_failure_keeps_primary_detection(monkeypatch):
    import pickhero.audio.detector as module
    original = module.aubio.onset
    def unsupported(method,*args):
        if method == "energy": raise RuntimeError("unsupported")
        return original(method,*args)
    monkeypatch.setattr(module.aubio,"onset",unsupported)
    detector = PitchDetector(instrument=BASS_UKULELE)
    assert detector._onset is not None and detector._repeat_corroborator is None
    assert detector.process(np.zeros(detector.hop_size,np.float32)) is None


@pytest.mark.parametrize("native", [-1, float("nan"), float("inf"), 10000])
def test_invalid_or_future_energy_timestamps_cannot_enter_history(native):
    class Onset:
        def __call__(self,block): return True
        def get_last(self): return native
    helper = RepeatCorroborator(Onset(),44100)
    helper.process(np.zeros(512,np.float32),512)
    assert not helper.pulses


def test_energy_history_remains_bounded_and_expires_old_pulses():
    class Onset:
        sample = 0
        def __call__(self,block): self.sample += len(block); return True
        def get_last(self): return self.sample
    onset = Onset()
    helper = RepeatCorroborator(onset,44100)
    block = np.zeros(512,np.float32)
    for i in range(2000): helper.process(block,(i+1)*512)
    assert len(helper.pulses) == 16
    assert all(p.sample >= onset.sample-44100*.5 for p in helper.pulses)


def test_real_fast_repetitions_have_owned_evidence_without_trace_side_effects():
    signal,rate,truth = passage(137,25,False)
    plain = PitchDetector(instrument=BASS_UKULELE,sample_rate=rate)
    traced = tracing.TracedDetector(instrument=BASS_UKULELE,sample_rate=rate)
    found = tracing.stream(traced,signal)
    assert found == tracing.stream(plain,signal)
    assert len(found) == 8
    repeated = [r for r in traced.trace if r['kind']=='report' and r['outcome']=='accepted_repeat']
    assert len(repeated) == 2
    for row,index in zip(repeated,(3,6)):
        assert row['admitted'] and row['attack']['repeat_evidence']['rise_db'] >= 9
        assert row['note']['onset_s']+.5 == pytest.approx(truth[index][0]/1000,abs=.05)


def test_all_eight_fast_plucks_are_individually_recognized():
    signal,rate,_ = passage(137,25,False)
    detector = PitchDetector(instrument=BASS_UKULELE,sample_rate=rate)
    assert len(tracing.stream(detector,signal)) == 8
