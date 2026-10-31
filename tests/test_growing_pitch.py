"""A covered string change must own its pitch, energy and original time."""
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
import json
import math
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from pickhero.audio.attack import AttackOrigin, PendingAttack
from pickhero.audio.detector import PitchDetector
from pickhero.audio.growing_pitch import GrowingPitchCorroborator, GrowingPitchFrame
from pickhero.audio.note_utils import midi_to_freq
from pickhero.audio.waveform_prediction import WaveformHistory
from pickhero.instruments import BASS_UKULELE
from pickhero import practice_advice
from tests.test_audio_trace import tracing
from tests.test_detector_refinement import passage, strikes
from tests.test_ukulele_pitch_progress import attempt, measurement, passage as practice_state


def target():
    signal, rate, truth = passage(137, 19, True)
    return np.r_[np.zeros(644, np.float32), signal*.8], rate, truth


def test_fast_string_change_recovers_two_notes_without_stealing_the_existing_a():
    signal, rate, truth = target()
    found = strikes(signal, rate)
    assert [midi for at, midi in found] == [28, 33, 33, 28]
    for actual, index in zip(found, (0, 1, 3, 6), strict=True):
        assert abs(actual[0] - (truth[index][0]+644/rate*1000)) < 50


def test_duplicate_output_still_advances_the_reference_for_the_next_string():
    signal, rate, truth = passage(250, 0, True)
    found = strikes(signal, rate)
    assert [midi for at, midi in found] == [28, 33, 33, 33, 28, 33]
    assert abs(found[-2][0]-truth[6][0]) < 50
    assert abs(found[-1][0]-truth[7][0]) < 50


def test_repeated_e_does_not_turn_into_a_new_higher_pitch():
    signal, rate, _ = passage(107, 23, False)
    found = strikes(np.r_[np.zeros(284, np.float32), signal*1.7], rate)
    assert found and all(midi == 28 for at, midi in found)


def test_growing_pitch_evidence_keeps_its_original_time_and_survives_reset():
    signal, rate, _ = target()
    detector = tracing.TracedDetector(instrument=BASS_UKULELE, sample_rate=rate)
    tracing.stream(detector, signal)
    reports = [row for row in detector.trace if row['kind'] == 'report'
               and row['admitted'] and row['attack']['growing_pitch_evidence'] is not None]
    assert len(reports) == 2
    for row in reports:
        owner = row['attack']
        proof = owner['growing_pitch_evidence']
        assert owner['report_outcome'] == 'reported_growing_pitch'
        assert proof['native_sample'] == owner['origin']['onset_sample']
        assert proof['short_start_sample'] < proof['short_split_sample'] < proof['short_end_sample']
        assert abs(proof['short_split_sample']-proof['native_sample']) <= .5
        assert proof['short_end_sample'] <= row['hop']*detector.hop_size
        assert sum(growth >= 6 for harmonic, growth in proof['short_growth']) >= 2
        for frame in proof['frames']:
            assert frame['reference_end_sample'] <= proof['native_sample']
            assert frame['end_sample']-detector.buf_size >= proof['native_sample']
            assert frame['end_sample'] <= row['hop']*detector.hop_size
        saved = deepcopy(owner)
        detector.reset()
        assert owner == saved
        assert not detector._growing_pitch.frames and detector._growing_pitch.reported is None


def armed(offset=0.):
    rate, hop, native = 44100, 512, 11025
    helper = GrowingPitchCorroborator(rate, hop, 4096, 38, 260)
    attack = PendingAttack(AttackOrigin(7, 24, native, 1.), pending=True, confirmed=False)
    helper.owner = 7
    for observed in (34, 35):
        helper.frames.append(GrowingPitchFrame(observed, native-1, observed*hop,
            55., 33, 3., 1., (1, 2, 3, 5)))
    time = np.arange(round(.5*rate))/rate
    audio = .001*np.sin(2*np.pi*midi_to_freq(28)*time)
    relative = time-native/rate-offset
    envelope = np.clip(relative/.005, 0, 1)
    audio += envelope*sum(.05/h*np.sin(2*np.pi*55*h*relative) for h in (1, 2, 3, 5))
    history = WaveformHistory(rate, hop)
    for start in range(0, len(audio), hop):
        block = audio[start:start+hop].astype(np.float32)
        history.append(block, start+len(block))
    pulse = SimpleNamespace(sample=float(native), observed_sample=native+hop, consumed=False)
    complex_onsets = SimpleNamespace(_near=lambda *args: [pulse])
    return helper, attack, complex_onsets, history


