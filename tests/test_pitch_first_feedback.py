"""The visible success follows pitch; timing remains an independent verdict.

Feed real timestamped attacks through NoteMatcher so these checks also cover
the half-second boundary and do not manufacture successes in the renderer.
No audio device or user's settings are used.
"""

import numpy as np
import pygame
import pytest

from pickhero import i18n
from pickhero.audio.detector import DetectedNote
from pickhero.audio.input import TimestampedNote
from pickhero.audio.note_utils import midi_to_freq, midi_to_name
from pickhero.config import Config
from pickhero.matcher import MatchType, NoteMatcher
from pickhero.tabs.timeline import NoteEvent, SongMetadata, Timeline
from pickhero.ui.colors import dimmed, get_look, set_look
from pickhero.ui.feedback import EFFECT_DURATION_MS, FeedbackRenderer
from pickhero.ui import design, feedback, paint_stage
from pickhero.ui.scrolling import PlayingScreen


@pytest.fixture(params=["studio", "stage", "hifi"])
def look(request):
    previous = get_look().key
    set_look(request.param)
    yield get_look()
    set_look(previous)


def _note(at=1000.0):
    return NoteEvent(at, 100.0, 40, 4, 0)


def _attack(note, error, midi_delta=0):
    midi = note.midi_note + midi_delta
    return TimestampedNote(
        DetectedNote(midi, midi_to_freq(midi), 1.0, midi_to_name(midi), True),
        note.timestamp_ms + error,
    )


def _match(note, error, midi_delta=0):
    matcher = NoteMatcher(Timeline([note], SongMetadata()), timing_window_ms=100.0)
    at = note.timestamp_ms + error
    results = matcher.process_detected_notes([_attack(note, error, midi_delta)], at)
    assert len(results) == 1
    return matcher, results, at


@pytest.mark.parametrize("error", [-500.0, -400.0, 0.0, 400.0, 500.0])
def test_right_note_is_green_throughout_the_half_second_pitch_reach(look, error):
    note = _note()
    matcher, results, at = _match(note, error)
    expected = (MatchType.EARLY if error < -100 else
                MatchType.LATE if error > 100 else MatchType.HIT)
    assert results[0].match_type is expected
    assert results[0].timing_error_ms == error
    renderer = FeedbackRenderer()
    renderer.add_results(results, at)

    assert renderer.get_note_color(note, (1, 2, 3), at, error > 0) == look.feedback_hit
    assert renderer.match_state(note) is expected
    assert renderer.timing_error_ms(note) == error
    stats = matcher.get_statistics()
    assert stats["hits"] == 1 and stats["misses"] == 0
    assert stats["in_time"] == int(error == 0)
    assert stats["early"] == int(error < -100)
    assert stats["late"] == int(error > 100)


@pytest.mark.parametrize("error", [-400.0, 0.0, 400.0])
def test_success_keeps_its_green_after_the_initial_flash(look, error):
    note = _note()
    _, results, at = _match(note, error)
    renderer = FeedbackRenderer()
    renderer.add_results(results, at)
    colour = renderer.get_note_color(note, (1, 2, 3), at + EFFECT_DURATION_MS + 1, True)
    assert colour == dimmed(look.feedback_hit, 0.6)


def test_a_run_of_right_notes_counts_early_and_late_successes(look):
    notes = [_note(1000 + 2000 * index) for index in range(3)]
    matcher = NoteMatcher(Timeline(notes, SongMetadata()), timing_window_ms=100.0)
    renderer = FeedbackRenderer()
    for count, (note, error) in enumerate(zip(notes, [-400.0, 0.0, 400.0]), 1):
        at = note.timestamp_ms + error
        results = matcher.process_detected_notes([_attack(note, error)], at)
        renderer.add_results(results, at)
        assert renderer.streak == count
    stats = matcher.get_statistics()
    assert stats["hits"] == 3 and stats["in_time"] == 1
    assert stats["early"] == stats["late"] == 1


@pytest.mark.parametrize("error", [-400.0, 0.0, 400.0])
@pytest.mark.parametrize("midi_delta", [-1, 1])
def test_wrong_semitone_is_close_and_never_green(look, error, midi_delta):
    note = _note()
    matcher, results, at = _match(note, error, midi_delta)
    assert results[0].match_type is MatchType.CLOSE
    renderer = FeedbackRenderer()
    renderer.add_results(results, at)
    assert renderer.get_note_color(note, (1, 2, 3), at, error > 0) == look.feedback_close
    assert look.feedback_close != look.feedback_hit
    assert renderer.streak == 0
    stats = matcher.get_statistics()
    assert stats["hits"] == 0 and stats["close"] == 1


def test_missing_note_keeps_its_miss_feedback(look):
    note = _note()
    matcher = NoteMatcher(Timeline([note], SongMetadata()))
    renderer = FeedbackRenderer()
    renderer.add_results(matcher.finish(), 1600.0)
    assert renderer.match_state(note) is MatchType.MISS
    assert renderer.get_note_color(note, (1, 2, 3), 1600.0, True) == look.feedback_miss
    assert renderer.streak == 0
    assert matcher.get_statistics()["hits"] == 0


