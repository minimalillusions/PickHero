"""A correct pitch with ambiguous onset ownership is not a timing verdict."""
from dataclasses import replace
from types import SimpleNamespace

import numpy as np
import pytest

pytest.importorskip("aubio")

from pickhero.audio.attack import AttackEvidence, AttackOrigin
from pickhero.audio.detector import DetectedNote, PitchDetector
from pickhero.audio.input import TimestampedNote
from pickhero.audio.note_utils import midi_to_freq
from pickhero.instruments import BASS_GUITAR, BASS_UKULELE, GUITAR
from pickhero.matcher import MatchType, NoteMatcher
from pickhero.tabs.timeline import NoteEvent, SongMetadata, Timeline
from pickhero.ui.colors import get_look, set_look
from pickhero.ui.feedback import FeedbackRenderer
from tests.test_audio_trace import tracing
from tests.test_detector_unmasking import unmasking_signal


def armed(profile=BASS_UKULELE):
    detector = PitchDetector(instrument=profile)
    detector._hops_seen = 90
    detector._last_report_note, detector._last_report_hop = 28, 10
    owner = detector._attack
    owner.origin = AttackOrigin(3, 74, 70.25*detector.hop_size, .75)
    owner.pending, owner.confirmed = True, True
    owner.age_hops, owner.votes = 8, [28]*3
    owner.evidence = [AttackEvidence("level", 85, 60, 73, (-44.,), (-18.,))]
    owner.requested_close_reason = "replaced"
    detector._onset = SimpleNamespace(get_last=lambda:80.5*detector.hop_size)
    return detector, owner


def test_annotation_keeps_the_pitch_native_time_and_owning_candidate():
    detector, owner = armed()
    origin, evidence = owner.origin, list(owner.evidence)
    note = detector._flush_votes()
    assert note.midi_note == 28 and note.is_onset and note.timing_uncertain
    assert note.onset_age_hops == 8.75
    assert owner.origin is origin and owner.evidence == evidence
    ambiguity = owner.timing_ambiguity
    assert ambiguity.successor_sample == 80.5*detector.hop_size
    assert ambiguity.confirmation_start_sample == 84*detector.hop_size
    assert ambiguity.observed_hop == 90
    detector._attack = type(owner)(AttackOrigin(4, 90, 80.5*detector.hop_size, .25))
    assert detector._report(note) is note
    assert owner.report_outcome == "reported" and detector._attack.timing_ambiguity is None
    assert detector._flush_votes() is None


@pytest.mark.parametrize("condition", [
    "earlier_level", "overlapping_level", "earlier_band_window", "additional_earlier_evidence",
    "different_pitch", "missing_native", "missing_evidence", "no_timestamp_api",
    "future_successor", "invalid_successor", "successor_before_opening", "partial_confirmation",
])
def test_annotation_requires_the_specific_late_owned_confirmation(condition):
    detector, owner = armed()
    if condition == "earlier_level": owner.evidence[0] = replace(owner.evidence[0], observed_hop=79)
    elif condition == "overlapping_level": owner.evidence[0] = replace(owner.evidence[0], observed_hop=81)
    elif condition == "earlier_band_window": owner.evidence[0] = replace(owner.evidence[0], kind="bands", observed_hop=84)
    elif condition == "additional_earlier_evidence": owner.evidence.append(replace(owner.evidence[0], observed_hop=75))
    elif condition == "different_pitch": detector._last_report_note = 33
    elif condition == "missing_native": owner.origin = replace(owner.origin, onset_sample=None)
    elif condition == "missing_evidence": owner.evidence.clear()
    elif condition == "no_timestamp_api": detector._onset = None
    elif condition == "future_successor": detector._onset.get_last = lambda:91*detector.hop_size
    elif condition == "invalid_successor": detector._onset.get_last = lambda:float("nan")
    elif condition == "successor_before_opening": detector._onset.get_last = lambda:73*detector.hop_size
    elif condition == "partial_confirmation": owner.evidence[0] = replace(owner.evidence[0], kind="partials")
    note = detector._flush_votes()
    assert note is not None and not note.timing_uncertain and owner.timing_ambiguity is None


@pytest.mark.parametrize("profile", [GUITAR, BASS_GUITAR], ids=lambda p:p.key)
def test_other_instruments_keep_their_existing_timing(profile):
    detector, owner = armed(profile)
    assert not detector._flush_votes().timing_uncertain
    assert owner.timing_ambiguity is None


@pytest.mark.parametrize("invalid", [None, float("nan"), float("inf")])
def test_reset_discards_annotation_state(invalid):
    detector, owner = armed()
    note = detector._flush_votes()
    assert note.timing_uncertain
    if invalid is None: detector.reset()
    else: detector.process(np.full(detector.hop_size, invalid, np.float32))
    assert detector._attack.timing_ambiguity is None and detector._report_candidate is None
    assert owner.timing_ambiguity is not None  # previous diagnostic snapshot remains owned


