"""Counterexamples for isolated aubio evidence integration, without a device."""

from dataclasses import replace
from copy import deepcopy
import json
from pathlib import Path
import numpy as np
import pytest

pytest.importorskip("aubio")
from pickhero.audio.detector import PitchDetector
from pickhero.audio.attack import AttackOrigin
from pickhero.audio.detector import DetectedNote
from pickhero.audio.note_utils import midi_to_freq, midi_to_name
from pickhero.audio.quiet_repeats import ComplexPulse
from pickhero.instruments import BASS_UKULELE, BASS_GUITAR, GUITAR


def tied(midi=31, instrument=BASS_UKULELE):
    d = PitchDetector(instrument=instrument, sample_rate=48000)
    a = d._attack
    a.origin = AttackOrigin(7, 18, 8000.0, 0.0)
    a.pending = True
    a.age_hops = 15
    a.votes = [midi, midi + 1] * 4
    helper = d._quiet_repeat_corroborator
    if helper is not None:
        for hop in (25, 26):
            helper.observe(
                a,
                hop,
                midi_to_freq(midi),
                midi_to_freq(midi),
                0.95,
                4096,
                4,
                True,
                lambda f: True,
            )
    d._hops_seen = 27
    return d


@pytest.mark.parametrize("midi", [28, 29, 31, 32, 33, 38, 43])
@pytest.mark.parametrize("upper", [False, True])
def test_yin_evidence_can_select_either_side_of_a_tie(midi, upper):
    d = tied(midi)
    if upper:
        for index, row in enumerate(d._quiet_repeat_corroborator.agreements):
            d._quiet_repeat_corroborator.agreements[index] = replace(
                row,
                midi_note=midi + 1,
                primary_frequency=midi_to_freq(midi + 1),
                periodicity_frequency=midi_to_freq(midi + 1),
            )
    assert d._choose_pitch(d._attack.votes) == midi + int(upper)


@pytest.mark.parametrize(
    "bad",
    [
        "one",
        "weak",
        "stale",
        "owner",
        "gap",
        "conflicting",
        "unrelated",
        "majority",
        "nonadjacent",
        "missing",
        "boundary",
        "future",
        "closed",
    ],
)
def test_tie_does_not_borrow_or_invent_pitch_evidence(bad):
    d = tied()
    h = d._quiet_repeat_corroborator
    if bad == "one":
        h.agreements.pop()
    elif bad == "weak":
        h.agreements[0] = replace(h.agreements[0], periodicity_confidence=0.89)
    elif bad == "stale":
        d._hops_seen = 30
    elif bad == "owner":
        h.owner_sequence = 8
    elif bad == "gap":
        h.agreements[0] = replace(h.agreements[0], observed_hop=24)
    elif bad == "conflicting":
        h.agreements[0] = replace(
            h.agreements[0], periodicity_frequency=midi_to_freq(32)
        )
    elif bad == "unrelated":
        for index, row in enumerate(h.agreements):
            h.agreements[index] = replace(row, periodicity_frequency=midi_to_freq(38))
    elif bad == "majority":
        d._attack.votes.append(32)
    elif bad == "nonadjacent":
        d._attack.votes = [29, 32] * 4
    elif bad == "missing":
        d._quiet_repeat_corroborator = None
    elif bad == "boundary":
        h.agreements[0] = replace(h.agreements[0], midi_note=32)
    elif bad == "future":
        d._hops_seen = 25
    elif bad == "closed":
        d._attack.close("timeout", "accepted")
        d._attack.votes = [31, 32] * 4
    assert d._choose_pitch(d._attack.votes) == max(
        set(d._attack.votes), key=d._attack.votes.count
    )
    assert not d._attack.pitch_tie_evidence


@pytest.mark.parametrize("instrument", [BASS_GUITAR, GUITAR])
def test_other_instruments_retain_their_pitch_decision(instrument):
    d = tied(instrument=instrument)
    assert d._choose_pitch(d._attack.votes) == max(
        set(d._attack.votes), key=d._attack.votes.count
    )


def late_quiet():
    d = PitchDetector(instrument=BASS_UKULELE, sample_rate=48000, noise_gate_db=-26.0)
    native = 24000.0
    frequency = midi_to_freq(28)
    pulse = native + 1.5 * d.sample_rate / frequency
    end = int(pulse + 2 * d.sample_rate / frequency) + d.hop_size
    d._hops_seen = (end + d.hop_size - 1) // d.hop_size
    end = d._hops_seen * d.hop_size
    time = np.arange(end) / d.sample_rate
    x = (0.001 * np.sin(2 * np.pi * frequency * time)).astype(np.float32)
    mask = np.arange(end) >= round(pulse)
    x[mask] = 0.3 * np.sin(2 * np.pi * frequency * time[mask])
    d._waveform_history.append(x, end)
    a = d._attack
    a.origin = AttackOrigin(7, 44, native, 0.0)
    a.leading_quiet = True
    a.confirmed = True
    a.decision = "accepted"
    a.close_reason = "votes_complete"
    d._quiet_repeat_corroborator.pulses.append(ComplexPulse(pulse, end))
    note = DetectedNote(28, frequency, 0.95, midi_to_name(28), True, 10.0)
    d._report_candidate = (note, a)
    return d, note, pulse


