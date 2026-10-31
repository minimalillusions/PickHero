"""Practice survives restarts without turning a short loop into a song record."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pygame
import pytest

from pickhero.progress import ProgressTracker, SongRecord, song_key, validate_session
from pickhero.recommendations import section_tempo_suggestion
from pickhero.ui.colors import set_look
from tests import test_timing_clock

clock_screen = test_timing_clock.clock_screen
heard = test_timing_clock.heard


def context(**overrides):
    return dict({"start_ms": 1000, "end_ms": 5000, "tempo_factor": .5,
                 "filter_key": "all strings", "uniform_tempo": True, "complete_loop": True},
                **overrides)


def stats(**overrides):
    return dict({"accuracy_percent": 95, "timing_percent": 92, "total": 8,
                 "timing_assessed": True, "reliable": True, "hits": 8}, **overrides)


def session(**overrides):
    return dict({"song_path": str(Path("song.gp5").resolve()), "instrument": "bass_ukulele",
                 "track_index": 1, "position_ms": 2500, "tempo_factor": .55,
                 "measure": 1, "loop_start_ms": 1000, "loop_end_ms": 4000,
                 "loop_enabled": True, "wait_mode": True, "max_fret": 7,
                 "active_strings": [True, False, True, True], "chord_partial_credit": False},
                **overrides)


def test_three_loops_survive_reload_and_do_not_change_full_song_record():
    tracker = ProgressTracker()
    tracker.record_result("song", stats(accuracy_percent=60))
    assert tracker.record_section_result("song", stats(), context()) is None
    tracker = ProgressTracker()
    assert tracker.record_section_result("song", stats(), context()) is None
    tracker = ProgressTracker()
    assert tracker.record_section_result("song", stats(), context()) == .55
    tracker = ProgressTracker()
    record = tracker.get_best("song")
    assert record.attempts == 1
    assert record.best_accuracy == 60
    assert len(record.section_attempts) == 3
    assert record.section_attempts[-1]["timing_percent"] == 92


@pytest.mark.parametrize("change,kind", [
    ({"accuracy_percent": 89}, "stats"), ({"timing_percent": 89}, "stats"),
    ({"timing_assessed": False}, "stats"), ({"reliable": False}, "stats"),
    ({"total": 3}, "stats"), ({"tempo_factor": .55}, "context"),
    ({"start_ms": 1100}, "context"), ({"end_ms": 5100}, "context"),
    ({"filter_key": "one string"}, "context"), ({"uniform_tempo": False}, "context"),
    ({"complete_loop": False}, "context"),
])
def test_unstable_or_incomparable_loop_interrupts_progression(change, kind):
    tracker = ProgressTracker()
    tracker.record_section_result("song", stats(), context())
    tracker.record_section_result("song", stats(**change) if kind == "stats" else stats(),
                                  context(**change) if kind == "context" else context())
    assert tracker.record_section_result("song", stats(), context()) is None


def test_unreliable_whole_song_cannot_change_best_or_attempts():
    tracker = ProgressTracker()
    assert tracker.record_detailed_result("song", stats(), [], .5)[0]
    assert tracker.record_detailed_result("song", stats(accuracy_percent=100, reliable=False), [], 1) == (False, [])
    assert ProgressTracker().get_best("song").attempts == 1
    assert tracker.get_best("song").best_accuracy == 95


def test_histories_are_bounded_and_unknown_legacy_quality_is_not_assumed():
    tracker = ProgressTracker()
    for _ in range(106):
        tracker.record_section_result("song", stats(reliable=False), context())
    assert len(ProgressTracker().get_best("song").section_attempts) == 100
    entries = tracker.get_best("song").section_attempts[-3:]
    for item in entries:
        item.pop("reliable")
    assert section_tempo_suggestion(entries) is None


def test_session_roundtrip_and_latest_are_instrument_and_track_specific():
    tracker = ProgressTracker()
    tracker.save_session("one", session())
    tracker.save_session("two", session(instrument="guitar", track_index=2))
    fresh = ProgressTracker()
    assert fresh.latest_session("bass_ukulele")["song_key"] == "one"
    assert fresh.latest_session()["song_key"] == "two"
    state = fresh.get_session("one")
    assert state["position_ms"] == 2500 and state["track_index"] == 1
    state["active_strings"][0] = False
    assert fresh.get_session("one")["active_strings"][0] is True
    assert fresh.get_best("one").attempts == 0


@pytest.mark.parametrize("field,value", [("instrument", []), ("position_ms", float("nan")),
                                        ("position_ms", -1), ("track_index", True),
                                        ("song_path", {})])
def test_malformed_resume_is_rejected_without_affecting_other_data(field, value):
    assert validate_session(session(**{field: value})) == {}


def test_malformed_optional_session_fields_are_ignored():
    state = validate_session(session(tempo_factor=float("inf"), active_strings=[True, 1],
                                     loop_enabled="true", max_fret=200))
    assert not {"tempo_factor", "active_strings", "loop_enabled", "max_fret"} & state.keys()


def test_content_identity_migration_preserves_history_and_existing_target():
    tracker = ProgressTracker()
    old = song_key("a.gp5", "guitar", 1)
    new = song_key("elsewhere.gp5", "guitar", 1, content_id="a" * 64)
    tracker.record_result(old, stats())
    tracker.migrate_content_identity("a.gp5", "guitar", 1, "a" * 64)
    assert tracker.get_best(new).best_accuracy == 95
    tracker.record_result(new, stats(accuracy_percent=100))
    tracker.migrate_content_identity("a.gp5", "guitar", 1, "a" * 64)
    assert tracker.get_best(new).attempts == 2
    assert tracker.get_best(old).attempts == 1
    assert song_key("b.gp5", "guitar", 0, content_id="a" * 64) != new


def test_moved_content_updates_resume_path_without_fabricating_new_practice():
    tracker = ProgressTracker()
    old = song_key("old.gp5", "bass_ukulele", 1)
    new = song_key("old.gp5", "bass_ukulele", 1, content_id="a" * 64)
    tracker.save_session(old, session(song_path=str(Path("old.gp5").resolve())))
    tracker.migrate_content_identity("old.gp5", "bass_ukulele", 1, "a" * 64)
    tracker.migrate_content_identity("moved.gp5", "bass_ukulele", 1, "a" * 64)
    latest = ProgressTracker().latest_session("bass_ukulele")
    assert latest["song_key"] == new
    assert latest["song_path"] == str(Path("moved.gp5").resolve())
    assert latest["position_ms"] == 2500
    assert latest["saved_order"] == 1


def test_legacy_path_history_is_bound_once_and_cannot_follow_unrelated_replacement():
    tracker = ProgressTracker()
    old = song_key("same.gp5", "guitar", 0)
    first = song_key("same.gp5", "guitar", 0, content_id="a" * 64)
    other = song_key("same.gp5", "guitar", 0, content_id="b" * 64)
    tracker.record_result(old, stats())
    tracker.migrate_content_identity("same.gp5", "guitar", 0, "a" * 64)
    tracker = ProgressTracker()
    tracker.migrate_content_identity("same.gp5", "guitar", 0, "b" * 64)
    assert tracker.get_best(first).best_accuracy == 95
    assert tracker.get_best(other) is None
    assert tracker.get_best(old).best_accuracy == 95
    assert tracker.get_best(old).migrated_content_id == "a" * 64


def test_thousand_library_migrations_write_once_and_preserve_every_record(tmp_path, monkeypatch):
    tracker = ProgressTracker()
    songs = []
    for index in range(1000):
        path = tmp_path / f"song-{index}.gp5"
        song = SimpleNamespace(file=path, track_index=index % 4, content_id=f"{index + 1:064x}")
        songs.append(song)
        old = song_key(path, "bass_ukulele", song.track_index)
        tracker._data[old] = SongRecord(best_accuracy=index % 101, attempts=index + 1,
                                      practice_state=session(song_path=str(path), track_index=song.track_index))
    save = Mock(wraps=tracker._save)
    monkeypatch.setattr(tracker, "_save", save)
    tracker.migrate_content_identities(songs, "bass_ukulele")
    save.assert_called_once_with()
    restored = ProgressTracker()
    for index, song in enumerate(songs):
        new = song_key(song.file, "bass_ukulele", song.track_index, content_id=song.content_id)
        record = restored.get_best(new)
        assert record.best_accuracy == index % 101
        assert record.attempts == index + 1
        assert record.practice_state["song_path"] == str(song.file)
        assert record.practice_state["position_ms"] == 2500
    save.reset_mock()
    tracker.migrate_content_identities(songs, "bass_ukulele")
    save.assert_not_called()

    # One moved score, one unrelated replacement, and 998 unchanged scores.
    moved = songs[0]
    songs[0] = SimpleNamespace(file=tmp_path / "moved.gp5", track_index=moved.track_index,
                               content_id=moved.content_id)
    replaced = songs[1]
    songs[1] = SimpleNamespace(file=replaced.file, track_index=replaced.track_index, content_id="f" * 64)
    tracker.migrate_content_identities(songs, "bass_ukulele")
    save.assert_called_once_with()
    moved_key = song_key(songs[0].file, "bass_ukulele", songs[0].track_index, content_id=songs[0].content_id)
    assert ProgressTracker().get_session(moved_key)["song_path"] == str(songs[0].file)
    assert tracker.get_best(song_key(replaced.file, "bass_ukulele", replaced.track_index,
                                     content_id="f" * 64)) is None


def test_verified_lesson_rewrite_retains_only_matching_instrument_and_tracks():
    tracker = ProgressTracker()
    old = song_key("a", "bass", 2, content_id="a" * 64)
    target = song_key("a", "bass", 2, content_id="b" * 64)
    guitar = song_key("a", "guitar", 2, content_id="a" * 64)
    tracker.record_result(old, stats())
    tracker.record_result(guitar, stats())
    tracker.migrate_content_replacement("a" * 64, "b" * 64, "bass")
    assert tracker.get_best(target).best_accuracy == 95
    assert tracker.get_best(song_key("a", "guitar", 2, content_id="b" * 64)) is None


def test_earned_learning_goals_survive_bounded_history_and_language_roundtrip():
    tracker = ProgressTracker()
    german = song_key("a", "bass", 0, content_id="a" * 64)
    english = song_key("a", "bass", 0, content_id="b" * 64)
    tracker.record_detailed_result(german, stats(accuracy_percent=85, timing_percent=20), [], 1)
    tracker.migrate_content_replacement("a" * 64, "b" * 64, "bass")
    tracker.record_detailed_result(english, stats(), [], 1)
    for _ in range(51):
        tracker.record_detailed_result(english, stats(accuracy_percent=10, timing_percent=0), [], 1)
    tracker.migrate_content_replacement("b" * 64, "a" * 64, "bass")
    record = ProgressTracker().get_best(german)
    assert record.goal_pitch_met and record.goal_rhythm_met
    assert record.best_accuracy == 95
    assert record.attempts == 53
    assert len(record.tempo_history) == 50
    assert all(entry["accuracy"] == 10 for entry in record.tempo_history)


def attach(screen):
    screen._song_key = "practice-test"
    screen._song_path = str(Path("practice.gp5").resolve())
    screen._progress_tracker = ProgressTracker()


def test_resume_restores_choices_and_counts_in_without_starting_capture(clock_screen):
    _, screen, _ = clock_screen()
    attach(screen)
    screen._track_index = 1
    assert screen.restore_practice_state(session())
    assert not screen.is_playing()
    assert screen._audio_capture is None
    assert screen._playback_ms == 2500
    assert screen._tempo_factor == .55
    assert screen._wait_mode and screen._loop_enabled
    assert screen._active_strings == [True, False, True, True]
    assert screen._max_fret == 7
    screen.toggle_play()
    assert screen.is_playing()
    assert screen._count_in_target_ms == 2500
    assert screen._playback_ms < 2500
    assert screen._assessment_start_ms == 2500
    assert not screen._full_song_attempt


def test_completed_session_reopens_at_start_and_other_track_does_not_restore(clock_screen):
    _, screen, _ = clock_screen()
    assert not screen.restore_practice_state(session(track_index=2))
    assert screen.restore_practice_state(session(track_index=0, completed=True, loop_enabled=False))
    assert screen._playback_ms == 0
    assert not screen._song_completed


@pytest.mark.parametrize("start,end", [(5000, 1000), (0, 80_000_000), (80_000_000, 90_000)])
def test_invalid_resume_loop_cannot_leave_unusable_markers(clock_screen, start, end):
    _, screen, _ = clock_screen()
    assert screen.restore_practice_state(session(track_index=0, loop_start_ms=start, loop_end_ms=end))
    assert not screen._loop_enabled
    screen._toggle_loop()
    assert screen._loop_enabled
    assert 0 <= screen._loop_start_ms < screen._loop_end_ms <= screen._timeline.duration_ms


def test_stop_saves_actual_position_and_tempo_change_is_written_immediately(clock_screen, monkeypatch):
    _, screen, _ = clock_screen()
    attach(screen)
    saved = []
    monkeypatch.setattr(screen._config, "save", lambda: saved.append(screen._config.tempo_factor))
    screen.set_tempo_factor(.65)
    assert saved == [.65]
    assert ProgressTracker().get_session(screen._song_key)["tempo_factor"] == .65
    screen.seek(3400)
    screen._midi_player.close = lambda: None
    screen.stop_audio()
    assert ProgressTracker().get_session(screen._song_key)["position_ms"] == 3400


@pytest.mark.parametrize("position", [0, 4000])
def test_paused_unstarted_passage_uses_new_tempo_for_entire_attempt(clock_screen, position):
    _, screen, _ = clock_screen(tempo=.5)
    screen.seek(position)
    screen.set_tempo_factor(.6)
    assert screen._attempt_tempo_factor == .6
    assert screen._attempt_uniform_tempo


def test_periodic_checkpoint_is_bounded_and_countin_saves_target(clock_screen, monkeypatch):
    clock, screen, _ = clock_screen()
    attach(screen)
    screen._start_practice(3000, force_count_in=True)
    screen.save_practice_state()
    assert ProgressTracker().get_session(screen._song_key)["position_ms"] == 3000
    writes = []
    original = screen._progress_tracker.save_session
    monkeypatch.setattr(screen._progress_tracker, "save_session", lambda *args: (writes.append(args), original(*args)))
    for _ in range(61):
        clock.now += .1
        screen.update()
    assert len(writes) == 1


def test_input_issue_is_latched_only_during_assessment_and_clears_on_new_attempt(clock_screen):
    clock, screen, _ = clock_screen()
    screen.toggle_play()
    screen._audio_capture.health_issue = lambda: "input_clipping"
    screen.update()
    assert screen._attempt_issues == []
    clock.now += 2.1
    screen.update()
    screen.update()
    assert screen._attempt_issues == ["input_clipping"]
    screen._audio_capture.health_issue = lambda: None
    screen.update()
    assert screen._attempt_issues == ["input_clipping"]
    screen.seek(0)
    assert screen._attempt_issues == []


def test_input_fault_prevents_best_and_is_visible_in_completed_result(clock_screen):
    clock, screen, _ = clock_screen()
    attach(screen)
    screen._count_in_ms = 0
    screen.toggle_play()
    screen.mark_attempt_unreliable("input_overflow")
    screen._playback_ms = screen._end_ms
    screen._last_tick = clock.now
    screen.update()
    assert screen._song_completed and not screen._is_new_best
    record = screen._progress_tracker.get_best(screen._song_key)
    assert record.attempts == 0
    assert record.section_attempts[-1]["reliable"] is False
    assert screen._result_panel().sections


def test_health_boundary_tracks_countin_tempo_and_stays_fixed_after_start(clock_screen, monkeypatch):
    from pickhero.audio import input as audio_input
    windows = []
    monkeypatch.setattr(audio_input.AudioCapture, "set_health_window",
                        lambda self, boundary: windows.append(boundary), raising=False)
    clock, screen, _ = clock_screen()
    screen.toggle_play()
    assert windows[-1] == pytest.approx(screen._audio_anchor_capture_ms + 2000)
    clock.now += .5
    screen.update()
    screen.set_tempo_factor(.5)
    assert windows[-1] == pytest.approx(screen._audio_anchor_capture_ms + 3000)
    clock.now += 3.1
    screen.update()
    previous = list(windows)
    screen.set_tempo_factor(.75)
    assert windows == previous


def test_filter_starts_a_new_health_window_without_reusing_previous_fault(clock_screen, monkeypatch):
    from pickhero.audio import input as audio_input
    windows = []
    monkeypatch.setattr(audio_input.AudioCapture, "set_health_window",
                        lambda self, boundary: windows.append(boundary), raising=False)
    clock, screen, _ = clock_screen()
    screen._count_in_ms = 0
    screen.toggle_play()
    clock.now += 1
    screen.update()
    screen.mark_attempt_unreliable("input_clipping")
    screen._toggle_string(1)
    assert screen._attempt_issues == []
    assert windows[-1] == pytest.approx(screen._audio_capture.capture_elapsed_ms())


def test_saved_passage_history_is_accessible_without_losing_paused_score(clock_screen):
    clock, screen, _ = clock_screen()
    attach(screen)
    screen._progress_tracker.record_section_result(screen._song_key, stats(), context())
    screen.toggle_play()
    clock.now += 2.1
    screen.update()
    screen._open_help()
    position = screen._playback_ms
    matcher = screen._matcher
    screen._panel_action("history")
    assert screen._panel.kind == "history" and len(screen._panel.sections[0][1]) == 1
    assert not screen.is_playing()
    screen._panel_action("close")
    assert screen._panel.kind == "help"
    screen._panel_action("close")
    assert screen.is_playing()
    assert screen._matcher is matcher
    assert screen._playback_ms == position


@pytest.mark.parametrize("action,expected", [("accept_tempo", .55), ("keep_tempo", .5), ("close", .5)])
def test_third_stable_loop_pauses_for_explicit_tempo_choice(clock_screen, action, expected):
    clock, screen, _ = clock_screen(tempo=.5)
    attach(screen)
    screen._loop_start_ms, screen._loop_end_ms, screen._loop_enabled = 0, 2000, True
    screen._count_in_ms = 0
    screen.toggle_play()
    for repetition in range(3):
        screen._begin_attempt(0)
        screen._playback_ms = 0
        screen._reanchor_audio()
        matcher = screen._matcher
        for note in screen._timeline.notes[:4]:
            matcher.process_detected_notes([heard(note.timestamp_ms - matcher.audio_offset_ms, note.midi_note)], note.timestamp_ms)
        screen._playback_ms = screen._loop_finish_ms()
        screen._last_tick = clock.now
        screen.update()
        if repetition < 2:
            assert screen._panel is None
    record = ProgressTracker().get_best(screen._song_key)
    assert record.attempts == 0 and record.best_accuracy == 0
    assert len(record.section_attempts) == 3
    assert screen._panel.kind == "loop_progress"
    assert not screen.is_playing()
    assert screen._tempo_factor == .5
    screen._panel_action(action)
    assert screen._tempo_factor == expected
    assert screen.is_playing() and screen._playback_ms < screen._count_in_target_ms


def test_late_report_near_loop_boundary_is_received_before_assessment_reset(clock_screen):
    clock, screen, _ = clock_screen()
    attach(screen)
    screen._loop_start_ms, screen._loop_end_ms, screen._loop_enabled = 0, 1550, True
    screen._count_in_ms = 0
    screen.toggle_play()
    capture = screen._audio_capture
    anchor = screen._audio_anchor_capture_ms
    clock.now += 1.56
    screen.update()
    assert not screen._progress_tracker.get_best(screen._song_key).section_attempts
    capture.notes.append(heard(anchor + 1500, 43))
    clock.now += .3
    screen.update()
    clock.now += .5
    screen.update()
    recorded = screen._progress_tracker.get_best(screen._song_key).section_attempts[-1]
    assert recorded["accuracy"] == 25
    assert recorded["timing_percent"] == 100
    assert screen._playback_ms < screen._count_in_target_ms


def test_late_pitch_at_loop_end_receives_same_grace_as_full_song(clock_screen):
    clock, screen, _ = clock_screen()
    attach(screen)
    screen._loop_start_ms, screen._loop_end_ms, screen._loop_enabled = 0, 1550, True
    screen._count_in_ms = 0
    screen.toggle_play()
    capture = screen._audio_capture
    anchor = screen._audio_anchor_capture_ms
    clock.now += 1.89
    screen.update()
    assert not screen._progress_tracker.get_best(screen._song_key).section_attempts
    capture.notes.append(heard(anchor + 1700, 43))
    clock.now += .02
    screen.update()
    recorded = screen._progress_tracker.get_best(screen._song_key).section_attempts[-1]
    assert recorded["accuracy"] == 25
    assert recorded["timing_percent"] == 0


def test_chord_credit_change_resets_comparable_passage_bounds(clock_screen):
    _, screen, _ = clock_screen()
    screen._loop_start_ms, screen._loop_end_ms, screen._loop_enabled = 0, 4000, True
    screen._count_in_ms = 0
    screen.toggle_play()
    screen._playback_ms = 1000
    screen._toggle_chord_mode()
    assert screen._assessment_start_ms == 1000
    assert screen._assessment_start_ms != screen._loop_start_ms


@pytest.mark.parametrize("look", ["studio", "stage", "hifi"])
@pytest.mark.parametrize("size", [(320, 240), (640, 480), (1280, 720)])
def test_loop_tempo_panel_all_actions_fit_all_looks(clock_screen, look, size):
    from pickhero.ui.practice_panels import PracticePanel, Action
    pygame.font.init()
    set_look(look)
    try:
        panel = PracticePanel("loop_progress", "Drei stabile Wiederholungen", "Übung",
                              [("", ["Tonhöhe und Timing waren dreimal sicher. " * 5])],
                              [Action("accept", "Tempo erhöhen"), Action("keep", "Tempo beibehalten"), Action("menu", "Bibliothek")])
        surface = pygame.Surface(size)
        panel.render(surface)
        for action in panel.actions:
            assert surface.get_rect().contains(panel.hit[action.name])
            assert not panel.body.colliderect(panel.hit[action.name])
    finally:
        set_look("studio")
