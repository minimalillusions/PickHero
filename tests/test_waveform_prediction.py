"""Causal continuation evidence must preserve real phase-changing re-plucks."""
from dataclasses import replace

import numpy as np
import pytest

from pickhero.audio.waveform_prediction import WaveformHistory
from tests.test_detector_unmasking import unmasking_signal


def captured(rate, phase=0., repluck=None):
    signal = unmasking_signal(rate, phase, repluck)
    history = WaveformHistory(rate, 512)
    end = round(.96 * rate)
    for offset in range(0, end, 512):
        block = signal[offset:min(end, offset + 512)]
        history.append(block, offset + len(block))
    return history, signal


@pytest.mark.parametrize("rate", [44100, 48000, 96000, 192000])
@pytest.mark.parametrize("phase", [0., np.pi / 2])
def test_continuing_tail_is_predictable_before_the_next_physical_pluck(rate, phase):
    history, signal = captured(rate, phase)
    prediction = history.predict(.79 * rate, .88 * rate)
    assert prediction is not None and prediction.is_continuation
    assert prediction.train_end_sample < .79 * rate < prediction.end_sample <= .88 * rate
    assert history.data.nbytes < 400000


@pytest.mark.parametrize("rate", [44100, 48000, 96000, 192000])
@pytest.mark.parametrize("phase", [0., np.pi / 2, np.pi, 3 * np.pi / 2])
def test_true_quiet_repluck_changes_the_waveform_even_when_total_energy_falls(rate, phase):
    history, signal = captured(rate, repluck=(.8, phase))
    prediction = history.predict(.79 * rate, .88 * rate)
    assert prediction is not None and not prediction.is_continuation


def test_analysis_does_not_use_samples_after_its_declared_end():
    first, signal = captured(44100)
    before = first.predict(.79 * 44100, .88 * 44100)
    signal[round(.88 * 44100):] = np.random.default_rng(42).normal(0, .9, len(signal) - round(.88 * 44100))
    second = WaveformHistory(44100, 512)
    for offset in range(0, round(.96 * 44100), 512):
        block = signal[offset:offset + 512]
        second.append(block, offset + len(block))
    assert second.predict(.79 * 44100, .88 * 44100) == before


def test_forecast_does_not_refit_to_a_new_attack_inside_the_evaluated_interval():
    a, _ = captured(44100)
    b, _ = captured(44100, repluck=(.8, np.pi))
    old = a.predict(.79 * 44100, .88 * 44100)
    new = b.predict(.79 * 44100, .88 * 44100)
    assert old.fit_error == new.fit_error
    assert old.pole_radius == new.pole_radius and old.rank == new.rank
    assert old.is_continuation and not new.is_continuation


@pytest.mark.parametrize("kind", ["reset", "nan", "infinity", "gap", "future", "past"])
def test_missing_or_broken_history_cannot_certify_a_continuation(kind):
    history, _ = captured(44100)
    if kind == "reset": history.reset()
    elif kind in ("nan", "infinity"):
        history.append(np.full(512, float("nan") if kind == "nan" else float("inf"), np.float32), history.end_sample + 512)
    elif kind == "gap": history.append(np.zeros(512, np.float32), history.end_sample + 1024)
    start, end = (.79 * 44100, .88 * 44100)
    if kind == "future": end = history.end_sample + 1
    elif kind == "past": start = 100
    assert history.predict(start, end) is None


def test_ring_wraps_without_changing_samples_or_growing():
    history = WaveformHistory(44100, 512)
    data = np.random.default_rng(61).normal(size=100000).astype(np.float32)
    original = data.copy()
    allocation = history.data
    for offset in range(0, len(data), 733):
        block = data[offset:offset + 733]
        history.append(block, offset + len(block))
    np.testing.assert_array_equal(history._read(90000, 97000), data[90000:97000])
    np.testing.assert_array_equal(data, original)
    assert history.data is allocation and history.filled == len(history.data)
    assert history._read(0, 10) is None


@pytest.mark.parametrize("field,value", [
    ("fit_error", .001), ("prediction_error", .1), ("peak_error", .1),
    ("pole_radius", 1.02), ("reference_power", 0.), ("observed_power", 0.),
])
def test_poor_prediction_unstable_model_and_inaudible_data_do_not_veto(field, value):
    history, _ = captured(44100)
    prediction = history.predict(.79 * 44100, .88 * 44100)
    assert prediction.is_continuation
    assert not replace(prediction, **{field: value}).is_continuation


def test_rejected_candidate_cannot_consume_the_successors_repeat_allowance():
    from pickhero.audio.attack import AttackOrigin
    from tests.test_onset_timing_confidence import armed

    history, _ = captured(44100)
    detector, owner = armed()
    detector.last_confidence = .99
    note = detector._flush_votes()
    owner.waveform_prediction = history.predict(.79 * 44100, .88 * 44100)
    before = detector._last_report_hop, detector._last_report_note
    detector._attack = type(owner)(AttackOrigin(4, 90, 80.5*detector.hop_size, .25))
    assert detector._report(note) is None
    assert owner.report_outcome == "prediction_filtered"
    assert (detector._last_report_hop, detector._last_report_note) == before
    assert detector._report_candidate is None
    assert detector._attack.waveform_prediction is None
    assert detector._attack.report_outcome is None


def test_forecast_of_another_pitch_keeps_the_original_uncertain_report():
    from tests.test_onset_timing_confidence import armed

    history, _ = captured(44100)
    detector, owner = armed()
    detector._last_report_note = 33
    owner.votes = [33] * 3
    note = detector._flush_votes()
    assert note.timing_uncertain
    owner.waveform_prediction = history.predict(.79 * 44100, .88 * 44100)
    assert owner.waveform_prediction.is_continuation
    assert not owner.waveform_prediction.supports_pitch(note.frequency)
    assert detector._report(note) is note
    assert owner.report_outcome == "reported"


@pytest.mark.parametrize("rate", [44100, 48000, 96000, 192000])
def test_fitted_components_match_the_actual_waveform_not_an_unrelated_pitch(rate):
    from pickhero.audio.note_utils import midi_to_freq

    history, _ = captured(rate)
    prediction = history.predict(.79 * rate, .88 * rate)
    assert prediction.supports_pitch(midi_to_freq(28))
    assert not prediction.supports_pitch(midi_to_freq(33))
    assert not prediction.supports_pitch(float("nan"))
    assert not prediction.supports_pitch(0)


@pytest.mark.parametrize("invalid", [None, float("nan"), float("inf")])
def test_detector_reset_clears_waveform_and_candidate_evidence(invalid):
    from pickhero.audio.detector import PitchDetector
    from pickhero.instruments import BASS_UKULELE

    detector = PitchDetector(instrument=BASS_UKULELE)
    detector.process(np.zeros(detector.hop_size, np.float32))
    assert detector._waveform_history.filled == detector.hop_size
    if invalid is None:
        detector.reset()
    else:
        detector.process(np.full(detector.hop_size, invalid, np.float32))
    assert detector._waveform_history.filled == detector._waveform_history.end_sample == 0
    assert detector._attack.waveform_prediction is None
