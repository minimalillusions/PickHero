"""Recover a settling bass attack without stealing a real following pluck."""
from copy import deepcopy
import json
from pathlib import Path

import numpy as np
import pytest

from pickhero import practice_advice
from pickhero.audio.detector import PitchDetector
from pickhero.audio.note_utils import midi_to_freq
from pickhero.audio.waveform_prediction import WaveformHistory
from pickhero.instruments import BASS_GUITAR
from tests.test_audio_trace import tracing
from tests.test_detector_sparse_bass import pluck, strikes
from tests.test_ukulele_pitch_progress import attempt, measurement, passage


def history(rate, midi=33, second=None):
    signal = pluck(rate, midi)
    if second is not None:
        shift = round(.1 * rate)
        following = pluck(rate, second) * .4
        signal = np.pad(signal, (0, shift))
        signal[shift:shift+len(following)] += following
    result = WaveformHistory(rate, 512)
    end = round(.68 * rate)
    for pos in range(0, end, 512):
        result.append(signal[pos:min(end, pos+512)], min(end, pos+512))
    return result, signal


@pytest.mark.parametrize("rate", [44100, 48000, 96000, 192000])
@pytest.mark.parametrize("midi", [33, 38])
def test_settling_tone_uses_only_its_own_past_and_supports_its_pitch(rate, midi):
    captured, _ = history(rate, midi)
    proof = captured.predict_continuation(.485*rate, .555*rate, round(.66*rate))
    assert proof is not None and proof.is_continuation
    assert proof.supports_pitch(midi_to_freq(midi))
    assert not proof.supports_pitch(midi_to_freq(midi+5))
    assert .485*rate < proof.train_start_sample < proof.train_end_sample < .555*rate < proof.end_sample
    assert proof.end_sample <= captured.end_sample
    assert captured.data.nbytes < 400000


@pytest.mark.parametrize("second", [33, 38])
def test_new_attack_changes_forecast_but_not_training(second):
    original, _ = history(44100)
    played, _ = history(44100, second=second)
    a = original.predict_continuation(.485*44100, .555*44100, round(.66*44100))
    b = played.predict_continuation(.485*44100, .555*44100, round(.66*44100))
    assert a.is_continuation and not b.is_continuation
    assert (a.fit_error, a.rank, a.pole_radius) == (b.fit_error, b.rank, b.pole_radius)


@pytest.mark.parametrize("bad", ["reset", "gap", "nan", "future", "past", "short", "reversed", "infinite"])
def test_missing_or_invalid_settled_history_cannot_certify_an_onset(bad):
    captured, _ = history(44100)
    start, native, end = .485*44100, .555*44100, round(.66*44100)
    if bad == "reset": captured.reset()
    elif bad == "gap": captured.append(np.zeros(512, np.float32), captured.end_sample+1024)
    elif bad == "nan": captured.append(np.full(512, np.nan, np.float32), captured.end_sample+512)
    elif bad == "future": end = captured.end_sample+1
    elif bad == "past": start = -1
    elif bad == "short": native = start+100
    elif bad == "reversed": start, native = native, start
    else: native = np.inf
    assert captured.predict_continuation(start, native, end) is None


def test_samples_after_declared_end_cannot_change_the_forecast():
    captured, signal = history(44100)
    end = round(.66*44100)
    expected = captured.predict_continuation(.485*44100, .555*44100, end)
    signal[end:] = np.random.default_rng(927).normal(0, .2, len(signal)-end)
    other = WaveformHistory(44100, 512)
    for pos in range(0, round(.68*44100), 512):
        other.append(signal[pos:pos+512], pos+512)
    assert other.predict_continuation(.485*44100, .555*44100, end) == expected


