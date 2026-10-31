"""Music choices must play what they preview, preserve history and lead on."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pygame
import pytest
import guitarpro

from pickhero import i18n
from pickhero.audio.midi_playback import NOTE_ON
from pickhero.config import Config
from pickhero.curriculum import next_lesson_practice, practice_state
from pickhero.instruments import BASS_UKULELE, GUITAR, PROFILES
from pickhero.library import content_identity
from pickhero.music import (MUSIC_DIRNAME, PIECES, VERSIONS, build_music, build_music_demo,
                            ensure_music, music_for_path, music_practice)
from pickhero.progress import ProgressTracker, SongRecord, song_key
from pickhero.tabs.loader import extract_backing_track, list_tracks, load_gp_file
from pickhero.ui.app import App
from pickhero.ui.colors import get_look, set_look
from pickhero.ui.lesson_menu import LessonScreen
from pickhero.ui.music_menu import MusicScreen
from pickhero.ui.scrolling import PlayingScreen


def key(code):
    return pygame.event.Event(pygame.KEYDOWN, key=code, mod=0, unicode="")


@pytest.fixture(autouse=True)
def display_state(monkeypatch):
    pygame.font.init()
    language, look = i18n.current_language(), get_look().key
    i18n.set_language("de")
    set_look("studio")
    monkeypatch.setattr(PlayingScreen, "_init_midi_player", lambda *a: None)
    monkeypatch.setattr(PlayingScreen, "_start_capture_only", lambda *a: None)
    monkeypatch.setattr(PlayingScreen, "_start_audio", lambda *a: None)
    yield
    i18n.set_language(language)
    set_look(look)


@pytest.mark.parametrize("instrument", list(PROFILES.values()), ids=lambda p: p.key)
@pytest.mark.parametrize("piece", PIECES, ids=lambda p: p.slug)
@pytest.mark.parametrize("version", VERSIONS)
def test_written_score_preview_and_drums_agree(tmp_path, instrument, piece, version):
    ensure_music(instrument, tmp_path)
    path = tmp_path/MUSIC_DIRNAME/piece.filename(version)
    exercise = build_music(piece, version, instrument)
    timeline = load_gp_file(path, track_index=0, instrument=instrument)
    expected = [step for bar in exercise.bars for step in bar if step.string is not None]
    assert [n.midi_note for n in timeline.notes] == [instrument.tuning[s.string]+s.fret for s in expected]
    assert len(timeline.measures) == len(exercise.bars)
    start = timeline.measures[0].start_ms
    assert timeline.duration_ms-start == pytest.approx(len(exercise.bars)*240000/piece.bpm)
    assert all(sum(4/s.value for s in bar) == 4 for bar in exercise.bars)
    assert max(s.fret for s in expected) <= 4
    tracks = list_tracks(path)
    assert len(tracks) == 2 and not tracks[0]['is_percussion'] and tracks[1]['is_percussion']
    assert tracks[0]['tuning'] == instrument.tuning
    drums = extract_backing_track(path, exclude_track_indices={0})
    hits = [e for e in drums.events if e.event_type == NOTE_ON]
    assert hits and {e.channel for e in hits} == {9}
    assert {e.data1 for e in hits} == {36, 38, 42}
    assert all(0 <= e.timestamp_ms < timeline.duration_ms for e in hits)
    demo = build_music_demo(piece, version, instrument)
    played = [e for e in demo.track.events if e.event_type == NOTE_ON and e.channel == 0]
    written = [n for n in timeline.notes if n.timestamp_ms < timeline.measures[4].start_ms]
    assert [e.data1 for e in played] == [n.midi_note for n in written]
    assert [e.timestamp_ms-demo.count_in_ms for e in played] == pytest.approx([n.timestamp_ms-start for n in written])
    assert demo.duration_ms == pytest.approx(demo.count_in_ms+4*240000/piece.bpm)
    assert sum(e.event_type == NOTE_ON and e.data1 == 76 for e in demo.track.events) == 4
    state = practice_state(music_practice(path, instrument), timeline, instrument)
    assert not state['loop_enabled'] and state['tempo_factor'] == 1 and not state['wait_mode']
    assert all(state['active_strings'])


def test_versions_are_distinct_and_night_drive_begins_with_one_string(tmp_path):
    ensure_music(BASS_UKULELE, tmp_path)
    identities = {content_identity(tmp_path/MUSIC_DIRNAME/p.filename(v)) for p in PIECES for v in VERSIONS}
    assert len(identities) == len(PIECES) * len(VERSIONS)
    notes = [s for bar in build_music(PIECES[0], 'start', BASS_UKULELE).bars for s in bar if s.string]
    assert {s.string for s in notes} == {4} and {s.fret for s in notes} == {0, 3}


@pytest.mark.parametrize('version', VERSIONS)
def test_evening_walk_is_a_playable_repeated_riff_with_a_quiet_answer(tmp_path, version):
    """The written file must contain the promised musical learning tasks."""
    piece = next(p for p in PIECES if p.slug == 'evening_walk')
    ensure_music(BASS_UKULELE, tmp_path)
    path = tmp_path/MUSIC_DIRNAME/piece.filename(version)
    timeline = load_gp_file(path, track_index=0, instrument=BASS_UKULELE)
    assert len(timeline.measures) == 12
    assert timeline.duration_ms-timeline.measures[0].start_ms == pytest.approx(36000)
    assert {n.string for n in timeline.notes} == {3, 4}
    assert {n.fret for n in timeline.notes} == {0, 2, 3}
    assert {n.midi_note for n in timeline.notes} == {28, 31, 33, 35}
    assert min(b.timestamp_ms-a.timestamp_ms for a,b in zip(timeline.notes, timeline.notes[1:])) == (
        750 if version == 'start' else 375)
    signature = lambda notes: [(n.timestamp_ms-notes[0].timestamp_ms, n.duration_ms, n.string, n.fret)
                               for n in notes]
    assert signature([n for n in timeline.notes if n.measure < 4]) == signature(
        [n for n in timeline.notes if 4 <= n.measure < 8])
    assert any(a.midi_note == b.midi_note for a,b in zip(timeline.notes, timeline.notes[1:]))
    ending = [n for n in timeline.notes if n.measure == 8]
    assert [n.midi_note for n in ending[:4]] == [28, 33, 28, 33]
    assert timeline.notes[-1].end_ms < timeline.duration_ms  # a real closing rest
    written = guitarpro.parse(path)
    assert [i for i,h in enumerate(written.measureHeaders) if h.marker] == [0, 4, 8]
    velocities = [[n.velocity for beat in measure.voices[0].beats for n in beat.notes]
                  for measure in written.tracks[0].measures]
    assert max(v for bar in velocities[4:8] for v in bar) < min(
        v for bar in velocities[:4] for v in bar)


def test_new_piece_is_reachable_for_playing_and_explicit_recording(tmp_path):
    from pickhero.ui.test_recording import RecordingRequest
    screen = MusicScreen(Config(instrument='bass_ukulele'), ProgressTracker(), tmp_path)
    screen.handle_event(key(pygame.K_END))
    screen.render(pygame.Surface((640, 480)))
    assert music_for_path(screen.path)[0].slug == PIECES[-1].slug
    assert f'piece:{len(PIECES)-1}' in screen.panel.hit
    request = screen.handle_event(key(pygame.K_u))
    assert isinstance(request, RecordingRequest)
    assert request.path == screen.path and request.practice.path == screen.path
    assert request.path.is_file()


def test_generation_is_idempotent_and_never_overwrites_custom_or_modified_files(tmp_path):
    assert ensure_music(BASS_UKULELE, tmp_path)
    folder = tmp_path/MUSIC_DIRNAME
    mine = folder/'my own song.gp5'
    mine.write_bytes(b'keep this')
    assert not ensure_music(BASS_UKULELE, tmp_path)
    generated = folder/PIECES[0].filename('start')
    generated.write_bytes(b'user edited this')
    with pytest.raises(ValueError, match='modified'):
        ensure_music(GUITAR, tmp_path)
    assert generated.read_bytes() == b'user edited this' and mine.read_bytes() == b'keep this'


def test_unowned_filename_collision_is_preserved(tmp_path):
    folder = tmp_path/MUSIC_DIRNAME
    folder.mkdir()
    path = folder/PIECES[0].filename('start')
    path.write_bytes(b'not ours')
    with pytest.raises(ValueError):
        ensure_music(BASS_UKULELE, tmp_path)
    assert path.read_bytes() == b'not ours'


def test_failed_atomic_replace_keeps_the_previous_piece(tmp_path, monkeypatch):
    ensure_music(BASS_UKULELE, tmp_path)
    folder = tmp_path/MUSIC_DIRNAME
    path = folder/PIECES[0].filename('start')
    before = path.read_bytes()
    def fail(*a, **k):
        raise OSError('full disk')
    monkeypatch.setattr(Path, 'replace', fail)
    with pytest.raises(OSError):
        ensure_music(GUITAR, tmp_path)
    assert path.read_bytes() == before and not list(folder.glob('*.tmp'))


def test_translated_music_keeps_history_but_other_instrument_and_version_do_not(tmp_path):
    tracker = ProgressTracker()
    ensure_music(BASS_UKULELE, tmp_path, tracker)
    path = tmp_path/MUSIC_DIRNAME/PIECES[0].filename('start')
    original_key = song_key(path, BASS_UKULELE.key, content_id=content_identity(path))
    tracker._data[original_key] = SongRecord(attempts=2, best_accuracy=85)
    i18n.set_language('en')
    ensure_music(BASS_UKULELE, tmp_path, tracker)
    current_key = song_key(path, BASS_UKULELE.key, content_id=content_identity(path))
    assert tracker.get_best(current_key).attempts == 2 and original_key in tracker._data
    other = path.with_name(PIECES[0].filename('groove'))
    assert tracker.get_best(song_key(other, BASS_UKULELE.key, content_id=content_identity(other))) is None
    ensure_music(GUITAR, tmp_path, tracker)
    assert tracker.get_best(song_key(path, GUITAR.key, content_id=content_identity(path))) is None
    ensure_music(BASS_UKULELE, tmp_path, tracker)
    assert tracker.get_best(song_key(path, BASS_UKULELE.key, content_id=content_identity(path))).attempts == 2


@pytest.mark.parametrize('look', ['studio', 'stage', 'hifi'])
@pytest.mark.parametrize('size', [(320, 240), (640, 480), (1280, 720)])
@pytest.mark.parametrize('language', ['de', 'en'])
def test_choices_resize_scroll_and_start_every_version(tmp_path, look, size, language):
    i18n.set_language(language)
    set_look(look)
    screen = MusicScreen(Config(instrument='bass_ukulele'), ProgressTracker(), tmp_path)
    assert not screen.error
    surface = pygame.Surface(size)
    for index in range(len(PIECES)):
        screen._select(index)
        for version in VERSIONS:
            screen.version = version
            screen._refresh()
            screen.render(surface)
            actions = [screen.panel.hit[a.name] for a in screen.panel.actions]
            assert all(surface.get_rect().contains(rect) for rect in actions)
            assert all(not a.colliderect(b) for i, a in enumerate(actions) for b in actions[i+1:])
            assert all(not rect.colliderect(screen.panel.body) for rect in actions)
            assert f'piece:{index}' in screen.panel.hit
            request = screen.handle_event(key(pygame.K_RETURN))
            assert music_for_path(request.path) == (PIECES[index], version)
    screen.handle_event(pygame.event.Event(pygame.MOUSEWHEEL, y=-100))
    screen.render(surface)
    assert screen._scroll == screen._maximum
    screen.handle_event(key(pygame.K_HOME))
    screen.render(surface)
    assert 'piece:0' in screen.panel.hit
    assert screen.handle_event(key(pygame.K_ESCAPE)) == 'back'


def test_listening_is_explicit_stops_on_change_and_never_scores(tmp_path, monkeypatch):
    from pickhero.ui import music_menu
    fake = Mock()
    fake.open.return_value = True
    constructor = Mock(return_value=fake)
    monkeypatch.setattr(music_menu, 'MidiPlayer', constructor)
    tracker = ProgressTracker()
    screen = MusicScreen(Config(), tracker, tmp_path)
    before = deepcopy(tracker._data)
    constructor.assert_not_called()
    screen.handle_event(key(pygame.K_d))
    fake.open.assert_called_once()
    assert screen._status == 'playing'
    screen.handle_event(key(pygame.K_v))
    fake.close.assert_called_once()
    assert screen._player is None and tracker._data == before
    screen.handle_event(key(pygame.K_d))
    assert screen.handle_event(key(pygame.K_t)) == 'technique'
    assert screen._status == 'idle' and screen._player is None
    assert next(a.label for a in screen.panel.actions if a.name == 'demo') == i18n.t('music.listen')
    fake.open.return_value = False
    screen.handle_event(key(pygame.K_d))
    assert screen._status == 'unavailable' and screen._player is None


def test_music_and_technique_are_reachable_without_replacing_the_old_lessons(tmp_path):
    screen = LessonScreen(Config(), ProgressTracker(), tmp_path, music_first=True)
    assert screen._mode == 'music'
    screen.handle_event(key(pygame.K_t))
    assert screen._mode == 'catalog' and len(screen.plan.lessons) == 10
    screen.handle_event(key(pygame.K_m))
    assert screen._mode == 'music'
    request = screen.handle_event(key(pygame.K_RETURN))
    assert screen.show_practice(request) and screen._music.path == request.path


def test_complete_music_can_continue_through_all_versions_without_certifying_goals(tmp_path):
    app = App(Config(instrument='bass_ukulele', songs_dir=str(tmp_path)))
    app._songs_dir = tmp_path
    app._open_lessons()
    assert app._state == 'menu' and app._menu.catalogue.category == 'music'
    first = tmp_path/MUSIC_DIRNAME/PIECES[0].filename('start')
    app._menu.focus_path(first)
    app._handle_menu_event(key(pygame.K_RETURN))
    expected = [(p, v) for p in PIECES for v in VERSIONS]
    for index, pair in enumerate(expected):
        screen = app._playing_screen
        assert music_for_path(screen._song_path) == pair
        assert screen._lesson is None and screen._music_piece == pair
        assert not screen._wait_mode and not screen._loop_enabled
        screen._song_completed = True
        screen._playback_ms = screen._timeline.duration_ms
        screen._playing = False
        screen._panel = None
        panel = screen._active_panel()
        if index < len(expected)-1:
            assert panel.actions[0].name == 'next_lesson'
            assert screen._next_lesson_hint() and '{' not in screen._next_lesson_hint()
        else:
            assert panel.actions[0].name == 'lessons'
        app._handle_playing_event(key(pygame.K_RETURN))
        assert all(not r.attempts and not r.goal_pitch_met and not r.goal_rhythm_met for r in app._progress._data.values())
    assert app._state == 'menu' and app._menu.catalogue.category == 'music'


def test_unknown_path_never_acquires_a_music_next_step(tmp_path):
    path = tmp_path/'My songs'/PIECES[0].filename('start')
    assert music_for_path(path) is None and next_lesson_practice(path, BASS_UKULELE) is None
