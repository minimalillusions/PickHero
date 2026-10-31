"""Timing advice must agree with actual scoring and measured practice conditions.

Only the capture device is simulated. These regressions feed timestamped
attacks through the real matcher, rather than inventing percentage summaries.
"""
from types import SimpleNamespace

import pytest

from pickhero import i18n
from pickhero.audio.detector import DetectedNote
from pickhero.audio.input import TimestampedNote
from pickhero.audio.note_utils import midi_to_freq, midi_to_name
from pickhero.config import Config
from pickhero.matcher import MatchType
from pickhero.tabs.timeline import NoteEvent, SongMetadata, Timeline
from pickhero.ui.colors import get_look, set_look
from pickhero.ui.scrolling import PlayingScreen


@pytest.fixture(autouse=True)
def simulated_capture(monkeypatch):
    class Capture:
        sample_rate = 44100
        detector = SimpleNamespace(expected_delay_ms=0)

        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

        def capture_elapsed_ms(self):
            return 0

    monkeypatch.setattr("pickhero.audio.input.AudioCapture", Capture)
    monkeypatch.setattr(PlayingScreen, "_init_midi_player", lambda *args: None)
    previous_look = get_look().key
    yield
    set_look(previous_look)


@pytest.fixture(params=["de", "en"])
def language(request):
    previous = i18n.current_language()
    i18n.set_language(request.param)
    yield request.param
    i18n.set_language(previous)


def assessed(errors, *, instrument="bass_guitar", window=100, tempo=1, missing=0):
    config = Config(instrument=instrument, timing_window_ms=window,
                    tempo_factor=tempo, count_in_beats=0)
    midi = min(config.get_instrument().tuning.values())
    string = max(config.get_instrument().tuning)
    # Wide spacing makes the written target unambiguous, even at +/-300 ms.
    notes = [NoteEvent(1000 + index * 1500, 100, midi, string, 0)
             for index in range(len(errors) + missing)]
    screen = PlayingScreen(Timeline(notes, SongMetadata(tempo=80)), config=config)
    screen._start_audio()
    assert screen._audio_enabled and screen._matcher is not None
    for note, error in zip(notes, errors):
        attack = TimestampedNote(
            DetectedNote(midi, midi_to_freq(midi), 1, midi_to_name(midi), True),
            note.timestamp_ms + error)
        screen._matcher.process_detected_notes([attack], note.timestamp_ms + max(0, error))
    screen._matcher.finish()
    return screen, screen._matcher.get_statistics()


def translated(key, **values):
    rendered = i18n.t(key, **values)
    assert rendered != key, f"Missing timing feedback translation: {key}"
    assert "{ms}" not in rendered
    return rendered


@pytest.mark.parametrize("look", ["studio", "stage", "hifi"])
def test_recent_session_describes_73_ms_as_within_tolerance_without_hiding_six_early_notes(
        language, look):
    set_look(look)
    screen, stats = assessed([-73] * 24 + [-287] * 6, missing=2)
    assert stats["hits"] == 30 and stats["total"] == 32
    assert stats["in_time"] == 24 and stats["early"] == 6
    assert stats["timing_median_ms"] == -73
    before = dict(stats)
    assert screen._timing_advice(stats) == translated("play.timing_within_early", ms=73)
    assert screen._matcher.get_statistics() == before


@pytest.mark.parametrize("sign,direction", [(-1, "early"), (1, "late")])
@pytest.mark.parametrize("instrument,within", [("bass_guitar", True), ("guitar", False)])
def test_advice_uses_the_instrument_window_that_actually_scored_the_notes(
        language, sign, direction, instrument, within):
    screen, stats = assessed([sign * 125] * 6, instrument=instrument)
    assert stats["hits"] == 6
    assert stats["in_time"] == (6 if within else 0)
    key = f"play.timing_{'within_' if within else ''}{direction}"
    assert screen._timing_advice(stats) == translated(key, ms=125)


@pytest.mark.parametrize("instrument,requested,actual", [
    ("guitar", 100, 100), ("bass_guitar", 100, 150), ("guitar", 200, 200),
])
@pytest.mark.parametrize("distance,outside", [(0, False), (1, True)])
@pytest.mark.parametrize("sign,direction", [(-1, "early"), (1, "late")])
def test_window_boundaries_and_user_overrides_agree_with_match_classification(
        language, instrument, requested, actual, distance, outside, sign, direction):
    error = sign * (actual + distance)
    screen, stats = assessed([error] * 6, instrument=instrument, window=requested)
    states = [screen._matcher.get_note_state(note) for note in screen._timeline.notes]
    expected_state = ((MatchType.EARLY if sign < 0 else MatchType.LATE)
                      if outside else MatchType.HIT)
    assert states == [expected_state] * 6
    key = f"play.timing_{'' if outside else 'within_'}{direction}"
    assert screen._timing_advice(stats) == translated(key, ms=abs(error))


def test_large_measured_bias_still_requests_timing_correction(language):
    screen, stats = assessed([-287] * 19 + [0] * 2)
    assert stats["hits"] == 21 and stats["in_time"] == 2
    assert stats["early"] == 19
    assert screen._timing_advice(stats) == translated("play.timing_early", ms=287)


@pytest.mark.parametrize("errors,key", [
    ([-300, 300] * 4, "play.timing_mixed"),
    ([0] * 7 + [-300, 300, 300], "play.timing_mixed"),
    ([0] * 8 + [-300, 300], "play.timing_solid"),
    ([0] * 6, "play.timing_solid"),
])
def test_balanced_errors_are_not_automatically_a_steady_pulse(language, errors, key):
    screen, stats = assessed(errors)
    assert stats["timing_median_ms"] == 0
    assert screen._timing_advice(stats) == translated(key)


@pytest.mark.parametrize("tempo,song_ms,real_ms", [(.5, -73, 146), (.25, 40, 160), (.5, -20, 40)])
def test_feedback_milliseconds_describe_real_time_at_reduced_practice_speed(
        language, tempo, song_ms, real_ms):
    screen, stats = assessed([song_ms] * 6, tempo=tempo)
    assert stats["in_time"] == 6
    direction = "early" if song_ms < 0 else "late"
    assert screen._timing_advice(stats) == translated(f"play.timing_within_{direction}", ms=real_ms)


@pytest.mark.parametrize("real_ms,key", [(25, "play.timing_solid"), (26, "play.timing_within_early")])
def test_neutral_feedback_threshold_is_also_in_real_milliseconds(language, real_ms, key):
    screen, stats = assessed([-real_ms * .5] * 6, tempo=.5)
    assert stats["in_time"] == 6
    assert screen._timing_advice(stats) == translated(key, ms=real_ms)


def test_changed_tempo_does_not_invent_one_real_time_median(language):
    screen, stats = assessed([-73] * 6, tempo=.5)
    screen._attempt_uniform_tempo = False
    assert screen._timing_advice(stats) == translated("play.timing_varied")


@pytest.mark.parametrize("count", [0, 5])
def test_too_few_measured_attacks_do_not_produce_timing_advice(language, count):
    screen, stats = assessed([-73] * count, missing=1)
    assert stats["hits"] == count
    assert screen._timing_advice(stats) == ""


@pytest.mark.parametrize("condition", [
    "wait", "input_disconnected", "input_stalled", "input_overflow", "input_clipping",
])
def test_unassessed_or_unreliable_attempts_do_not_blame_the_players_timing(language, condition):
    screen, stats = assessed([-287] * 6)
    if condition == "wait":
        screen._timing_assessed = False
    else:
        screen.mark_attempt_unreliable(condition)
    assert screen._timing_advice(stats) == ""