@pytest.mark.parametrize("rate,phase", [(44100,np.pi/2),(48000,np.pi/2),(96000,0.)])
def test_missing_forecast_retains_uncertainty_instead_of_discarding_the_pitch(rate, phase):
    signal = unmasking_signal(rate, phase)
    plain = PitchDetector(instrument=BASS_UKULELE, sample_rate=rate)
    traced = tracing.TracedDetector(instrument=BASS_UKULELE, sample_rate=rate)
    plain._waveform_history = traced._waveform_history = None
    actual = tracing.stream(traced, signal)
    assert actual == tracing.stream(plain, signal)
    early, = [e for e in actual if e['midi_note'] == 28 and .2<e['onset_s']<.4]
    assert early['timing_uncertain']
    report, = [r for r in traced.trace if r['kind'] == 'report' and r['admitted']
               and r['note'] == early]
    owner = report['attack']
    a = owner['timing_ambiguity']
    assert owner['origin']['opened_hop']*traced.hop_size < a['successor_sample']
    assert a['successor_sample'] <= a['confirmation_start_sample']
    assert [e['midi_note'] for e in actual] == [28, 28, 33]
    assert not actual[0]['timing_uncertain'] and not actual[-1]['timing_uncertain']


@pytest.mark.parametrize("rate,phase", [(44100,np.pi/2),(48000,np.pi/2),(96000,0.)])
def test_predictable_old_waveform_removes_false_e_and_keeps_diagnostic_ownership(rate, phase):
    signal = unmasking_signal(rate, phase)
    plain = PitchDetector(instrument=BASS_UKULELE, sample_rate=rate)
    traced = tracing.TracedDetector(instrument=BASS_UKULELE, sample_rate=rate)
    actual = tracing.stream(traced, signal)
    assert actual == tracing.stream(plain, signal)
    assert [e['midi_note'] for e in actual] == [28, 33]
    report, = [r for r in traced.trace if r['kind'] == 'report'
               and r['outcome'] == 'prediction_filter']
    assert not report['admitted'] and report['note']['timing_uncertain']
    owner = report['attack']
    assert owner['report_outcome'] == 'prediction_filtered'
    prediction = owner['waveform_prediction']
    assert prediction['train_end_sample'] < owner['origin']['onset_sample']
    assert prediction['end_sample'] <= owner['timing_ambiguity']['successor_sample']


def detection(at, *, uncertain=False, midi=28):
    return TimestampedNote(DetectedNote(midi, midi_to_freq(midi), .99, "E1", True,
                                      timing_uncertain=uncertain), at)


@pytest.mark.parametrize("partial", [False, True])
def test_chord_credit_never_turns_uncertainty_into_measured_rhythm(partial):
    notes = [NoteEvent(1000, 100, midi, string, 0)
             for midi, string in ((28, 4), (33, 3))]
    matcher = NoteMatcher(Timeline(notes, SongMetadata()), chord_partial_credit=partial)
    result, = matcher.process_detected_notes([detection(800, uncertain=True)], 1000)
    assert sorted(result.matched_events, key=lambda n: n.string) == sorted(notes, key=lambda n: n.string)
    assert result.timing_uncertain and result.timing_error_ms is None
    stats = matcher.get_statistics()
    assert stats["hits"] == stats["timing_unknown"] == 2
    assert stats["in_time"] == stats["timing_measured"] == 0


@pytest.mark.parametrize("look", ["studio", "stage", "hifi"])
@pytest.mark.parametrize("error", [-400, 0, 400])
def test_uncertain_pitch_is_green_without_a_timing_verdict_or_residual(look, error):
    previous = get_look().key
    try:
        set_look(look)
        note = NoteEvent(1000, 100, 28, 4, 0)
        matcher = NoteMatcher(Timeline([note], SongMetadata()), timing_window_ms=100)
        result, = matcher.process_detected_notes([detection(1000+error, uncertain=True)], 1000+max(error,0))
        assert result.match_type == MatchType.HIT and result.timing_uncertain
        assert result.timing_error_ms is None and matcher.get_note_state(note) == MatchType.HIT
        stats = matcher.get_statistics()
        assert stats['hits'] == 1 and stats['accuracy_percent'] == 100
        assert stats['in_time'] == stats['early'] == stats['late'] == stats['timing_measured'] == 0
        assert stats['timing_unknown'] == 1 and stats['timing_median_ms'] is None
        feedback = FeedbackRenderer(); feedback.add_results([result], 1400)
        assert feedback.streak == 1 and feedback.timing_error_ms(note) is None
        assert feedback.get_note_color(note, (90,90,90), 1400, True) == get_look().feedback_hit
        matcher.reset()
        assert matcher.get_statistics()['timing_unknown'] == 0
    finally:
        set_look(previous)


def test_mixed_timing_uses_only_measured_notes_and_preserves_the_pitch_denominator():
    notes = [NoteEvent(1000+i*1500, 100, 28, 4, 0) for i in range(4)]
    matcher = NoteMatcher(Timeline(notes, SongMetadata()), timing_window_ms=100)
    for note,error,uncertain in zip(notes, [-300,-400,0,20], [False,True,False,False]):
        matcher.process_detected_notes([detection(note.timestamp_ms+error, uncertain=uncertain)],
                                      note.timestamp_ms+max(error,0))
    stats = matcher.get_statistics()
    assert stats['total'] == stats['hits'] == 4 and stats['accuracy_percent'] == 100
    assert stats['timing_measured'] == 3 and stats['timing_unknown'] == 1
    assert stats['in_time'] == 2 and stats['early'] == 1 and stats['late'] == 0
    assert stats['timing_percent'] == pytest.approx(200/3) and stats['timing_median_ms'] == 0
