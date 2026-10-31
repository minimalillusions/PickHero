"""Ukulele attack changes preserve history without inventing player progress."""
from copy import deepcopy
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from pickhero import practice_advice
from pickhero.config import Config
from pickhero.instruments import with_tuning


@pytest.mark.parametrize("case", [
    (key, semitones, rate) for key in ("guitar", "bass_guitar", "bass_ukulele")
    for semitones in (0, -2) for rate in (None, 48000, 192000)
])
@pytest.mark.parametrize("generation", ["retrigger", "unmasking"])
def test_historical_bass_policies_retain_old_history(case, generation):
    config, profile, capture = measurement(case)
    saved = deepcopy((asdict(config), asdict(profile), vars(capture) if capture else None))
    current = practice_advice.input_fingerprint(config, profile, capture)
    old = (BEFORE_RETRIGGER_KEYS if generation == "retrigger" else BEFORE_UNMASKING_KEYS)[case]
    assert (current != old) is (case[0] in {"bass_ukulele", "bass_guitar"})
    assert saved == (asdict(config), asdict(profile), vars(capture) if capture else None)
    if case[0] in {"bass_ukulele", "bass_guitar"}:
        history = [attempt(old, 40), attempt(old, 60), attempt(current, 95)]
        before = deepcopy(history)
        assert practice_advice.comparable_attempts(history, passage(), input_key=current) == history[-1:]
        assert practice_advice.comparable_attempts(history, passage(), input_key=old) == history[:2]
        assert practice_advice.section_advice(history) is None
        assert history == before


# Frozen before the ukulele phase guard. These include steel-bass policy 2.
BEFORE_UKULELE_GUARD = {('guitar', 0, None): '9986db313e4bdf5e093ad40787a0f72452d2131d6c8305a7357bbd5dbffc5a76', ('guitar', 0, 48000): 'fd01e9cce1724744c91390711f69f6de347cd7c6fc7848420d6d50064e2d13c0', ('guitar', 0, 192000): '89bc67df9908830a8b7228c60abc2a7bbf5d57c984f06fa5b0fb696701766e56', ('guitar', -2, None): 'b9f0909e55e18c58b352340a8169f429ca7287560625f440699c5dc6d42015aa', ('guitar', -2, 48000): '68e924d3b821fdd16f86dc517bb2292ac807e152ba967e14152613fa2a6d1d23', ('guitar', -2, 192000): '3e7f9e7c2b01c6060d1a8702c35d114ccbe50d0178d2145a293a713a00d5bb7e', ('bass_guitar', 0, None): 'ae8ab3eba0a5a62b56306db4ef556498591aafc209c1746d516b26579c8876e7', ('bass_guitar', 0, 48000): '601443b7dfa553f68e80d69980889d336f79acde762fe55f39bdaf9de43b1d12', ('bass_guitar', 0, 192000): '5e1e07b3b561b19c0fc90244167a0289874e8183d5343f1277907831df1a8e69', ('bass_guitar', -2, None): '7aa61d7d9245f512f18a8816cb96fda4ec4a527cae7c48a7f03e8d887ecbd241', ('bass_guitar', -2, 48000): 'c9077ef7a3e2782d0da07dfaf71075a73054d9cbce4dfd22c9f0361e77e0a78b', ('bass_guitar', -2, 192000): '970f5e0a55141cab212d19a0502ab2b40827dbb4c0566b4c3fb4a16182882770', ('bass_ukulele', 0, None): '830dcd94b282fa4292faf64412df3a8a8e190985acdbc24d4fac56979ae67d36', ('bass_ukulele', 0, 48000): 'da7ecda3edd6927a679314aad3a6ce12f30afa0aca57ec2ddc60614e75e40e5e', ('bass_ukulele', 0, 192000): '61b7e88f54f38a527948f19be814791c6d3b5e27f31978b2ed6cd2a84c0620aa', ('bass_ukulele', -2, None): '3b7ac492b2a32f104c0839ac008ff3be07f3f3ad39a7ab0e88cbdf2ead19e2f0', ('bass_ukulele', -2, 48000): 'af0ce15c4f29d3d97962f16fe7e97ce68571fcd4c373417c17810c15f6611715', ('bass_ukulele', -2, 192000): 'd56a5ebfee3def521f6a3f954e199370ac883cdd3b8ef9ed7575efb22cefee31'}
UKULELE_CASES = [case for case in BEFORE_UKULELE_GUARD if case[0] == "bass_ukulele"]


