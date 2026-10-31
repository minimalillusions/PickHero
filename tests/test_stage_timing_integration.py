"""Actual visible heads, capture timestamps and comparable practice results."""

import hashlib
import json
from types import SimpleNamespace

import pygame
import pytest

from pickhero.audio.detector import DetectedNote
from pickhero.audio.input import TimestampedNote
from pickhero.audio.note_utils import midi_to_freq, midi_to_name
from pickhero.config import Config
from pickhero.matcher import MatchType, NoteMatcher
from pickhero.practice_advice import (
    comparable_attempts, filter_key_for_state, input_fingerprint, section_advice,
)
from pickhero.tabs.timeline import NoteEvent, SongMetadata, Timeline
from pickhero.ui import design, paint_stage
from pickhero.ui.colors import get_look, set_look
from pickhero.ui.scrolling import PlayingScreen


@pytest.fixture
def environment(monkeypatch):
    pygame.display.init()
    pygame.font.init()
    pygame.display.set_mode((1, 1))
    previous = get_look().key
    set_look("stage")
    clock = SimpleNamespace(now=100.0)

    class Capture:
        sample_rate = 44100
        detector = SimpleNamespace(expected_delay_ms=0)

        def __init__(self, *args, **kwargs):
            self.started = clock.now
            self.notes = []

        def start(self):
            self.started = clock.now

        def stop(self):
            pass

        def capture_elapsed_ms(self):
            return (clock.now - self.started) * 1000

        def get_notes(self):
            notes, self.notes = self.notes, []
            return notes

        def get_signal_db(self):
            return -25.0

        def get_tuner_data(self):
            return 0.0, 0.0

    monkeypatch.setattr("pickhero.audio.input.AudioCapture", Capture)
    monkeypatch.setattr("pickhero.ui.scrolling.time.perf_counter", lambda: clock.now)
    monkeypatch.setattr(PlayingScreen, "_init_midi_player", lambda *args: None)
    yield clock
    set_look(previous)
    design.clear_caches()
    pygame.display.quit()


def _screen(instrument="bass_guitar", tempo=1.0, *, first=2000.0, count_in=0,
            requested_window=100.0):
    config = Config(instrument=instrument, tempo_factor=tempo,
                    count_in_beats=count_in, timing_window_ms=requested_window)
    tuning = config.get_instrument().tuning
    string = len(tuning)
    notes = [NoteEvent(at, 100.0, tuning[string], string, 0)
             for at in (first, first + 10000.0)]
    timeline = Timeline(notes, SongMetadata(tempo=120, tuning=tuning))
    return PlayingScreen(timeline, config=config), notes[0]


def _heard(note, at):
    midi = note.midi_note
    return TimestampedNote(
        DetectedNote(midi, midi_to_freq(midi), 1.0, midi_to_name(midi), True), at)


def _actual_line(page):
    strip = pygame.Surface((3, page.height))
    y = paint_stage.strike_y(page)
    return pygame.draw.line(strip, (255, 255, 255), (0, y), (2, y),
                            max(2, round(page.height * 0.0049)))


