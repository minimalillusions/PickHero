"""Playback regressions using a deterministic clock and no physical devices."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from pickhero.audio.detector import DetectedNote
from pickhero.audio.input import TimestampedNote
from pickhero.config import Config
from pickhero.matcher import MatchType, NoteMatcher
from pickhero.tabs.timeline import BeatInfo, MeasureInfo, NoteEvent, SongMetadata, Timeline
from pickhero.ui.scrolling import PlayingScreen


def event(at, midi=64, string=1, duration=100):
    return NoteEvent(at, duration, midi, string, 0)


def heard(at, midi=64):
    return TimestampedNote(DetectedNote(midi, 440.0, 1.0, "test", True), at)


class Capture:
    def __init__(self, config, *, instrument=None):
        self.instrument = instrument
        self.detector = SimpleNamespace(expected_delay_ms=200.0)
        self.elapsed = 0.0
        self.notes = []
        self.starts = 0
        self.sample_rate = 44100

    def start(self):
        self.starts += 1
        self.elapsed = 0.0
        self.notes.clear()

    def stop(self):
        pass

    def capture_elapsed_ms(self):
        return self.elapsed

    def get_notes(self):
        notes, self.notes = self.notes, []
        return notes

    def get_signal_db(self):
        return -30.0

    def get_tuner_data(self):
        return 0.0, 0.0


@pytest.fixture
def screen_factory(monkeypatch):
    monkeypatch.setattr("pickhero.audio.input.AudioCapture", Capture)
    monkeypatch.setattr(PlayingScreen, "_init_midi_player", lambda *args: None)
    monkeypatch.setattr(PlayingScreen, "_log_result", lambda *args: None)
    monkeypatch.setattr("pickhero.ui.scrolling.time.perf_counter", lambda: 100.0)

    def make(notes=None, config=None, timeline=None, tracker=None):
        notes = notes if notes is not None else [event(0), event(1000), event(3000)]
        screen = PlayingScreen(timeline or Timeline(notes),
                               config=config or Config(count_in_beats=0),
                               progress_tracker=tracker, song_key="test-song")
        screen._midi_player = Mock()
        return screen

    return make


def frame_at(screen, song_ms):
    """Advance through the real update loop, without changing its song position."""
    screen._last_tick = 100.0 - (song_ms - screen._playback_ms) / 1000 / screen._tempo_factor
    screen.update()


def test_pause_resume_keeps_hits_and_matcher(screen_factory):
    screen = screen_factory()
    screen.toggle_play()
    matcher = screen._matcher
    matcher.process_detected_notes([heard(0), heard(1000)], 1200)
    screen._playback_ms = 1800
    screen.toggle_play()
    screen.toggle_play()
    assert screen._matcher is matcher
    matcher.process_detected_notes([], 1800)
    assert matcher.get_statistics()["hits"] == 2
    assert matcher.get_statistics()["misses"] == 0
    assert matcher.audio_offset_ms == 1800


def test_seek_scores_only_remaining_interval_and_clears_completion(screen_factory):
    screen = screen_factory()
    screen.toggle_play()
    screen._song_completed = True
    screen._is_new_best = True
    screen.seek(2500)
    matcher = screen._matcher
    matcher.process_detected_notes([heard(500)], 3000)
    matcher.finish()
    assert matcher.get_statistics()["hits"] == 1
    assert matcher.get_statistics()["total"] == 1
    assert not screen._song_completed
    assert not screen._is_new_best
    screen.seek(0)
    matcher.finish()
    assert matcher.get_statistics()["misses"] == 3


def test_loop_excludes_both_outside_boundaries_and_repeats_click(screen_factory):
    screen = screen_factory(notes=[event(0), event(1000), event(1500), event(2000)])
    screen.toggle_play()
    screen._set_loop_start(1000)
    screen._set_loop_end(2000)
    matcher = screen._matcher
    matcher.process_detected_notes([heard(0), heard(500)], 1500)
    matcher.finish()
    assert matcher.get_statistics()["total"] == 2
    assert matcher.get_statistics()["hits"] == 2
    frame_at(screen, 2000)
    assert screen._playback_ms == 1000
    assert matcher.get_statistics()["total"] == 0
    screen._midi_player.play_click.reset_mock()
    frame_at(screen, 1001)
    screen._midi_player.play_click.assert_called_once()


def test_final_short_note_becomes_miss_and_cannot_save_perfect_score(screen_factory):
    tracker = Mock()
    tracker.record_detailed_result.return_value = (True, [])
    screen = screen_factory(notes=[event(0), event(1000, duration=1)], tracker=tracker)
    screen.toggle_play()
    screen._matcher.process_detected_notes([heard(0)], 0)
    frame_at(screen, screen._end_ms + 1)
    stats = tracker.record_detailed_result.call_args.args[1]
    assert stats["total"] == 2
    assert stats["accuracy_percent"] == 50.0
    assert screen._matcher.get_note_state(screen._timeline.notes[-1]) == MatchType.MISS
    assert screen._song_completed
    screen.update()
    tracker.record_detailed_result.assert_called_once()


def test_last_late_detection_has_time_to_arrive(screen_factory):
    screen = screen_factory(notes=[event(1000, duration=1)])
    screen.toggle_play()
    screen._audio_capture.notes = [heard(1450)]
    frame_at(screen, 1650)
    assert screen._matcher.get_statistics()["late"] == 1
    assert screen.is_playing()
    frame_at(screen, screen._end_ms + 1)
    assert screen._song_completed
    assert screen._matcher.get_statistics()["accuracy_percent"] == 100.0


def test_low_b_last_note_uses_live_detector_delay_for_completion(screen_factory):
    tuning = {1: 43, 2: 38, 3: 33, 4: 28, 5: 23}
    timeline = Timeline([event(1000, midi=23, string=5, duration=1)],
                        SongMetadata(tuning=tuning))
    screen = screen_factory(timeline=timeline,
                            config=Config(count_in_beats=0, instrument="bass_guitar"))
    capture = Capture(screen._config, instrument=screen._instrument)
    capture.detector.expected_delay_ms = 700
    screen._audio_capture = capture
    screen.toggle_play()
    capture.notes = [heard(1490, midi=23)]
    frame_at(screen, 2190)
    assert screen.is_playing()
    assert screen._matcher.get_statistics()["late"] == 1
    frame_at(screen, screen._end_ms + 1)
    assert screen._song_completed
    assert screen._matcher.get_statistics()["accuracy_percent"] == 100


@pytest.mark.parametrize("notes", [[], [event(0)]])
def test_completion_works_without_tracker_and_restart_resets(screen_factory, notes):
    screen = screen_factory(notes=notes)
    screen.toggle_play()
    frame_at(screen, screen._end_ms + 1)
    assert screen._song_completed
    screen.toggle_play()
    assert screen.is_playing()
    assert not screen._song_completed
    assert screen._matcher.get_statistics()["total"] == 0


def test_partial_song_does_not_write_full_song_best(screen_factory):
    tracker = Mock()
    tracker.claim_section_advice.return_value = None
    screen = screen_factory(tracker=tracker)
    screen.toggle_play()
    screen.seek(2500)
    screen._audio_capture.notes = [heard(500)]
    frame_at(screen, 3000)
    frame_at(screen, screen._end_ms + 1)
    assert screen._matcher.get_statistics()["accuracy_percent"] == 100.0
    assert screen._song_completed
    tracker.record_detailed_result.assert_not_called()


@pytest.mark.parametrize("second_fret, saves", [(0, True), (2, False)])
def test_only_filters_omitting_playable_notes_disqualify_best(screen_factory, second_fret, saves):
    tracker = Mock()
    tracker.claim_section_advice.return_value = None
    tracker.record_detailed_result.return_value = (False, [])
    notes = [event(0), NoteEvent(1000, 100, 64, 1, second_fret)]
    screen = screen_factory(notes=notes, config=Config(count_in_beats=0, max_fret=1),
                            tracker=tracker)
    screen.toggle_play()
    frame_at(screen, screen._end_ms + 1)
    assert tracker.record_detailed_result.called is saves


def test_audio_gap_is_partial_and_skipped_notes_are_excluded(screen_factory):
    tracker = Mock()
    tracker.claim_section_advice.return_value = None
    screen = screen_factory(tracker=tracker)
    screen.toggle_play()
    screen._matcher.process_detected_notes([heard(0)], 0)
    screen._toggle_audio()
    frame_at(screen, 2500)
    screen._toggle_audio()
    screen._audio_capture.notes = [heard(500)]
    frame_at(screen, 3000)
    frame_at(screen, screen._end_ms + 1)
    assert screen._matcher.get_statistics()["total"] == 1
    assert screen._song_completed
    tracker.record_detailed_result.assert_not_called()


def test_audio_toggled_while_paused_keeps_assessment(screen_factory):
    screen = screen_factory()
    screen.toggle_play()
    screen._matcher.process_detected_notes([heard(0)], 0)
    screen._playback_ms = 700
    screen.toggle_play()
    screen._toggle_audio()
    screen._toggle_audio()
    screen.toggle_play()
    assert screen._matcher.get_statistics()["hits"] == 1
    assert screen._full_song_attempt


def test_disabling_frozen_wait_reanchors_capture_and_midi(screen_factory):
    screen = screen_factory()
    screen.toggle_play()
    screen._playback_ms = 1000
    screen._audio_capture.elapsed = 10000
    screen._wait_mode = screen._wait_mode_frozen = True
    screen._toggle_wait_mode()
    detection = heard(10000)
    screen._convert_timestamps([detection])
    assert detection.timestamp_ms + screen._matcher.audio_offset_ms == 1000
    assert not screen._wait_mode_frozen
    screen._midi_player.seek.assert_called_with(1000)


def test_mouse_wait_button_releases_clocks_and_saves_setting(screen_factory):
    import pygame

    screen = screen_factory(config=Config(count_in_beats=0, wait_mode=True))
    screen.toggle_play()
    screen._playback_ms = 1000
    screen._audio_capture.elapsed = 10000
    screen._wait_mode_frozen = True
    screen._button_rects = {"wait": [pygame.Rect(10, 10, 100, 30)]}
    screen._config.save = Mock()
    screen._handle_click((30, 20))
    assert not screen._wait_mode
    assert not screen._wait_mode_frozen
    assert not screen._config.wait_mode
    screen._config.save.assert_called_once()
    detection = heard(10000)
    screen._convert_timestamps([detection])
    assert detection.timestamp_ms + screen._matcher.audio_offset_ms == 1000
    screen._midi_player.seek.assert_called_with(1000)


def test_skipping_empty_intro_still_counts_as_full_song(screen_factory):
    tracker = Mock()
    tracker.record_detailed_result.return_value = (False, [])
    screen = screen_factory(notes=[event(10000)], tracker=tracker)
    screen.seek(2000)
    screen.toggle_play()
    frame_at(screen, screen._end_ms + 1)
    tracker.record_detailed_result.assert_called_once()


def test_wait_catches_note_crossed_by_slow_frame_then_resumes(screen_factory):
    screen = screen_factory(notes=[event(1000), event(3000)],
                            config=Config(count_in_beats=0, wait_mode=True))
    screen.toggle_play()
    frame_at(screen, 1800)
    assert screen._playback_ms == 1000
    assert screen._wait_mode_frozen
    screen._audio_capture.elapsed = 9000
    screen._audio_capture.notes = [heard(8800)]
    frame_at(screen, 1010)
    assert screen._matcher.get_statistics()["hits"] == 1
    frame_at(screen, 1010)
    assert not screen._wait_mode_frozen
    assert screen._audio_anchor_capture_ms == 9000
    assert screen._full_song_attempt


def test_count_in_opens_capture_once_even_with_pause(screen_factory):
    screen = screen_factory(config=Config(count_in_beats=4))
    screen.toggle_play()
    assert screen._audio_capture.starts == 1
    frame_at(screen, -500)
    screen.toggle_play()
    screen.toggle_play()
    frame_at(screen, 0)
    # Resume opens a fresh stream before the remaining count-in; crossing
    # zero must keep that same stream and assessment alive.
    assert screen._audio_capture.starts == 2
    matcher = screen._matcher
    for at in (20, 40, 60):
        frame_at(screen, at)
    assert screen._audio_capture.starts == 2
    assert screen._matcher is matcher


def test_count_in_waits_for_first_note_even_if_frame_crosses_zero(screen_factory):
    screen = screen_factory(config=Config(count_in_beats=4, wait_mode=True))
    screen.toggle_play()
    frame_at(screen, 50)
    assert screen._playback_ms == 0
    assert screen._wait_mode_frozen
    assert screen._matcher.get_statistics()["misses"] == 0


def test_slow_device_start_does_not_advance_song_or_shift_capture(screen_factory, monkeypatch):
    now = [100.0]
    monkeypatch.setattr("pickhero.ui.scrolling.time.perf_counter", lambda: now[0])
    original_start = Capture.start

    def slow_start(capture):
        original_start(capture)
        now[0] += 2.0
        capture.elapsed = 2000.0

    monkeypatch.setattr(Capture, "start", slow_start)
    screen = screen_factory()
    screen.toggle_play()
    now[0] += 0.1
    screen.update()
    assert screen._playback_ms == pytest.approx(100)
    detection = heard(2100)
    screen._convert_timestamps([detection])
    assert detection.timestamp_ms + screen._matcher.audio_offset_ms == pytest.approx(100)


@pytest.mark.parametrize("offset, verdict", [(-200, MatchType.EARLY), (200, MatchType.LATE)])
def test_chord_majority_accepts_correct_pitches_outside_timing_window(offset, verdict):
    notes = [event(1000, 64, 1), event(1000, 59, 2), event(1000, 55, 3)]
    matcher = NoteMatcher(Timeline(notes))
    matcher.process_detected_notes([heard(1000 + offset, 64), heard(1000 + offset, 59)],
                                   1000 + offset)
    matcher.finish()
    assert all(matcher.get_note_state(note) == verdict for note in notes)
    assert matcher.get_statistics()["hits"] == 3


def test_delayed_frame_consumes_queued_detection_before_retirement():
    note = event(1000)
    matcher = NoteMatcher(Timeline([note]))
    matcher.process_detected_notes([heard(1000)], 2500)
    assert matcher.get_note_state(note) == MatchType.HIT


def test_finish_is_idempotent_and_excludes_skipped_notes_and_boundary():
    notes = [event(0), event(1000), event(2000)]
    matcher = NoteMatcher(Timeline(notes))
    matcher.reset(1000, 2000)
    matcher.finish()
    matcher.finish()
    assert matcher.get_statistics()["misses"] == 1
    assert matcher.get_note_state(notes[0]) == MatchType.PENDING
    assert matcher.get_note_state(notes[2]) == MatchType.PENDING


def test_metronome_follows_imported_beats_across_tempo_change(screen_factory):
    timeline = Timeline([event(4000)], SongMetadata(tempo=120),
                        [MeasureInfo(0, 0, 2000), MeasureInfo(1, 2000, 6000)],
                        beats=[BeatInfo(0, 0, 0), BeatInfo(500, 0, 1),
                               BeatInfo(1000, 0, 2), BeatInfo(1500, 0, 3),
                               BeatInfo(2000, 1, 0), BeatInfo(3000, 1, 1),
                               BeatInfo(4000, 1, 2), BeatInfo(5000, 1, 3)],
                        tempo_changes=[(2000, 60)])
    screen = screen_factory(timeline=timeline)
    screen.toggle_play()
    frame_at(screen, 2000)
    screen._midi_player.play_click.assert_called_with(96)
    screen._midi_player.play_click.reset_mock()
    frame_at(screen, 2500)
    screen._midi_player.play_click.assert_not_called()
    frame_at(screen, 3000)
    screen._midi_player.play_click.assert_called_once_with(62)
    assert screen._beat_ms() == 1000


def test_grid_uses_tempo_map_instead_of_extending_initial_bpm(screen_factory, monkeypatch):
    import pygame

    timeline = Timeline([event(5000)], SongMetadata(tempo=120),
                        tempo_changes=[(2000, 60)])
    screen = screen_factory(timeline=timeline)
    lines = []
    monkeypatch.setattr(pygame.draw, "line", lambda surface, color, start, *args: lines.append(start[0]))
    layout = SimpleNamespace(lane_top=20, lane_bottom=100, pixels_per_ms=0.1,
                             hit_zone_x=0, gutter_x=0, screen_w=600)
    screen._draw_grid(pygame.Surface((600, 100)), layout)
    assert 200 in lines
    assert 300 in lines
    assert 250 not in lines


def test_saved_tempo_reports_slowest_speed_used(screen_factory):
    tracker = Mock()
    tracker.record_detailed_result.return_value = (False, [])
    screen = screen_factory(tracker=tracker)
    screen.toggle_play()
    screen.set_tempo_factor(0.5)
    screen.set_tempo_factor(1.0)
    frame_at(screen, screen._end_ms + 1)
    assert tracker.record_detailed_result.call_args.args[3] == 0.5


def test_wait_mode_pitch_progress_saves_without_claiming_measured_timing(screen_factory):
    tracker = Mock()
    tracker.record_detailed_result.return_value = (True, [])
    screen = screen_factory(config=Config(count_in_beats=0, wait_mode=True), tracker=tracker)
    screen.toggle_play()
    screen._matcher.process_detected_notes([heard(0), heard(1000), heard(3000)], 3000)
    # Turning wait off at the end must not reclassify an assisted attempt.
    screen._toggle_wait_mode()
    frame_at(screen, screen._end_ms + 1)
    stats = tracker.record_detailed_result.call_args.args[1]
    assert stats["accuracy_percent"] == 100
    assert stats["timing_percent"] == 100
    assert stats["timing_assessed"] is False
    screen.toggle_play()
    assert screen._timing_assessed is True


def test_enabling_wait_mid_attempt_marks_its_timing_unassessed(screen_factory):
    screen = screen_factory()
    screen.toggle_play()
    screen._toggle_wait_mode()
    screen._toggle_wait_mode()
    assert screen._timing_assessed is False


def test_weakest_section_loop_ends_at_final_weak_bar(screen_factory):
    timeline = Timeline([event(0), event(4000)], measures=[
        MeasureInfo(0, 0, 1000), MeasureInfo(1, 1000, 2000),
        MeasureInfo(2, 2000, 3000), MeasureInfo(3, 3000, 4500)])
    screen = screen_factory(timeline=timeline)
    screen._song_completed = True
    screen._weakest_sections = [(1, 2, 20.0)]
    screen._loop_weakest_section()
    assert screen._loop_start_ms == 1000
    assert screen._loop_end_ms == 3000


def test_short_loop_at_song_end_wraps_in_passive_mode(screen_factory):
    screen = screen_factory(notes=[event(1000)])
    screen._audio_enabled = False
    screen._set_loop_start(1050)
    screen._set_loop_end(1100)
    assert screen._loop_end_ms == 1100
    assert screen._loop_start_ms == 600
    screen.toggle_play()
    frame_at(screen, 1110)
    assert screen._playback_ms == 600
    assert not screen._song_completed


def test_enabling_capture_after_passive_completion_prepares_fresh_run(screen_factory):
    screen = screen_factory()
    screen._toggle_audio()
    screen.toggle_play()
    frame_at(screen, screen._end_ms + 1)
    assert screen._song_completed
    screen._toggle_audio()
    assert screen._playback_ms == 0
    assert screen._full_song_attempt
    screen.toggle_play()
    assert screen.is_playing()
    assert screen._matcher.get_statistics()["total"] == 0
