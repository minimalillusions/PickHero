"""Bass pitch correction cannot create attacks or change their timing.

Controlled readings exercise causal state boundaries; real-audio acceptance
is recorded separately and is not implied by these unit tests.
"""
from types import SimpleNamespace

import numpy as np
import pytest

from pickhero import instruments
from pickhero.audio.note_utils import midi_to_freq, midi_to_name

pytest.importorskip("aubio")
from pickhero.audio import detector as detector_module


class Check:
    def __init__(self, midi=28, confidence=.98, buf_size=4096):
        self.midi, self.confidence, self.buf_size = midi, confidence, buf_size
        self.calls = 0

    def __call__(self, block):
        self.calls += 1
        return [midi_to_freq(self.midi)]

    def get_confidence(self):
        return self.confidence


def detector(instrument=instruments.BASS_GUITAR):
    result = detector_module.PitchDetector(instrument=instrument)
    result._fundamental_present = lambda frequency: True
    result._pitch = SimpleNamespace(process=lambda block: (midi_to_freq(40), .7))
    result._pitch_check = Check()
    result._onset = lambda block: False
    result.last_signal_db = -20
    result.last_confidence = .7
    result._onset_pending = True
    result._onset_age = 9
    result._onset_backdate_hops = 1.25
    result._hops_seen = 30
    result._strike_confirmed = True
    return result


def pending(result, harmonic=40, correction=28):
    result._onset_votes = [harmonic, harmonic]
    result._bass_pitch_votes = [(correction, .98), (correction, .97)]


def test_normal_report_changes_only_pitch_and_keeps_fractional_time():
    result = detector()
    result._onset_votes = [40] * 7
    result._bass_pitch_votes = [(28, .98)]
    note = result.process(np.full(result.hop_size, .1, np.float32))
    assert note.is_onset and note.midi_note == 28 and note.original_midi_note == 40
    assert note.frequency == midi_to_freq(28) and note.name == midi_to_name(28)
    assert note.onset_age_hops == 11.25
    assert result._last_report_hop == 19.75 and result._last_report_note == 40


def test_flushed_old_strike_keeps_snapshot_when_new_strike_resets_state():
    result = detector()
    pending(result)
    result._periodicity_votes = [40, 40]
    # This new onset has no old-window evidence of its own and reports a
    # different secondary pitch. It must not rename the flushed old strike.
    result._pitch_check = Check(33)

    class NewOnset:
        def __call__(self, block):
            return True

        def get_last(self):
            return result._hops_seen * result.hop_size

    result._onset = NewOnset()
    changed = result.process(np.full(result.hop_size, .1, np.float32))
    assert changed.is_onset and changed.midi_note == 28
    assert changed.original_midi_note == 40 and changed.onset_age_hops == 11.25
    assert result._onset_pending and result._onset_age == 0
    assert result._bass_pitch_votes == result._periodicity_votes == []
    assert result._pitch_check.calls == 1


@pytest.mark.parametrize("reason", ["silence", "timeout"])
def test_flush_on_end_or_timeout_preserves_old_pitch_and_time(reason):
    result = detector()
    pending(result)
    if reason == "timeout":
        result._onset_age = result.onset_hold_hops
    age = result._onset_age + result._onset_backdate_hops + 1
    level = 0 if reason == "silence" else .1
    note = result.process(np.full(result.hop_size, level, np.float32))
    assert note.is_onset and note.midi_note == 28 and note.onset_age_hops == age
    assert not result._onset_pending
    assert result._pitch_check.calls == 1
    assert result._flush_votes() is None


@pytest.mark.parametrize("missing", ["primary", "confirmation", "strong_confirmation"])
def test_yin_cannot_create_or_self_confirm_an_attack(missing):
    result = detector()
    pending(result)
    if missing == "primary":
        result._onset_votes = []
    else:
        result._strike_confirmed = False
        result._transient_candidate = True
        result._periodicity_votes = [40, 40] if missing == "strong_confirmation" else []
    note = result._flush_votes()
    if missing == "strong_confirmation":
        assert note.is_onset and note.midi_note == 40
        assert note.original_midi_note is None
    else:
        assert note is None