@pytest.mark.parametrize("size", [(320, 240), (1280, 720), (2560, 1080), (2560, 240)])
@pytest.mark.parametrize("instrument", ["bass_guitar", "guitar"])
@pytest.mark.parametrize("tempo", [0.5, 1.0])
@pytest.mark.parametrize("direction", [-1, 1])
def test_rendered_head_touching_the_target_is_in_time_through_the_capture_clock(
        environment, monkeypatch, size, instrument, tempo, direction):
    screen, note = _screen(instrument, tempo)
    canvas = pygame.Surface(size)
    screen.render(canvas)
    screen.toggle_play()
    assert screen._matcher is not None and screen._audio_enabled
    page = screen._last_page
    target = _actual_line(page)
    preview = screen._visible_window_ms
    lanes = len(screen._tuning)
    # Independently sample the actual integer head rectangles, not the
    # timing helper's returned threshold or its binary-search algorithm.
    touching = []
    for magnitude in range(5, 1501, 5):
        error = direction * magnitude
        head = paint_stage.note_box(page, 0, lanes, -error / preview)
        if head.top < target.bottom and head.bottom > target.top:
            touching.append(error)
    assert touching
    error = touching[-1]
    rectangles = []
    original = screen._draw_fret

    def fret(surface, rect, *args, **kwargs):
        rectangles.append(rect.copy())
        return original(surface, rect, *args, **kwargs)

    monkeypatch.setattr(screen, "_draw_fret", fret)
    elapsed = (note.timestamp_ms + error) / tempo
    capture = screen._audio_capture
    environment.now = capture.started + elapsed / 1000
    capture.notes = [_heard(note, elapsed)]
    screen.update()
    assert screen._playback_ms == pytest.approx(note.timestamp_ms + error)
    assert screen._matcher.get_note_state(note) is MatchType.HIT
    assert screen._matcher.timing_errors == pytest.approx([error])
    screen.render(canvas)
    expected = paint_stage.note_box(screen._last_page, 0, lanes, -error / preview)
    assert expected in rectangles, "The scored head must actually remain visible at the target"
    assert expected.top < target.bottom and expected.bottom > target.top
    if direction > 0:
        assert expected.centery > target.centery, "Late heads must run past the target"


@pytest.mark.parametrize("theme", ["studio", "hifi"])
@pytest.mark.parametrize("instrument,base", [("bass_guitar", 150.0), ("guitar", 100.0)])
@pytest.mark.parametrize("requested", [100.0, 250.0])
def test_flat_designs_keep_instrument_and_user_timing_tolerance(
        environment, theme, instrument, base, requested):
    screen, _ = _screen(instrument, requested_window=requested)
    screen.render(pygame.Surface((2560, 240)))
    screen.toggle_play()
    set_look(theme)
    screen.render(pygame.Surface((1280, 720)))
    assert screen._matcher._timing_window_ms == max(base, requested)
    assert screen._matcher._reach_ms == 500.0


@pytest.mark.parametrize("tempo", [0.5, 1.0])
@pytest.mark.parametrize("song_error", [-501.0, -500.0, -400.0])
@pytest.mark.parametrize("delayed_report", [False, True])
def test_count_in_accepts_the_first_note_through_the_pitch_reach(
        environment, tempo, song_error, delayed_report):
    screen, note = _screen(tempo=tempo, first=0.0, count_in=4)
    screen.render(pygame.Surface((1280, 720)))
    screen.toggle_play()
    assert screen._preroll_before_ms == -500.0
    capture = screen._audio_capture
    elapsed = (screen._count_in_ms + song_error) / tempo
    environment.now = capture.started + elapsed / 1000 + (0.6 if delayed_report else 0)
    capture.notes = [_heard(note, screen._audio_anchor_capture_ms + elapsed)]
    screen.update()
    if song_error < -500:
        assert screen._matcher.get_note_state(note) is MatchType.PENDING
        assert not screen._matcher.stray
    else:
        assert screen._matcher.get_note_state(note) is MatchType.EARLY
        assert screen._matcher.hits == 1 and screen._matcher.in_time == 0
        assert screen._matcher.timing_errors == pytest.approx([song_error])


def test_widened_matcher_reach_scores_late_visible_head_before_retirement():
    note = NoteEvent(1000.0, 100.0, 28, 4, 0)
    matcher = NoteMatcher(Timeline([note], SongMetadata()))
    matcher.set_timing_window(700.0)
    assert not matcher.process_detected_notes([], 1690.0)
    result = matcher.process_detected_notes([_heard(note, 1690.0)], 1690.0)
    assert result[0].match_type is MatchType.HIT
    assert matcher.completion_grace_ms == 701.0
    matcher.set_timing_window(100.0)
    assert matcher.completion_grace_ms == 501.0
    assert matcher.get_note_state(note) is MatchType.HIT
    assert matcher.timing_errors == [690.0]


