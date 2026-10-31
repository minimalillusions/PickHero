"""Stage verdicts survive moving-note culling and stay readable in real time."""

import numpy as np
import pygame
import pytest

from pickhero.config import Config
from pickhero.matcher import MatchResult, MatchType
from pickhero.tabs.timeline import NoteEvent, SongMetadata, Timeline
from pickhero.ui import design, feedback, paint_stage
from pickhero.ui.colors import get_look, set_look
from pickhero.ui.page import Page
from pickhero.ui.scrolling import PlayingScreen


@pytest.fixture
def display_clock(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(feedback, "_display_time", lambda: clock[0])
    return clock


@pytest.fixture
def drawing(monkeypatch):
    pygame.display.init()
    pygame.font.init()
    pygame.display.set_mode((1, 1))
    previous = get_look().key
    set_look("stage")
    monkeypatch.setattr(PlayingScreen, "_init_midi_player", lambda *args: None)
    yield
    set_look(previous)
    design.clear_caches()
    pygame.display.quit()


def _note(at=1000.0, string=3):
    tuning = {1: 43, 2: 38, 3: 33, 4: 28}
    return NoteEvent(at, 100.0, tuning[string], string, 0)


def _result(note, state=MatchType.HIT):
    return MatchResult(state, matched_events=[note])


def _screen(monkeypatch, preview=750.0, tempo=1.0):
    note = _note()
    tuning = {1: 43, 2: 38, 3: 33, 4: 28}
    timeline = Timeline([note], SongMetadata(tempo=60, tuning=tuning))
    screen = PlayingScreen(timeline, config=Config(instrument="bass_ukulele"))
    screen._playing = True
    screen._tempo_factor = tempo
    # The report arrives 250 song milliseconds after the written note, well
    # beyond the old 2%-of-preview lifetime, even with an eight-second view.
    screen._playback_ms = 1250.0
    monkeypatch.setattr(screen, "_reading_window", lambda *args: preview)
    return screen, note


def _record_badges(monkeypatch):
    calls = []
    original = paint_stage.strike_feedback

    def record(surface, page, lane, lanes, label, symbol, colour, strength=1.0, **kwargs):
        rect = original(surface, page, lane, lanes, label, symbol, colour, strength, **kwargs)
        calls.append((rect.copy(), lane, symbol, colour, strength))
        return rect

    monkeypatch.setattr(paint_stage, "strike_feedback", record)
    return calls


@pytest.mark.parametrize("preview", [750.0, 8000.0])
@pytest.mark.parametrize("tempo", [0.5, 1.0, 1.5])
@pytest.mark.parametrize("age_ms", [300.0, 600.0])
def test_delayed_hit_is_still_visible_after_moving_note_has_left_the_road(
        monkeypatch, drawing, display_clock, preview, tempo, age_ms):
    screen, note = _screen(monkeypatch, preview, tempo)
    canvas = pygame.Surface((1280, 720))
    badges = _record_badges(monkeypatch)
    heads = []
    monkeypatch.setattr(screen, "_draw_fret", lambda *args, **kwargs: heads.append(args))
    screen.render(canvas)
    assert not heads and not badges

    screen._feedback.add_results([_result(note)], screen._playback_ms)
    display_clock[0] += age_ms / 1000
    screen._playback_ms += age_ms * tempo
    screen.render(canvas)

    assert not heads, "The regression needs a culled note, not an extended note lifetime"
    assert len(badges) == 1
    rect, lane, symbol, colour, strength = badges[0]
    assert lane == 1 and symbol == "hit" and colour == get_look().feedback_hit
    assert strength >= 0.79
    assert rect.top > paint_stage.strike_y(screen._last_page)
    before = pygame.surfarray.array3d(canvas.subsurface(rect)).copy()
    # The green feedback is actual rendered content, not just a scheduled
    # draw call. Removing it at the same song/display time removes pixels.
    screen._feedback.reset()
    screen.render(canvas)
    after = pygame.surfarray.array3d(canvas.subsurface(rect))
    assert np.count_nonzero(np.any(before != after, axis=2)) > rect.width


def test_expiry_uses_wall_time_even_when_wait_freezes_the_song(
        monkeypatch, drawing, display_clock):
    screen, note = _screen(monkeypatch)
    badges = _record_badges(monkeypatch)
    canvas = pygame.Surface((1280, 720))
    screen._feedback.add_results([_result(note)], screen._playback_ms)
    display_clock[0] += 0.7
    screen.render(canvas)
    assert len(badges) == 1 and badges[0][-1] > 0
    badges.clear()
    display_clock[0] += 0.101
    screen.render(canvas)
    assert not badges and not screen._feedback.recent_lane_verdicts()
    assert screen._playback_ms == 1250.0


def test_song_clock_cleanup_does_not_remove_a_recent_visible_verdict(display_clock):
    renderer = feedback.FeedbackRenderer()
    note = _note()
    renderer.add_results([_result(note)], 1250.0)
    renderer.cleanup(10000.0)
    assert renderer.match_state(note) is None
    display_clock[0] += 0.3
    assert renderer.recent_lane_verdicts()[0][:2] == (note, MatchType.HIT)


def test_new_hit_on_same_string_replaces_and_restarts_the_badge(display_clock):
    renderer = feedback.FeedbackRenderer()
    first, second = _note(), _note(1300.0)
    renderer.add_results([_result(first)], 1250.0)
    display_clock[0] += 0.6
    renderer.add_results([_result(second)], 1550.0)
    display_clock[0] += 0.3
    verdicts = renderer.recent_lane_verdicts()
    assert len(verdicts) == 1
    assert verdicts[0][:2] == (second, MatchType.HIT)
    assert verdicts[0][2] == pytest.approx(300)


def test_late_retirement_of_older_note_does_not_overwrite_newer_hit(display_clock):
    renderer = feedback.FeedbackRenderer()
    newer = _note(1300.0)
    renderer.add_results([_result(newer)], 1550.0)
    display_clock[0] += 0.2
    renderer.add_results([_result(_note(), MatchType.MISS)], 1750.0)
    assert renderer.recent_lane_verdicts()[0][:2] == (newer, MatchType.HIT)
    assert renderer.recent_lane_verdicts()[0][2] == pytest.approx(200)


def test_strings_expire_independently_and_reset_clears_every_badge(display_clock):
    renderer = feedback.FeedbackRenderer()
    first, second = _note(), _note(1200.0, string=2)
    renderer.add_results([_result(first)], 1250.0)
    display_clock[0] += 0.4
    renderer.add_results([_result(second, MatchType.EARLY)], 1650.0)
    assert len(renderer.recent_lane_verdicts()) == 2
    display_clock[0] += 0.401
    assert [item[:2] for item in renderer.recent_lane_verdicts()] == [
        (second, MatchType.EARLY)]
    renderer.reset()
    assert not renderer.recent_lane_verdicts()


@pytest.mark.parametrize("state", [MatchType.HIT, MatchType.EARLY, MatchType.LATE,
                                  MatchType.CLOSE, MatchType.MISS])
def test_green_confirms_the_right_pitch_with_timing_kept_separate(
        monkeypatch, drawing, display_clock, state):
    screen, note = _screen(monkeypatch)
    badges = _record_badges(monkeypatch)
    screen._feedback.add_results([_result(note, state)], screen._playback_ms)
    screen.render(pygame.Surface((1280, 720)))
    assert len(badges) == 1
    on_pitch = state in (MatchType.HIT, MatchType.EARLY, MatchType.LATE)
    assert badges[0][2] == ("hit" if on_pitch else state.value)
    assert (badges[0][3] == get_look().feedback_hit) is on_pitch


@pytest.mark.parametrize("state", [MatchType.PENDING, MatchType.STRAY])
def test_pending_and_unmatched_notes_do_not_create_a_verdict(display_clock, state):
    renderer = feedback.FeedbackRenderer()
    renderer.add_results([_result(_note(), state)], 1250.0)
    assert not renderer.recent_lane_verdicts()


@pytest.mark.parametrize("look", ["studio", "hifi"])
def test_other_designs_keep_their_own_feedback(monkeypatch, drawing, display_clock, look):
    set_look(look)
    screen, note = _screen(monkeypatch)
    badges = _record_badges(monkeypatch)
    screen._feedback.add_results([_result(note)], screen._playback_ms)
    screen.render(pygame.Surface((1280, 720)))
    assert not badges


def test_disabling_listening_hides_old_verdicts(monkeypatch, drawing, display_clock):
    screen, note = _screen(monkeypatch)
    badges = _record_badges(monkeypatch)
    screen._feedback.add_results([_result(note)], screen._playback_ms)
    screen._audio_enabled = False
    screen.render(pygame.Surface((1280, 720)))
    assert not badges


def test_seeking_starts_without_the_previous_attempts_verdicts(
        monkeypatch, drawing, display_clock):
    screen, note = _screen(monkeypatch)
    screen._playing = False
    screen._feedback.add_results([_result(note)], screen._playback_ms)
    assert screen._feedback.recent_lane_verdicts()
    screen.seek(0)
    assert not screen._feedback.recent_lane_verdicts()
    badges = _record_badges(monkeypatch)
    screen.render(pygame.Surface((1280, 720)))
    assert not badges


@pytest.mark.parametrize("size", [(320, 240), (640, 480), (1280, 720), (1280, 240)])
@pytest.mark.parametrize("lanes", [4, 6])
def test_raised_target_and_verdicts_fit_without_covering_pads(drawing, size, lanes):
    page = Page(*size)
    surface = pygame.Surface(size)
    surface.fill(get_look().stage_bg)
    target = paint_stage.strike_y(page)
    assert target <= round(size[1] * 0.82), "Leave visible space below the target"
    badges = []
    for lane in range(lanes):
        pad = paint_stage.strike_ring(surface, page, lane, lanes, name="E1")
        badge = paint_stage.strike_feedback(surface, page, lane, lanes,
                                            "Im Takt", "hit", get_look().feedback_hit)
        assert pad.bottom < badge.top
        assert surface.get_rect().contains(badge)
        assert paint_stage.project(page, lane, lanes, 0)[1] == target
        assert paint_stage.note_box(page, lane, lanes, 0).centery == target
        assert all(not badge.colliderect(previous) for previous in badges)
        badges.append(badge)
