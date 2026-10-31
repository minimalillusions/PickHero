"""Small scale preparations use the same timeline, scoring and resume path."""
from pathlib import Path

import pygame
import pytest

from pickhero.audio.midi_playback import NOTE_ON
from pickhero.config import Config
from pickhero.curriculum import (LESSONS, LessonPractice, build_demo, exercise_for_lesson,
                                 practice_state, practice_steps)
from pickhero.exercises import EXERCISE_DIRNAME, write_exercises
from pickhero.import_worker import encode_song
from pickhero.instruments import PROFILES, BASS_UKULELE
from pickhero.library import content_identity
from pickhero.tabs.loader import load_gp_file
from pickhero.ui.app import App
from pickhero.ui.scrolling import PlayingScreen


def scale_path(instrument, directory):
    return write_exercises(instrument, directory / EXERCISE_DIRNAME)[7]


@pytest.mark.parametrize("instrument", list(PROFILES.values()), ids=lambda i: i.key)
def test_each_preparation_demo_matches_its_played_bars_and_speed(tmp_path, instrument):
    lesson = LESSONS[7]
    path = scale_path(instrument, tmp_path)
    timeline = load_gp_file(path, instrument=instrument)
    exercise = exercise_for_lesson(lesson, instrument)
    for step in practice_steps(lesson, instrument):
        state = practice_state(LessonPractice(path, step), timeline, instrument)
        start = timeline.measures[step.first_bar].start_ms
        end = timeline.measures[step.stop_bar - 1].end_ms
        notes = [n for n in timeline.notes if start <= n.timestamp_ms < end]
        demo = build_demo(exercise, instrument, step.stop_bar - step.first_bar,
                          first_bar=step.first_bar, tempo_factor=step.tempo_factor)
        played = [e for e in demo.track.events if e.channel == 0 and e.event_type == NOTE_ON]
        assert [e.data1 for e in played] == [n.midi_note for n in notes]
        for event, note in zip(played, notes):
            assert event.timestamp_ms == pytest.approx(
                demo.count_in_ms + (note.timestamp_ms - start) / step.tempo_factor)
        assert state["position_ms"] == start
        assert state["loop_end_ms"] == (end if step.loop else None)
        assert state["wait_mode"] == step.wait_mode
        assert demo.duration_ms == pytest.approx(demo.count_in_ms + (end - start) / step.tempo_factor)


@pytest.mark.parametrize("lesson", LESSONS, ids=lambda lesson: lesson.slug)
@pytest.mark.parametrize("instrument", list(PROFILES.values()), ids=lambda instrument: instrument.key)
def test_practice_enters_paused_and_does_not_claim_full_goal_for_fragments(
        tmp_path, monkeypatch, lesson, instrument):
    path = write_exercises(instrument, tmp_path / EXERCISE_DIRNAME)[lesson.order - 1]
    monkeypatch.setattr(PlayingScreen, "_init_midi_player", lambda self, track: None)
    monkeypatch.setattr(PlayingScreen, "_start_capture_only", lambda self: None)
    config = Config(instrument=instrument.key, songs_dir=str(tmp_path))
    app = App(config)
    for step in practice_steps(lesson, instrument):
        config.max_fret = 1
        config.active_strings = [True] + [False] * (instrument.string_count - 1)
        request = LessonPractice(path, step)
        app._load_song(path, track_index=0, practice=request)
        screen = app._playing_screen
        assert app._state == "playing" and screen is not None
        assert not screen._playing and screen._resume_count_pending
        assert screen._tempo_factor == step.tempo_factor
        assert screen._loop_enabled == step.loop
        assert screen._wait_mode == step.wait_mode
        assert screen._full_song_attempt is (not step.loop)
        assert all(screen._active_strings) and screen._max_fret >= 4
        assert screen._unplayable_warning() is None
        assert screen._playback_ms == screen._timeline.measures[step.first_bar].start_ms
        stored = app._progress.get_session(screen._song_key)
        assert stored["tempo_factor"] == step.tempo_factor
        assert not app._progress.get_best(screen._song_key).goal_pitch_met
        assert not app._progress.get_best(screen._song_key).goal_rhythm_met
        app._handle_playing_event(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_ESCAPE))
        assert app._state == "lessons"
        assert app._lesson_screen.show_practice(request)


