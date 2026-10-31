"""Overlapping ukulele strings and measurement comparability.

The audio cases transform previously used recordings, not independent players.
"""
from copy import deepcopy
import json
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("aubio")

from pickhero import practice_advice
from pickhero.audio.note_utils import midi_to_freq
from tests.test_detector_note_energy import armed
from tests.test_detector_refinement import passage, strikes
from tests.test_ukulele_pitch_progress import attempt, measurement, passage as practice_state


@pytest.mark.parametrize("interval,cents,gain,wanted,tolerance", [
    (257, 23, 1., (0, 1, 3, 5, 6, 7), 150),
    (375, 25, .5, tuple(range(8)), 50),
])
def test_overlapping_strings_retain_the_last_quiet_a(interval, cents, gain, wanted, tolerance):
    signal, rate, truth = passage(interval, cents, True)
    found = strikes(signal * gain, rate)
    remaining = list(enumerate(truth))
    matched = []
    for onset, midi in found:
        candidates = [(index, note) for index, note in remaining
                      if midi == note[1] and abs(onset - note[0]) <= tolerance]
        assert candidates, "Unexpected extra or wrong-pitch report"
        best = min(candidates, key=lambda row: abs(onset - row[1][0]))
        remaining.remove(best)
        matched.append(best[0])
    assert matched == list(wanted)
    assert found[-1][1] == 33
    assert abs(found[-1][0] - truth[-1][0]) < 50


@pytest.mark.parametrize("harmonics,allowed", [((2, 5), True), ((3, 6), False)])
def test_a_needs_two_growing_partials_outside_ringing_e(harmonics, allowed):
    detector, native = armed()
    detector._last_report_note = 28
    detector._attack.votes = [33, 33]
    detector._attack.periodicity_votes = [33, 33]
    reference, current = [power for _, power in detector._partial_history]
    reference.fill(0.)
    current.fill(0.)
    for harmonic in harmonics:
        mask = abs(detector._partial_bins - midi_to_freq(33) * harmonic) <= detector.sample_rate / detector.buf_size / 2
        current[mask] = 1e-5 / np.count_nonzero(mask)
        reference[mask] = current[mask] / 100
    assert detector._corroborated_note_energy(33) is allowed
    assert detector._attack.origin.onset_sample == native
    assert bool(detector._attack.evidence) is allowed
    if allowed:
        assert detector._attack.evidence[0].bands == harmonics


BEFORE = json.loads((Path(__file__).parent / "fixtures/ukulele_before_string_changes.json").read_text())


@pytest.mark.parametrize("record", BEFORE, ids=lambda row: str(row["case"]))
def test_detector_improvement_does_not_count_as_player_progress(record):
    case, old_key = record["case"], record["key"]
    current = practice_advice.input_fingerprint(*measurement(case))
    # Both bass profiles now have subsequent detector generations.
    assert (current != old_key) is (case[0] in {"bass_ukulele", "bass_guitar"})
    if case[0] == "guitar":
        return
    history = [attempt(old_key, 95), attempt(old_key, 95), attempt(current, 95)]
    before = deepcopy(history)
    assert practice_advice.section_advice(history) is None
    assert practice_advice.comparable_attempts(history, practice_state(), input_key=current) == history[-1:]
    assert practice_advice.comparable_attempts(history, practice_state(), input_key=old_key) == history[:2]
    assert history == before
    history.extend([attempt(current, 95), attempt(current, 95)])
    advice = practice_advice.section_advice(history)
    assert advice["kind"] == "raise" and advice["input_key"] == current