def measurement(case):
    key, semitones, actual = case
    config = Config(instrument=key)
    profile = config.get_instrument()
    if semitones:
        profile = with_tuning(profile, {
            string: pitch + semitones for string, pitch in profile.tuning.items()
        })
    capture = SimpleNamespace(sample_rate=actual, device_index=3) if actual else None
    return config, profile, capture


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


@pytest.mark.parametrize("case", list(BEFORE_UKULELE_GUARD))
def test_changed_bass_profiles_start_new_generations_at_each_rate_and_tuning(case):
    config, profile, capture = measurement(case)
    before = deepcopy((asdict(config), asdict(profile), vars(capture) if capture else None))
    current = practice_advice.input_fingerprint(config, profile, capture)
    assert (current != BEFORE_UKULELE_GUARD[case]) is (case[0] in {"bass_ukulele", "bass_guitar"})
    assert before == (asdict(config), asdict(profile), vars(capture) if capture else None)


@pytest.mark.parametrize("case", UKULELE_CASES)
def test_old_ukulele_attempts_stay_available_but_do_not_inflate_new_progress(case):
    current = practice_advice.input_fingerprint(*measurement(case))
    old = BEFORE_UKULELE_GUARD[case]
    history = [attempt(old, 40), attempt(old, 60), attempt(current, 80)]
    before = deepcopy(history)
    assert [entry["accuracy"] for entry in practice_advice.comparable_attempts(
        history, passage(), input_key=current)] == [80]
    assert [entry["accuracy"] for entry in practice_advice.comparable_attempts(
        history, passage(), input_key=old)] == [40, 60]
    assert history == before


@pytest.mark.parametrize("case", UKULELE_CASES)
@pytest.mark.parametrize("accuracy,kind,target", [(95, "raise", .55), (40, "ease", .45)])
def test_advice_requires_three_ukulele_results_from_the_new_generation(case, accuracy, kind, target):
    current = practice_advice.input_fingerprint(*measurement(case))
    history = [attempt(BEFORE_UKULELE_GUARD[case], accuracy) for _ in range(2)]
    for count in range(1, 4):
        history.append(attempt(current, accuracy))
        before = deepcopy(history)
        advice = practice_advice.section_advice(history)
        if count < 3:
            assert advice is None
        else:
            assert advice["kind"] == kind and advice["target_tempo"] == target
            assert advice["input_key"] == current
        assert history == before


@pytest.mark.parametrize("case", UKULELE_CASES)
def test_old_offer_does_not_suppress_new_ukulele_advice(case):
    current = practice_advice.input_fingerprint(*measurement(case))
    old = BEFORE_UKULELE_GUARD[case]
    old_offer = practice_advice.section_advice([attempt(old) for _ in range(3)])
    old_offer["at_result"] = 3
    history = [attempt(old) for _ in range(2)] + [attempt(current) for _ in range(3)]
    before = deepcopy((history, old_offer))
    new_offer = practice_advice.section_advice(history, [old_offer], result_count=5)
    assert new_offer["kind"] == "raise" and new_offer["input_key"] == current
    assert (history, old_offer) == before
    new_offer["at_result"] = 5
    assert practice_advice.section_advice(history, [new_offer], result_count=5) is None


# Frozen after the phase guard, before the additional overlap recognition.
PHASE_ONLY_KEYS = {('guitar', 0, None): '9986db313e4bdf5e093ad40787a0f72452d2131d6c8305a7357bbd5dbffc5a76',
 ('guitar', 0, 48000): 'fd01e9cce1724744c91390711f69f6de347cd7c6fc7848420d6d50064e2d13c0',
 ('guitar', 0, 192000): '89bc67df9908830a8b7228c60abc2a7bbf5d57c984f06fa5b0fb696701766e56',
 ('guitar', -2, None): 'b9f0909e55e18c58b352340a8169f429ca7287560625f440699c5dc6d42015aa',
 ('guitar', -2, 48000): '68e924d3b821fdd16f86dc517bb2292ac807e152ba967e14152613fa2a6d1d23',
 ('guitar', -2, 192000): '3e7f9e7c2b01c6060d1a8702c35d114ccbe50d0178d2145a293a713a00d5bb7e',
 ('bass_guitar', 0, None): 'ae8ab3eba0a5a62b56306db4ef556498591aafc209c1746d516b26579c8876e7',
 ('bass_guitar', 0, 48000): '601443b7dfa553f68e80d69980889d336f79acde762fe55f39bdaf9de43b1d12',
 ('bass_guitar', 0, 192000): '5e1e07b3b561b19c0fc90244167a0289874e8183d5343f1277907831df1a8e69',
 ('bass_guitar', -2, None): '7aa61d7d9245f512f18a8816cb96fda4ec4a527cae7c48a7f03e8d887ecbd241',
 ('bass_guitar', -2, 48000): 'c9077ef7a3e2782d0da07dfaf71075a73054d9cbce4dfd22c9f0361e77e0a78b',
 ('bass_guitar', -2, 192000): '970f5e0a55141cab212d19a0502ab2b40827dbb4c0566b4c3fb4a16182882770',
 ('bass_ukulele', 0, None): '25175b36b70ded5b9d7b62d09c6cfae136400a9455623dd4f8cd0a4df6d3c719',
 ('bass_ukulele', 0, 48000): 'e4acb7d1c14bd2e7db1a51c7ab233f414b962156e2040a632b7de6f48290a349',
 ('bass_ukulele', 0, 192000): 'ff63eb733d6039dc262f7fad838a60d1d04d7b75355b22c3e4c9055c19cb9dc0',
 ('bass_ukulele', -2, None): '21fc72d7f57a0144fb8e887147083e06fa7bfb4040583e4e77e8c0fef500ba29',
 ('bass_ukulele', -2, 48000): '8472f0b80e21e917a508eeba7d0cf58226d3c1ea5359c62ea5cc910d69b90a77',
 ('bass_ukulele', -2, 192000): '44a1f44380c4d064210ffe721f4077da8a19ee538b69efc29a25d07cdfc681cf'}