class FakeJob:
    def __init__(self, operation, path, instrument, **options):
        self.operation, self.path, self.options = operation, path, options
        self.status, self.error, self.result = {}, None, None
        self.cancelled = False

    def poll(self):
        return self.result is not None

    def cancel(self):
        self.cancelled = True


def test_background_import_carries_selected_step_until_score_is_ready(tmp_path, monkeypatch):
    instrument = BASS_UKULELE
    path = scale_path(instrument, tmp_path)
    timeline = load_gp_file(path, instrument=instrument)
    monkeypatch.setattr("pickhero.ui.app.ImportJob", FakeJob)
    monkeypatch.setattr(PlayingScreen, "_init_midi_player", lambda self, track: None)
    monkeypatch.setattr(PlayingScreen, "_start_capture_only", lambda self: None)
    app = App(Config(instrument=instrument.key), background=True)
    request = LessonPractice(path, practice_steps(LESSONS[7], instrument)[3])
    app._load_song(path, track_index=0, practice=request)
    job = app._import_job
    assert job.operation == "load" and job.options["track_index"] == 0
    app._poll_import()
    assert app._playing_screen is None
    job.result = {"song": encode_song(timeline), "content_id": content_identity(path)}
    app._poll_import()
    screen = app._playing_screen
    assert screen._loop_start_ms == timeline.measures[2].start_ms
    assert screen._loop_end_ms == timeline.measures[3].end_ms
    assert screen._tempo_factor == .75 and not screen._wait_mode
    assert app._lesson_practice is None


def test_cancel_discards_preparation_and_retry_retains_it(tmp_path, monkeypatch):
    monkeypatch.setattr("pickhero.ui.app.ImportJob", FakeJob)
    path = scale_path(BASS_UKULELE, tmp_path)
    request = LessonPractice(path, practice_steps(LESSONS[7], BASS_UKULELE)[0])
    app = App(Config(instrument="bass_ukulele"), background=True)
    app._load_song(path, track_index=0, practice=request)
    first = app._import_job
    app._handle_import_event(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_ESCAPE))
    assert first.cancelled and app._lesson_practice is None
    app._load_song(path, track_index=0, practice=request)
    app._load_error = (path, False)
    app._handle_load_error(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_r))
    assert app._lesson_practice == request
    assert app._import_job.options["track_index"] == 0


def test_preparation_cannot_be_applied_to_unrelated_score(tmp_path):
    step = practice_steps(LESSONS[7], BASS_UKULELE)[0]
    path = scale_path(BASS_UKULELE, tmp_path)
    timeline = load_gp_file(path, instrument=BASS_UKULELE)
    with pytest.raises(ValueError):
        practice_state(LessonPractice(Path("different.gp5"), step), timeline, BASS_UKULELE)


def test_escape_returns_to_selected_step_and_refreshes_learning_plan(tmp_path, monkeypatch):
    from unittest.mock import Mock
    pygame.font.init()
    path = scale_path(BASS_UKULELE, tmp_path)
    request = LessonPractice(path, practice_steps(LESSONS[7], BASS_UKULELE)[1])
    monkeypatch.setattr(PlayingScreen, "_init_midi_player", lambda self, track: None)
    monkeypatch.setattr(PlayingScreen, "_start_capture_only", lambda self: None)
    app = App(Config(instrument="bass_ukulele", songs_dir=str(tmp_path)))
    app._load_song(path, track_index=0, practice=request)
    previous = app._playing_screen
    previous.handle_event = Mock(return_value="menu")
    app._handle_playing_event(pygame.event.Event(pygame.KEYDOWN, key=pygame.K_ESCAPE))
    assert app._state == "lessons" and app._playing_screen is None
    assert app._lesson_screen._mode == "steps" and app._lesson_screen._step_index == 1
    assert app._lesson_screen._selected == 7
    # A later ordinary song must not send its Exit to an unrelated lesson.
    app._load_song(path)
    assert app._lesson_return is None
