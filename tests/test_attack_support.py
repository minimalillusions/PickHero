"""Independent evidence must not turn ambiguity or ringing into scored notes."""
from copy import deepcopy
import json
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("aubio")

from pickhero import practice_advice
from pickhero.audio.detector import PitchDetector
from pickhero.audio.note_utils import midi_to_freq
from pickhero.instruments import BASS_GUITAR, BASS_UKULELE
from tests import test_detector_phase_attack as phase
from tests.test_audio_trace import tracing
from tests.test_detector_refinement import passage
from tests.test_detector_sparse_bass import pluck, strikes
from tests.test_ukulele_pitch_progress import attempt, measurement, passage as state


def test_new_a_has_its_own_complex_onset_and_partial_evidence():
    signal, rate, truth = passage(137, 19, True)
    signal = np.r_[np.zeros(644, np.float32), signal * .8]
    d = tracing.TracedDetector(instrument=BASS_UKULELE)
    events = tracing.stream(d, signal)
    assert [row["midi_note"] for row in events] == [28, 33, 33, 28]
    assert events[2]["onset_s"] + .5 == pytest.approx(truth[3][0]/1000 + 644/rate, abs=.05)
    report = next(r for r in d.trace if r["kind"] == "report" and r["admitted"]
                  and r["note"]["midi_note"] == 33 and r["attack"]["complex_confirmation"] is not None)
    proof = report["attack"]["complex_confirmation"]
    native = report["attack"]["origin"]["onset_sample"]
    assert proof["native_sample"] == native
    assert abs(proof["complex_sample"] - native) <= rate / 55
    assert proof["observed_sample"] <= report["hop"] * d.hop_size
    assert any(e["kind"] == "partials" for e in report["attack"]["evidence"])
    d = PitchDetector(instrument=BASS_UKULELE)
    d._quiet_repeat_corroborator = None
    assert [r["midi_note"] for r in tracing.stream(d, signal)] == [28]


def test_quiet_start_owns_full_period_energy_and_reset_clears_anchor(monkeypatch):
    monkeypatch.setattr(phase, "PitchDetector", tracing.TracedDetector)
    d, found, expected = phase.scenario(192000, 0., (.6, .85), instrument=BASS_UKULELE)
    assert [t for _, t in found] == pytest.approx(expected)
    rows = [r for r in d.trace if r["kind"] == "report" and r["admitted"]]
    proof = rows[0]["attack"]["quiet_start_evidence"]
    assert proof["onset"]["native_sample"] == rows[0]["attack"]["origin"]["onset_sample"]
    assert proof["end_sample"] <= rows[0]["hop"] * d.hop_size
    assert proof["observed_power"] >= proof["reference_power"] * 10 ** (14/10)
    saved = deepcopy(proof)
    d.reset()
    assert d._quiet_start_anchor is None
    assert d._attack.quiet_start_evidence is None
    assert rows[0]["attack"]["quiet_start_evidence"] == saved


def test_quiet_start_cannot_skip_its_independent_onset(monkeypatch):
    def without_support(*args, **kwargs):
        d = PitchDetector(*args, **kwargs)
        d._quiet_repeat_corroborator = None
        return d
    monkeypatch.setattr(phase, "PitchDetector", without_support)
    _, found, expected = phase.scenario(192000, 0., (.6, .85), instrument=BASS_UKULELE)
    assert len(found) == 1 and found[0][1] == pytest.approx(expected[1])


def sparse_evidence():
    d = PitchDetector(instrument=BASS_GUITAR)
    d._attack.preceded_by_quiet = True
    d._attack.confirmed = True
    d._attack.quiet_reference_span = (10, 14)
    d._attack.quiet_reference_peak_db = -60.
    d._attack.bass_pitch_votes = [(38, .95), (38, .99), (38, .999), (38, .998)]
    d._pitch.dominant_frequency = midi_to_freq(38)
    return d


def test_sparse_evidence_keeps_native_identity_and_measured_support():
    d = sparse_evidence()
    origin = d._attack.origin
    assert d._sparse_bass_pitch() == (38, .99)
    proof = d._attack.sparse_pitch_evidence
    assert (proof.quiet_first_hop, proof.quiet_last_hop, proof.quiet_peak_db) == (10, 14, -60.)
    assert proof.periodicity_votes == tuple(d._attack.bass_pitch_votes)
    assert d._attack.origin is origin
    d.reset()
    assert d._attack.sparse_pitch_evidence is None
    assert len(proof.periodicity_votes) == 4


@pytest.mark.parametrize("bad", ["ringing", "unconfirmed", "two_votes", "conflicting_pitch", "weak_final_vote", "other_peak", "nan", "inf", "zero"])
def test_sparse_pitch_requires_all_independent_evidence(bad):
    d = sparse_evidence()
    if bad == "ringing": d._attack.preceded_by_quiet = False
    elif bad == "unconfirmed": d._attack.confirmed = False
    elif bad == "two_votes": d._attack.bass_pitch_votes = [(38, .99)] * 2
    elif bad == "conflicting_pitch": d._attack.bass_pitch_votes[0] = (40, .99)
    elif bad == "weak_final_vote": d._attack.bass_pitch_votes[-1] = (38, .97)
    elif bad == "other_peak": d._pitch.dominant_frequency = midi_to_freq(40)
    else: d._pitch.dominant_frequency = {"nan": np.nan, "inf": np.inf, "zero": 0.}[bad]
    assert d._sparse_bass_pitch() is None
    assert d._attack.sparse_pitch_evidence is None


@pytest.mark.parametrize("rate,midi,cents,interval,other,required", [
    (44100, 45, -19, 113, 45, ()),
    (48000, 33, -19, 113, 38, ()),
    (48000, 45, 17, 113, 45, (1,)),
])
def test_sparse_recovery_cannot_merge_rapid_real_successors(rate, midi, cents, interval, other, required):
    shift = round(interval/1000 * rate)
    signal = np.pad(pluck(rate, midi, cents), (0, shift))
    following = pluck(rate, other, cents)
    signal[shift:shift+len(following)] += following
    truth = [(midi, .5), (other, .5 + shift/rate)]
    found = strikes(PitchDetector(instrument=BASS_GUITAR, sample_rate=rate), signal)
    matched = set()
    for pitch, onset in found:
        candidates = [i for i, (m, t) in enumerate(truth) if i not in matched and m == pitch and abs(onset-t) <= .05]
        assert candidates, "A retained onset invented a pitch or lost its time"
        matched.add(min(candidates, key=lambda i: abs(onset-truth[i][1])))
    assert set(required) <= matched


BEFORE = json.loads((Path(__file__).parent / "fixtures/attack_support_before.json").read_text())


@pytest.mark.parametrize("record", BEFORE, ids=lambda row: str(row["case"]))
def test_new_audio_generations_preserve_history_without_false_progress(record):
    current = practice_advice.input_fingerprint(*measurement(record["case"]))
    old = record["key"]
    changed = record["case"][0] != "guitar"
    assert (current != old) is changed
    history = [attempt(old), attempt(old), attempt(current)]
    before = deepcopy(history)
    if changed:
        assert practice_advice.section_advice(history) is None
        assert practice_advice.comparable_attempts(history, state(), input_key=current) == history[-1:]
        assert practice_advice.comparable_attempts(history, state(), input_key=old) == history[:2]
    else:
        assert practice_advice.section_advice(history)["kind"] == "raise"
    assert history == before