@pytest.mark.parametrize("rate,midi", [(44100,33), (48000,33), (48000,38)])
def test_recovered_attack_owns_its_native_time_and_frozen_evidence(rate, midi):
    detector = tracing.TracedDetector(instrument=BASS_GUITAR, sample_rate=rate)
    tracing.stream(detector, pluck(rate,midi))
    report = next(row for row in detector.trace if row["kind"] == "report" and row["admitted"])
    owner = report["attack"]
    proof = owner["bass_continuation_evidence"]
    assert owner["report_outcome"] == "reported_continuation"
    assert proof["origin_sample"] == owner["origin"]["onset_sample"]
    assert proof["native_sample"] == owner["bass_continuation_sample"]
    assert proof["prediction"]["end_sample"] == proof["observed_sample"] <= report["hop"]*detector.hop_size
    assert len(owner["sparse_pitch_evidence"]["periodicity_votes"]) >= 3
    saved = deepcopy(owner)
    detector.reset()
    assert detector._bass_recovery is None
    assert detector._attack.bass_continuation_evidence is None
    assert saved == owner


def repeated_low_e():
    rate = 44100
    phase = 1.3073831164432708
    frequency = midi_to_freq(28)*2**(-12/1200)
    t = np.arange(round(.55*rate))/rate
    def tone(offset):
        audio = sum(a*np.sin(2*np.pi*frequency*h*t+offset*h)
                    for h,a in enumerate((1.,.4,.125,.06),1))
        return (audio*.15*np.exp(-3*t)*np.minimum(t/.009,1)*np.minimum((.55-t)/.005,1)).astype(np.float32)
    shift = round(.127*rate)
    signal = np.pad(tone(phase), (0,shift))
    following = tone(phase+.37)
    signal[shift:shift+len(following)] += following
    pad = 22985
    return np.r_[np.zeros(pad,np.float32),signal,np.zeros(round(.3*rate),np.float32)], rate, [pad/rate,(pad+shift)/rate]


def test_recovered_first_e_must_not_steal_the_real_second_e():
    signal, rate, times = repeated_low_e()
    found = strikes(PitchDetector(instrument=BASS_GUITAR,sample_rate=rate), signal)
    assert [midi for midi, _ in found] == [28,28]
    assert [onset for _, onset in found] == pytest.approx(times, abs=.05)


def test_first_tone_finishes_before_a_later_real_pluck_can_invalidate_it():
    rate = 44100
    shift = round(.179*rate)
    signal = np.pad(pluck(rate,45,-19), (0,shift))
    following = pluck(rate,50,-19)
    signal[shift:shift+len(following)] += following
    detector = tracing.TracedDetector(instrument=BASS_GUITAR,sample_rate=rate)
    tracing.stream(detector, signal)
    report = next(row for row in detector.trace if row["kind"] == "report" and row["admitted"])
    assert report["note"]["midi_note"] == 45
    assert report["hop"]*detector.hop_size/rate < .5+shift/rate


def test_missing_prediction_does_not_unlock_sparse_recovery():
    detector = PitchDetector(instrument=BASS_GUITAR)
    detector._waveform_history = None
    found = strikes(detector, pluck(44100,33))
    assert not any(midi == 33 and abs(onset-.5) <= .05 for midi,onset in found)


BEFORE = json.loads((Path(__file__).parent/'fixtures/bass_before_continuity.json').read_text(encoding='utf-8'))


@pytest.mark.parametrize("record", BEFORE, ids=lambda row: str(row["case"]))
def test_subsequent_bass_profile_changes_keep_old_measurements_separate(record):
    current = practice_advice.input_fingerprint(*measurement(record["case"]))
    old = record["key"]
    assert (old != current) is (record["case"][0] in {"bass_guitar", "bass_ukulele"})
    history = [attempt(old),attempt(old),attempt(current)]
    saved = deepcopy(history)
    if old != current:
        assert practice_advice.section_advice(history) is None
        assert practice_advice.comparable_attempts(history, passage(), input_key=current) == history[-1:]
        assert practice_advice.comparable_attempts(history, passage(), input_key=old) == history[:2]
    else:
        assert practice_advice.section_advice(history)["kind"] == "raise"
    assert history == saved
