"""Changed bass pitch or attack decisions are not changes in player progress."""
from copy import deepcopy
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from pickhero.config import Config
from pickhero.instruments import with_tuning

from pickhero import practice_advice


LEGACY = {
    "guitar": "9986db313e4bdf5e093ad40787a0f72452d2131d6c8305a7357bbd5dbffc5a76",
    "bass_ukulele": "830dcd94b282fa4292faf64412df3a8a8e190985acdbc24d4fac56979ae67d36",
    "bass_guitar": "63aa50cf055bb8ccb9fbd87f7ff1cc81d25aa034cf20734f927d04b4c8959347",
}


# Frozen before the bass correction. Later ukulele policy has its own transition tests.
LEGACY_CAPTURE_KEYS = {
    ('guitar', None): '9986db313e4bdf5e093ad40787a0f72452d2131d6c8305a7357bbd5dbffc5a76',
    ('guitar', 48000): 'fd01e9cce1724744c91390711f69f6de347cd7c6fc7848420d6d50064e2d13c0',
    ('guitar', 192000): '89bc67df9908830a8b7228c60abc2a7bbf5d57c984f06fa5b0fb696701766e56',
    ('bass_ukulele', None): '830dcd94b282fa4292faf64412df3a8a8e190985acdbc24d4fac56979ae67d36',
    ('bass_ukulele', 48000): 'da7ecda3edd6927a679314aad3a6ce12f30afa0aca57ec2ddc60614e75e40e5e',
    ('bass_ukulele', 192000): '61b7e88f54f38a527948f19be814791c6d3b5e27f31978b2ed6cd2a84c0620aa',
    ('bass_guitar', None): '63aa50cf055bb8ccb9fbd87f7ff1cc81d25aa034cf20734f927d04b4c8959347',
    ('bass_guitar', 48000): '85b4b6021f7998d414f1152f511384981c3249766d6d8f7513ecf998bf652641',
    ('bass_guitar', 192000): '0fe80e7ef2e2c09d6764cd7191206ae7b67dfb7e4506e1c3f3ebc3b28c69e62a',
}


@pytest.mark.parametrize("instrument", list(LEGACY))
@pytest.mark.parametrize("actual", [None, 48000, 192000])
def test_historical_policy_changes_at_all_negotiated_rates(instrument, actual):
    config = Config(instrument=instrument)
    before = deepcopy(asdict(config))
    capture = SimpleNamespace(sample_rate=actual, device_index=3) if actual else None
    profile = config.get_instrument()
    old = LEGACY_CAPTURE_KEYS[(instrument, actual)]
    new = practice_advice.input_fingerprint(config, profile, capture)
    if actual is None:
        assert old == LEGACY[instrument]
    assert (old != new) is (instrument in {"bass_guitar", "bass_ukulele"})
    assert asdict(config) == before


def test_retuned_bass_also_starts_new_measurement_generation():
    config = Config(instrument="bass_guitar")
    profile = config.get_instrument()
    retuned = with_tuning(profile, {string: note - 2 for string, note in profile.tuning.items()})
    assert retuned.key == "bass_guitar"
    assert '197fae89cd613c180af0be63d7c6d31ddfad061d811e1f3d22c944709858d806' != practice_advice.input_fingerprint(config, retuned)


def passage():
    return {"active_strings": [True] * 4, "max_fret": 12,
            "chord_partial_credit": False, "wait_mode": False,
            "loop_start_ms": 1000, "loop_end_ms": 5000, "tempo_factor": .5}


def attempt(key, accuracy=95):
    return {"start_ms": 1000, "end_ms": 5000, "tempo_factor": .5,
            "filter_key": practice_advice.filter_key_for_state(passage()),
            "accuracy": accuracy, "timing_percent": 95, "total": 8,
            "reliable": True, "uniform_tempo": True, "complete_loop": True,
            "timing_assessed": True, "uniform_wait": True, "wait_mode": False,
            "uniform_input": True, "input_key": key,
            "completed_at": "2026-10-04T10:00:00+00:00"}


@pytest.mark.parametrize("instrument", list(LEGACY))
def test_progress_keeps_old_history_but_compares_only_matching_policy(instrument):
    config = Config(instrument=instrument)
    current = practice_advice.input_fingerprint(config, config.get_instrument())
    old = LEGACY[instrument]
    history = [attempt(old, 40), attempt(old, 60), attempt(current, 80)]
    before = deepcopy(history)
    compared = practice_advice.comparable_attempts(history, passage(), input_key=current)
    assert [entry["accuracy"] for entry in compared] == (
        [80] if instrument in {"bass_guitar", "bass_ukulele"} else [60, 80])
    assert history == before
    if instrument in {"bass_guitar", "bass_ukulele"}:
        assert [entry["accuracy"] for entry in practice_advice.comparable_attempts(
            history, passage(), input_key=old)] == [40, 60]