def test_dedup_uses_original_pitch_for_both_previous_and_next_strike():
    result = detector()
    admitted = []
    # Renaming a note must neither release a previously suppressed repeat nor
    # suppress a different original note. IDs refer to original strike times.
    for event_id, (hop, original, corrected) in enumerate([
        (30, 40, 28), (33, 40, 40), (34, 52, 40), (35, 52, 52),
        (36, 40, 40), (60, 40, 28),
    ]):
        result._hops_seen = hop
        note = detector_module.DetectedNote(corrected, midi_to_freq(corrected), .97,
                                     midi_to_name(corrected), True, .25)
        if corrected != original:
            note.original_midi_note = original
        if result._report(note) is not None:
            admitted.append(event_id)
            assert result._last_report_note == original
            assert result._last_report_hop == hop - .25
    assert admitted == [0, 2, 4, 5]


@pytest.mark.parametrize("rate,main_window,yin_window", [
    (44100, 4096, 4096), (48000, 4480, 4096),
    (96000, 8896, 8192), (192000, 17856, 16384),
])
def test_collection_uses_real_fft_window_and_keeps_existing_rate_policy(rate, main_window, yin_window):
    result = detector_module.PitchDetector(sample_rate=rate, instrument=instruments.BASS_GUITAR)
    assert result.buf_size == main_window
    assert result._pitch_check.buf_size == yin_window
    result._onset_pending = True
    result.last_signal_db = -20
    # Isolate exactly one sample either side of the native FFT-window edge.
    for sample_count, expected in [(yin_window - 1, 0), (yin_window, 1)]:
        result._onset_age = 0
        result._onset_backdate_hops = ((sample_count - result.onset_latency_ms * rate / 1000)
                                       / result.hop_size)
        result._collect_bass_pitch(midi_to_freq(28), .98)
        assert len(result._bass_pitch_votes) == expected


@pytest.mark.parametrize("frequency,confidence", [
    (float("nan"), .98), (float("inf"), .98), (0, .98), (-1, .98),
    (midi_to_freq(28), float("nan")), (midi_to_freq(28), float("inf")),
    (midi_to_freq(28), .899), (midi_to_freq(0), .98), (midi_to_freq(127), .98),
])
def test_invalid_secondary_readings_are_never_votes(frequency, confidence):
    result = detector()
    result._collect_bass_pitch(frequency, confidence)
    assert result._bass_pitch_votes == []
    assert result._periodicity_votes == result._onset_votes == []


@pytest.mark.parametrize("condition", ["not_pending", "quiet", "missing_window", "no_check"])
def test_no_collector_evidence_from_inactive_or_unusable_source(condition):
    result = detector()
    if condition == "not_pending":
        result._onset_pending = False
    elif condition == "quiet":
        result.last_signal_db = result.noise_gate_db - 1
    elif condition == "missing_window":
        result._pitch_check = SimpleNamespace()
    else:
        result._pitch_check = None
    result._collect_bass_pitch(midi_to_freq(28), .98)
    assert result._bass_pitch_votes == []


@pytest.mark.parametrize("votes", [[(28, .98)], [(28, .98), (29, .98)],
                                    [(33, .98), (33, .98)]])
def test_weak_disagreeing_or_non_octave_votes_do_not_rename(votes):
    result = detector()
    pending(result)
    result._bass_pitch_votes = votes
    assert result._flush_votes().midi_note == 40


def test_missing_fundamental_keeps_original_pitch():
    result = detector()
    pending(result)
    result._fundamental_present = lambda frequency: False
    assert result._flush_votes().midi_note == 40


@pytest.mark.parametrize("instrument", [instruments.GUITAR, instruments.BASS_UKULELE])
def test_other_instruments_cannot_collect_or_apply_correction(instrument):
    result = detector(instrument=instrument)
    result._collect_bass_pitch(midi_to_freq(28), .98)
    assert result._bass_pitch_votes == []
    pending(result)
    assert result._flush_votes().midi_note == 40


def test_reset_removes_votes_and_rebuilds_native_history():
    result = detector()
    pending(result)
    previous_check = result._pitch_check
    result.reset()
    assert result._bass_pitch_votes == result._periodicity_votes == result._onset_votes == []
    assert not result._onset_pending
    assert result._pitch_check is not previous_check
    assert result._flush_votes() is None
