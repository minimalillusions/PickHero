"""Previously false early attacks and real repetition counterexamples."""
import numpy as np
import pytest

from tests.test_detector_unmasking import strikes, unmasking_signal
from tests.test_detector_refinement import passage


@pytest.mark.parametrize("rate,phase", [
    (44100, 0.), (44100, np.pi/2),
    (48000, 0.), (48000, np.pi/2),
    (96000, 0.), (96000, np.pi/2),
])
def test_new_a_does_not_finance_an_extra_early_e(rate, phase):
    found = strikes(unmasking_signal(rate, phase), rate)
    assert [midi for _, midi in found] == [28, 33]


def test_quiet_true_e_before_a_survives_even_if_its_phase_reduces_total_energy():
    found = strikes(unmasking_signal(96000, repluck=(.8, np.pi)), 96000)
    for expected_time, expected_pitch in ((.5, 28), (.8, 28), (.9, 33)):
        assert any(midi == expected_pitch and abs(at - expected_time) <= .15 for at,midi in found)


def test_a_late_confirmation_filter_must_not_remove_existing_fast_repetitions():
    signal, rate, truth = passage(137, 25, False)
    found = strikes(signal, rate)
    # Independently corroborated repetitions recover attacks 3 and 6.
    # Attack 1 still lacks enough reliable pitch measurements.
    assert len(found) == 8
    for index in range(8):
        milliseconds, midi = truth[index]
        assert any(pitch == midi and abs(at - milliseconds/1000) <= .05 for at,pitch in found)