@pytest.mark.parametrize("instrument", list(LEGACY))
def test_advice_needs_three_comparable_results_and_other_instruments_keep_history(instrument):
    config = Config(instrument=instrument)
    current = practice_advice.input_fingerprint(config, config.get_instrument())
    history = [attempt(LEGACY[instrument]), attempt(LEGACY[instrument])]
    for count in range(1, 4):
        history.append(attempt(current))
        advice = practice_advice.section_advice(history)
        if instrument in {"bass_guitar", "bass_ukulele"} and count < 3:
            assert advice is None
        else:
            assert advice["kind"] == "raise" and advice["target_tempo"] == .55
            assert advice["input_key"] == current


# Frozen against the immediately previous octave-correction release.
OCTAVE_POLICY_KEYS = {
    None: "de52b9d9895dc683d31656e76b132c38b294237ef21a501859a62cdca68b8cf3",
    48000: "8317b465924a83f7caa02e307076d0f0b4917390bdeb6174770c1d28e8467c68",
    192000: "3858ee7113997d8fffc15106d5d105498c996fc30e8b680b71915bd5b88dc8a1",
}


@pytest.mark.parametrize("actual", list(OCTAVE_POLICY_KEYS))
def test_changed_attack_decisions_do_not_count_as_a_jump_in_learning_progress(actual):
    config = Config(instrument="bass_guitar")
    capture = SimpleNamespace(sample_rate=actual, device_index=3) if actual else None
    old = OCTAVE_POLICY_KEYS[actual]
    new = practice_advice.input_fingerprint(config, config.get_instrument(), capture)
    assert new != old
    history = [attempt(old, 40), attempt(old, 60), attempt(new, 80)]
    unchanged = deepcopy(history)
    assert [entry["accuracy"] for entry in practice_advice.comparable_attempts(
        history, passage(), input_key=new)] == [80]
    assert [entry["accuracy"] for entry in practice_advice.comparable_attempts(
        history, passage(), input_key=old)] == [40, 60]
    assert practice_advice.section_advice([attempt(old), attempt(old), attempt(new)]) is None
    assert history == unchanged


def test_retuned_bass_separates_attack_decisions_from_octave_only_release():
    config = Config(instrument="bass_guitar")
    profile = config.get_instrument()
    retuned = with_tuning(profile, {string: note - 2 for string, note in profile.tuning.items()})
    assert practice_advice.input_fingerprint(config, retuned) != (
        "3b485984ddbd9f4d5af10d4bacf2d5e091ca564e62ec0be056c9221e5ffc96ed")



@pytest.mark.parametrize("accuracy,kind", [(95, "raise"), (40, "ease")])
def test_policy1_results_cannot_supply_the_first_two_policy2_advice_attempts(accuracy, kind):
    config = Config(instrument="bass_guitar")
    old = OCTAVE_POLICY_KEYS[None]
    new = practice_advice.input_fingerprint(config, config.get_instrument())
    history = [attempt(old, accuracy), attempt(old, accuracy)]
    for count in range(1, 4):
        history.append(attempt(new, accuracy))
        before = deepcopy(history)
        advice = practice_advice.section_advice(history)
        if count < 3:
            assert advice is None
        else:
            assert advice["kind"] == kind and advice["input_key"] == new
        assert history == before


def test_policy1_offer_does_not_suppress_a_new_policy2_offer():
    config = Config(instrument="bass_guitar")
    old = OCTAVE_POLICY_KEYS[None]
    new = practice_advice.input_fingerprint(config, config.get_instrument())
    old_offer = practice_advice.section_advice([attempt(old)] * 3)
    old_offer["at_result"] = 3
    history = [attempt(old)] * 2 + [attempt(new)] * 3
    before = deepcopy((history, old_offer))
    new_offer = practice_advice.section_advice(history, [old_offer], result_count=5)
    assert new_offer["kind"] == "raise" and new_offer["input_key"] == new
    assert (history, old_offer) == before
    new_offer["at_result"] = 5
    assert practice_advice.section_advice(history, [new_offer], result_count=5) is None