def _passage(input_key, *, uniform=True):
    state = {"active_strings": [True] * 4, "max_fret": 12,
             "chord_partial_credit": False, "wait_mode": False,
             "tempo_factor": .5, "loop_start_ms": 0.0, "loop_end_ms": 8000.0}
    entry = {"start_ms": 0.0, "end_ms": 8000.0, "tempo_factor": .5,
             "filter_key": filter_key_for_state(state), "total": 8,
             "accuracy": 95.0, "timing_percent": 95.0, "reliable": True,
             "uniform_tempo": True, "uniform_wait": True, "uniform_input": uniform,
             "complete_loop": True, "timing_assessed": True, "wait_mode": False,
             "input_key": input_key, "completed_at": "2026-10-04T12:00:00+00:00"}
    return state, entry


def test_resize_during_attempt_stays_noncomparable_even_after_resizing_back(environment):
    screen, _ = _screen(tempo=.5)
    small, wide = pygame.Surface((1280, 720)), pygame.Surface((2560, 240))
    screen.render(small)
    screen.toggle_play()
    original_key = screen._input_key()
    assert screen._attempt_uniform_input
    screen._playback_ms = 1000.0
    screen.render(wide)
    assert screen._input_key() != original_key
    assert not screen._attempt_uniform_input
    screen.render(small)
    assert screen._input_key() == original_key
    assert not screen._attempt_uniform_input
    recorded = []
    screen._song_key = "isolated-passage"
    screen._progress_tracker = SimpleNamespace(
        record_section_result=lambda key, stats, context: recorded.append(dict(context)),
        claim_section_advice=lambda key: None,
    )
    screen._record_section_attempt(8000.0)
    assert len(recorded) == 1
    assert recorded[0]["input_key"] == original_key
    assert recorded[0]["uniform_input"] is False
    state, mixed = _passage(original_key, uniform=screen._attempt_uniform_input)
    assert comparable_attempts([mixed], state, original_key) == []
    assert section_advice([mixed] * 3) is None


def test_resize_before_start_sets_new_comparable_conditions(environment):
    screen, _ = _screen(tempo=.5)
    screen.render(pygame.Surface((1280, 720)))
    screen._start_audio()
    previous_key = screen._input_key()
    screen.render(pygame.Surface((2560, 240)))
    assert screen._input_key() != previous_key
    assert screen._attempt_input_key == screen._input_key()
    assert screen._attempt_uniform_input
    state, old = _passage(previous_key)
    _, current = _passage(screen._input_key())
    assert comparable_attempts([old, current], state, screen._input_key()) == [current]
    assert section_advice([old, old, current]) is None
    assert section_advice([current] * 3)["kind"] == "raise"


def test_policy_two_and_effective_window_separate_legacy_passage_results(monkeypatch):
    config = Config(instrument="bass_guitar")
    instrument = config.get_instrument()
    encoded = []
    sha256 = hashlib.sha256

    def capture_payload(data):
        encoded.append(json.loads(data))
        return sha256(data)

    monkeypatch.setattr("pickhero.practice_advice.hashlib.sha256", capture_payload)
    current_key = input_fingerprint(config, instrument, timing_window_ms=300.0)
    payload = encoded[-1]
    assert payload["scoring_policy"] == 2
    assert payload["effective_timing_window_ms"] == 300.0
    payload.pop("scoring_policy")
    payload.pop("effective_timing_window_ms")
    legacy_key = sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    assert current_key != legacy_key
    assert current_key != input_fingerprint(config, instrument, timing_window_ms=150.0)
    state, legacy = _passage(legacy_key)
    _, current = _passage(current_key)
    assert comparable_attempts([legacy, current], state, current_key) == [current]
    assert section_advice([legacy, legacy, current]) is None