def test_two_short_windows_distinguish_owned_energy_from_a_later_note():
    helper, attack, pulses, history = armed()
    proof = helper.corroborate(attack, 28, pulses, history, lambda f: True)
    assert proof is not None and proof.midi_note == 33
    assert proof.confidence == pytest.approx(1-math.exp(-1))
    assert proof.native_sample == attack.origin.onset_sample
    late = armed(.07)[-1]
    assert helper.corroborate(attack, 28, pulses, late, lambda f: True) is None
    with pytest.raises(FrozenInstanceError):
        proof.native_sample = 0
    assert attack.pending and attack.decision is None
    assert not pulses._near()[0].consumed


@pytest.mark.parametrize('bad', ['owner', 'closed', 'not_pending', 'no_previous', 'same_pitch',
    'one_frame', 'nonadjacent', 'different_pitch', 'few_partials', 'even_partials',
    'weak_score', 'weak_margin', 'unstable', 'boundary', 'high_partials',
    'missing_complex', 'missing_audio', 'missing_fundamental'])
def test_incomplete_or_ambiguous_evidence_cannot_recover_a_note(bad):
    helper, attack, pulses, history = armed()
    last, fundamental = 28, lambda f: True
    first, second = helper.frames
    if bad == 'owner': helper.owner = 8
    elif bad == 'closed': attack.close('timeout', 'insufficient_votes')
    elif bad == 'not_pending': attack.pending = False
    elif bad == 'no_previous': last = -1
    elif bad == 'same_pitch': last = 33
    elif bad == 'one_frame': helper.frames.pop()
    elif bad == 'nonadjacent': helper.frames[-1] = replace(second, observed_hop=37)
    elif bad == 'different_pitch': helper.frames[-1] = replace(second, midi_note=34)
    elif bad == 'few_partials': helper.frames[-1] = replace(second, harmonics=(1, 2, 3))
    elif bad == 'even_partials': helper.frames[-1] = replace(second, harmonics=(2, 4, 6, 8))
    elif bad == 'weak_score': helper.frames[-1] = replace(second, score=1.)
    elif bad == 'weak_margin': helper.frames[-1] = replace(second, margin=.1)
    elif bad == 'unstable': helper.frames[-1] = replace(second, frequency=57.)
    elif bad == 'boundary':
        helper.frames[0] = replace(first, frequency=55*2**(45/1200))
        helper.frames[1] = replace(second, frequency=55*2**(45/1200))
    elif bad == 'high_partials':
        helper.frames[0] = replace(first, harmonics=(3, 4, 5, 6))
        helper.frames[1] = replace(second, harmonics=(3, 4, 5, 6))
    elif bad == 'missing_complex': pulses = None
    elif bad == 'missing_audio': history.reset()
    else: fundamental = lambda f: False
    assert helper.corroborate(attack, last, pulses, history, fundamental) is None


def test_silence_and_reset_leave_no_growing_pitch_history():
    detector = PitchDetector(instrument=BASS_UKULELE)
    for _ in range(100):
        assert detector.process(np.zeros(detector.hop_size, np.float32)) is None
    assert not detector._growing_pitch.frames
    signal, _, _ = target()
    for start in range(0, len(signal)-detector.hop_size+1, detector.hop_size):
        detector.process(signal[start:start+detector.hop_size])
    assert len(detector._growing_pitch.frames) <= 32
    assert detector._growing_pitch.reported is not None
    detector.process(np.full(detector.hop_size, np.nan, np.float32))
    assert not detector._growing_pitch.frames and detector._growing_pitch.reported is None


BEFORE = json.loads((Path(__file__).parent/'fixtures/ukulele_before_growing_pitch.json').read_text(encoding='utf-8'))


@pytest.mark.parametrize('record', BEFORE, ids=lambda row: str(row['case']))
def test_only_the_new_ukulele_measurement_generation_changes(record):
    old = record['key']
    current = practice_advice.input_fingerprint(*measurement(record['case']))
    assert (old != current) is (record['case'][0] == 'bass_ukulele')
    history = [attempt(old), attempt(old), attempt(current)]
    saved = deepcopy(history)
    if old != current:
        assert practice_advice.section_advice(history) is None
        assert practice_advice.comparable_attempts(history, practice_state(), input_key=current) == history[-1:]
    else:
        assert practice_advice.section_advice(history)['kind'] == 'raise'
    assert history == saved