@pytest.mark.parametrize("case", list(PHASE_ONLY_KEYS))
def test_historical_overlap_measurements_differ_from_current_bass_generations(case):
    config, profile, capture = measurement(case)
    before = deepcopy((asdict(config), asdict(profile), vars(capture) if capture else None))
    current = practice_advice.input_fingerprint(config, profile, capture)
    assert (current != PHASE_ONLY_KEYS[case]) is (case[0] in {"bass_ukulele", "bass_guitar"})
    assert before == (asdict(config), asdict(profile), vars(capture) if capture else None)


@pytest.mark.parametrize("case", UKULELE_CASES)
@pytest.mark.parametrize("accuracy,kind,target", [(95, "raise", .55), (40, "ease", .45)])
def test_phase_only_results_cannot_supply_overlap_progress_or_advice(case, accuracy, kind, target):
    current = practice_advice.input_fingerprint(*measurement(case))
    old = PHASE_ONLY_KEYS[case]
    history = [attempt(old, accuracy) for _ in range(2)]
    for count in range(1, 4):
        history.append(attempt(current, accuracy))
        before = deepcopy(history)
        compared = practice_advice.comparable_attempts(history, passage(), input_key=current)
        assert compared == history[-min(count, 2):]
        assert practice_advice.comparable_attempts(history, passage(), input_key=old) == history[:2]
        advice = practice_advice.section_advice(history)
        if count < 3:
            assert advice is None
        else:
            assert advice["kind"] == kind and advice["target_tempo"] == target
            assert advice["input_key"] == current
        assert history == before


def test_phase_only_offer_does_not_suppress_overlap_generation_advice():
    case = ("bass_ukulele", 0, None)
    current = practice_advice.input_fingerprint(*measurement(case))
    old = PHASE_ONLY_KEYS[case]
    old_offer = practice_advice.section_advice([attempt(old) for _ in range(3)])
    old_offer["at_result"] = 3
    history = [attempt(old) for _ in range(2)] + [attempt(current) for _ in range(3)]
    before = deepcopy((history, old_offer))
    offered = practice_advice.section_advice(history, [old_offer], result_count=5)
    assert offered["kind"] == "raise" and offered["input_key"] == current
    assert (history, old_offer) == before
    offered["at_result"] = 5
    assert practice_advice.section_advice(history, [offered], result_count=5) is None