def test_later_complex_confirmation_owns_energy_and_output_time():
    d, note, pulse = late_quiet()
    result = d._report(note)
    assert result is not None and result.midi_note == 28
    assert result.onset_age_hops == pytest.approx(
        note.onset_age_hops - (pulse - 24000) / d.hop_size
    )
    assert d._attack.origin.onset_sample == 24000
    proof = d._attack.quiet_start_evidence
    assert (
        proof.split_sample == round(pulse)
        and proof.end_sample <= d._hops_seen * d.hop_size
    )
    assert proof.observed_power >= proof.reference_power * 10 ** (14 / 10)
    assert d._quiet_repeat_corroborator.pulses[0].consumed


@pytest.mark.parametrize(
    "bad",
    ["none", "consumed", "far", "two", "no_history", "future", "quiet", "no_rise"],
)
def test_later_confirmation_cannot_bypass_energy_or_ownership(bad):
    d, note, pulse = late_quiet()
    h = d._quiet_repeat_corroborator
    if bad == "none":
        h.pulses.clear()
    elif bad == "consumed":
        h.pulses[0].consumed = True
    elif bad == "far":
        h.pulses[0].sample = 24000 + 3 * d.sample_rate / note.frequency
    elif bad == "two":
        h.pulses.append(ComplexPulse(pulse + 1, d._hops_seen * d.hop_size))
    elif bad == "no_history":
        d._waveform_history.reset()
    elif bad == "future":
        h.pulses[0].sample = d._waveform_history.end_sample
    elif bad == "quiet":
        d._waveform_history.data *= 0.01
    elif bad == "no_rise":
        d._waveform_history.data.fill(0.2)
    assert d._report(note) is None


def test_recovered_quiet_pluck_keeps_time_without_duplicate_on_decay():
    from tests.test_detector_refinement import passage

    raw, source_rate, targets = passage(293, -32, False)
    rate, pad = 192000, 8059
    x = (
        np.interp(
            np.arange(round(len(raw) * rate / source_rate)) * source_rate / rate,
            np.arange(len(raw)),
            raw,
        ).astype(np.float32)
        * 0.27
    )
    x = np.r_[np.zeros(pad, np.float32), x]
    truth = [at / 1000 + pad / rate for at, _ in targets]
    d = PitchDetector(instrument=BASS_UKULELE, sample_rate=rate)
    matched = set()
    for pos in range(0, len(x) - d.hop_size + 1, d.hop_size):
        note = d.process(x[pos : pos + d.hop_size])
        if note is None or not note.is_onset:
            continue
        at = (
            (pos + d.hop_size) / rate
            - note.onset_age_hops * d.hop_size / rate
            - d.onset_latency_ms / 1000
        )
        choices = [
            i
            for i, target in enumerate(truth)
            if i not in matched and abs(at - target) <= 0.05
        ]
        assert note.midi_note == 28 and choices, "Extra, mistimed or mispitched attack"
        matched.add(min(choices, key=lambda i: abs(at - truth[i])))
    assert {0, 3, 6} <= matched


def test_reset_discards_confirmation_but_keeps_existing_evidence_snapshot():
    d, note, _ = late_quiet()
    assert d._report(note) is not None
    evidence = d._attack.quiet_start_evidence
    d.reset()
    assert d._attack.quiet_start_evidence is None
    assert d._attack.late_quiet_start_sample is None
    assert not d._attack.pitch_tie_evidence
    assert not d._quiet_repeat_corroborator.pulses
    assert evidence.observed_power > evidence.reference_power


BEFORE = json.loads(
    (Path(__file__).parent / "fixtures/ukulele_before_aubio_evidence.json").read_text()
)


@pytest.mark.parametrize("record", BEFORE, ids=lambda row: str(row["case"]))
def test_only_ukulele_gets_a_new_measurement_generation(record):
    from pickhero import practice_advice
    from tests.test_ukulele_pitch_progress import measurement, attempt, passage

    current = practice_advice.input_fingerprint(*measurement(record["case"]))
    old = record["key"]
    changed = record["case"][0] == "bass_ukulele"
    assert (old != current) is changed
    history = [attempt(old), attempt(old), attempt(current)]
    saved = deepcopy(history)
    if changed:
        assert practice_advice.section_advice(history) is None
        assert (
            practice_advice.comparable_attempts(history, passage(), input_key=current)
            == history[-1:]
        )
        assert (
            practice_advice.comparable_attempts(history, passage(), input_key=old)
            == history[:2]
        )
    else:
        assert practice_advice.section_advice(history)["kind"] == "raise"
    assert history == saved