@pytest.mark.parametrize("error", [-501.0, 501.0])
def test_an_unassigned_attack_does_not_invent_a_success(look, error):
    note = _note()
    matcher = NoteMatcher(Timeline([note], SongMetadata()))
    at = note.timestamp_ms + error
    results = matcher.process_detected_notes([_attack(note, error)], at)
    assert results[0].match_type is MatchType.STRAY
    renderer = FeedbackRenderer()
    renderer.add_results(results, at)
    assert renderer.streak == 0
    assert matcher.get_statistics()["hits"] == 0
    assert renderer.get_note_color(note, (1, 2, 3), at, error > 0) != look.feedback_hit


@pytest.fixture
def drawing(monkeypatch):
    pygame.display.init()
    pygame.font.init()
    pygame.display.set_mode((1, 1))
    previous = get_look().key
    monkeypatch.setattr(PlayingScreen, "_init_midi_player", lambda *args: None)
    monkeypatch.setattr(feedback, "_display_time", lambda: 123.0)
    yield
    set_look(previous)
    design.clear_caches()
    pygame.display.quit()


def _playing_match(error):
    note = NoteEvent(1000.0, 100.0, 33, 3, 0)
    tuning = {1: 43, 2: 38, 3: 33, 4: 28}
    timeline = Timeline([note], SongMetadata(tempo=60, tuning=tuning))
    screen = PlayingScreen(timeline, config=Config(instrument="bass_guitar"))
    screen._matcher = NoteMatcher(timeline, timing_window_ms=150.0)
    screen._playback_ms = note.timestamp_ms + error
    screen._audio_enabled = True
    screen._playing = True
    results = screen._matcher.process_detected_notes([_attack(note, error)], screen._playback_ms)
    screen._feedback.add_results(results, screen._playback_ms)
    return screen, note


@pytest.mark.parametrize("theme,error", [
    ("studio", -400.0), ("studio", 0.0), ("studio", 400.0),
    ("hifi", -400.0), ("hifi", 0.0), ("hifi", 400.0),
    ("stage", -400.0), ("stage", 0.0),
])
def test_moving_note_rings_and_glow_celebrate_correct_pitch(
        monkeypatch, drawing, theme, error):
    set_look(theme)
    screen, _ = _playing_match(error)
    rings = []
    if theme == "studio":
        original = design.card

        def card(surface, rect, *args, **kwargs):
            if kwargs.get("ring") is not None:
                rings.append((kwargs["ring"], kwargs["ring_width"], kwargs["glow"]))
            return original(surface, rect, *args, **kwargs)

        monkeypatch.setattr(design, "card", card)
    elif theme == "hifi":
        original = screen._draw_disc

        def disc(surface, rect, fill, ring, ring_w, glow):
            rings.append((ring, ring_w, glow))
            return original(surface, rect, fill, ring, ring_w, glow)

        monkeypatch.setattr(screen, "_draw_disc", disc)
    else:
        original = paint_stage.capsule

        def capsule(*args, **kwargs):
            rings.append((kwargs.get("ring"), kwargs.get("ring_width"), kwargs.get("halo")))
            return original(*args, **kwargs)

        monkeypatch.setattr(paint_stage, "capsule", capsule)

    screen.render(pygame.Surface((1280, 720)))
    assert any(colour == get_look().feedback_hit and width == 5 and glow > 0
               for colour, width, glow in rings)


@pytest.mark.parametrize("size", [(320, 240), (640, 480), (1280, 720), (1280, 240)])
@pytest.mark.parametrize("error", [-500.0, -400.0, 400.0, 500.0])
def test_stage_green_success_and_blue_timing_are_separate_visible_messages(
        monkeypatch, drawing, size, error):
    set_look("stage")
    screen, _ = _playing_match(error)
    # A late note is culled from the road. The stationary success must still
    # confirm the actual correct strike, along with the secondary timing.
    monkeypatch.setattr(screen, "_reading_window", lambda *args: 750.0)
    badges = []
    heads = []
    original = paint_stage.strike_feedback

    def badge(surface, page, lane, lanes, label, symbol, colour, strength=1.0, **kwargs):
        rect = original(surface, page, lane, lanes, label, symbol, colour, strength, **kwargs)
        badges.append((rect, label, symbol, colour, kwargs))
        return rect

    monkeypatch.setattr(paint_stage, "strike_feedback", badge)
    monkeypatch.setattr(screen, "_draw_fret", lambda *args, **kwargs: heads.append(args))
    canvas = pygame.Surface(size)
    screen.render(canvas)
    if error > 0:
        assert not heads
    assert len(badges) == 1
    rect, label, symbol, colour, timing = badges[0]
    assert label == i18n.t("stage.verdict.hit")
    assert symbol == "hit" and colour == get_look().feedback_hit
    direction = "early" if error < 0 else "late"
    assert timing["timing_label"] == i18n.t("stage.verdict." + direction)
    assert timing["timing_direction"] == direction
    assert canvas.get_rect().contains(rect)
    success_pixels = pygame.surfarray.array3d(canvas.subsurface(rect))
    assert np.any(np.all(success_pixels == get_look().feedback_hit, axis=2))
    timing_rect = pygame.Rect(rect.left, rect.bottom, rect.width,
                              design.reading_font(11).get_linesize() + 4)
    timing_rect = timing_rect.clip(canvas.get_rect())
    timing_pixels = pygame.surfarray.array3d(canvas.subsurface(timing_rect))
    assert np.any(np.all(timing_pixels == get_look().feedback_early, axis=2)), (
        "The secondary timing caption/arrow must render blue below the green success")