# Frozen before retention of an unfinished native uke attack.
BEFORE_RETRIGGER_KEYS = {('guitar', 0, None): '9986db313e4bdf5e093ad40787a0f72452d2131d6c8305a7357bbd5dbffc5a76', ('guitar', 0, 48000): 'fd01e9cce1724744c91390711f69f6de347cd7c6fc7848420d6d50064e2d13c0', ('guitar', 0, 192000): '89bc67df9908830a8b7228c60abc2a7bbf5d57c984f06fa5b0fb696701766e56', ('guitar', -2, None): 'b9f0909e55e18c58b352340a8169f429ca7287560625f440699c5dc6d42015aa', ('guitar', -2, 48000): '68e924d3b821fdd16f86dc517bb2292ac807e152ba967e14152613fa2a6d1d23', ('guitar', -2, 192000): '3e7f9e7c2b01c6060d1a8702c35d114ccbe50d0178d2145a293a713a00d5bb7e', ('bass_guitar', 0, None): 'ae8ab3eba0a5a62b56306db4ef556498591aafc209c1746d516b26579c8876e7', ('bass_guitar', 0, 48000): '601443b7dfa553f68e80d69980889d336f79acde762fe55f39bdaf9de43b1d12', ('bass_guitar', 0, 192000): '5e1e07b3b561b19c0fc90244167a0289874e8183d5343f1277907831df1a8e69', ('bass_guitar', -2, None): '7aa61d7d9245f512f18a8816cb96fda4ec4a527cae7c48a7f03e8d887ecbd241', ('bass_guitar', -2, 48000): 'c9077ef7a3e2782d0da07dfaf71075a73054d9cbce4dfd22c9f0361e77e0a78b', ('bass_guitar', -2, 192000): '970f5e0a55141cab212d19a0502ab2b40827dbb4c0566b4c3fb4a16182882770', ('bass_ukulele', 0, None): '36da4350a4b781be0970ed85441c70dd3166f389f2fdc0a78e33a91fb34b1bc5', ('bass_ukulele', 0, 48000): 'd87284c0fb19ca2d4f53deff6f3604411a59ee3067ca301224418af0da46bf5c', ('bass_ukulele', 0, 192000): '724517e850b4517873f9507c88e326e43a4afa8415a4a988757cd2afa6bb3ebf', ('bass_ukulele', -2, None): '1a9a9176e6d42102aa72a0784ec38a1c48640ee63704aa4d849a69aa7da8e2e5', ('bass_ukulele', -2, 48000): 'e19181bafa081a4dc43ddc86c9e08ef1c8e95bd75eb10c4c60ee10276d00e4ea', ('bass_ukulele', -2, 192000): '3c987fa2030d107f541ffdee1ab0aabdf803e8fd53fd262d46b1ae6495115044'}


# Frozen before the note-specific decay veto.
BEFORE_UNMASKING_KEYS = {('bass_guitar', -2, 48000): 'c9077ef7a3e2782d0da07dfaf71075a73054d9cbce4dfd22c9f0361e77e0a78b',
 ('bass_guitar', -2, 192000): '970f5e0a55141cab212d19a0502ab2b40827dbb4c0566b4c3fb4a16182882770',
 ('bass_guitar', -2, None): '7aa61d7d9245f512f18a8816cb96fda4ec4a527cae7c48a7f03e8d887ecbd241',
 ('bass_guitar', 0, None): 'ae8ab3eba0a5a62b56306db4ef556498591aafc209c1746d516b26579c8876e7',
 ('bass_guitar', 0, 48000): '601443b7dfa553f68e80d69980889d336f79acde762fe55f39bdaf9de43b1d12',
 ('bass_guitar', 0, 192000): '5e1e07b3b561b19c0fc90244167a0289874e8183d5343f1277907831df1a8e69',
 ('bass_ukulele', -2, None): 'e5a14a5ffb9a6640f3c8efe0f1ad57285a3704606c47413a5615d7493e1bb4f6',
 ('bass_ukulele', -2, 48000): 'e1d75d0dd89ddd51ba34be664b080fc40600f7148c918e422fe13cbed2a67d0b',
 ('bass_ukulele', -2, 192000): 'a0453da098c09abcafbad61a6b4518480689d2c3101188c659eebecf1eb392d6',
 ('bass_ukulele', 0, None): '42ac355bf96454f8badea84334d8be767562b6404a51dc7bc83059facae9130f',
 ('bass_ukulele', 0, 48000): '2a3c1ca31e3947671ee497998b1fa9b0e1d9a5a4022883e4bb2f4697afce5fa5',
 ('bass_ukulele', 0, 192000): 'd1b636e6b1c9d026d53766b50405d045a631c226dea12708b7bacf1bcd65b771',
 ('guitar', -2, 48000): '68e924d3b821fdd16f86dc517bb2292ac807e152ba967e14152613fa2a6d1d23',
 ('guitar', -2, 192000): '3e7f9e7c2b01c6060d1a8702c35d114ccbe50d0178d2145a293a713a00d5bb7e',
 ('guitar', -2, None): 'b9f0909e55e18c58b352340a8169f429ca7287560625f440699c5dc6d42015aa',
 ('guitar', 0, None): '9986db313e4bdf5e093ad40787a0f72452d2131d6c8305a7357bbd5dbffc5a76',
 ('guitar', 0, 48000): 'fd01e9cce1724744c91390711f69f6de347cd7c6fc7848420d6d50064e2d13c0',
 ('guitar', 0, 192000): '89bc67df9908830a8b7228c60abc2a7bbf5d57c984f06fa5b0fb696701766e56'}
