"""Real WAV round trips and explicit UI lifecycle; no physical input."""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import wave

import numpy as np
import pygame
import pytest

from pickhero import config as config_module, i18n
from pickhero.audio.input import AudioCapture
from pickhero.audio.recording import TestRecording
from pickhero.config import Config
from pickhero.tabs.timeline import NoteEvent, SongMetadata, Timeline
from pickhero.ui.colors import set_look
from pickhero.ui.scrolling import PlayingScreen


def read_take(take):
    assert take.finish({'reason': 'test'})
    report = json.loads((take.folder/'session.json').read_text(encoding='utf-8'))
    events = [json.loads(line) for line in (take.folder/'events.jsonl').read_text(encoding='utf-8').splitlines()]
    with wave.open(str(take.folder/'input.wav'), 'rb') as wav:
        signal = np.frombuffer(wav.readframes(wav.getnframes()), '<i4').astype(np.float64)/2147483648
        assert wav.getframerate() == take.rate and wav.getnchannels() == 1
    return report, events, signal


@pytest.mark.parametrize('rate', [44100, 48000, 96000, 192000])
def test_raw_audio_is_not_gated_normalized_or_retimed(tmp_path, rate):
    take = TestRecording(tmp_path, rate, {'label': 'acoustic'})
    signal = np.array([0., .125, -.25, .9, -1., 1., 1e-5], dtype=np.float32)
    take.audio(signal, 123., '')
    take.audio(signal, 200., 'input overflow')
    signal[:] = 0  # PortAudio is free to reuse its original callback buffer.
    report, events, pcm = read_take(take)
    assert report['status'] == 'saved'
    assert [e['capture_ms'] for e in events] == [123., 200.]
    assert events[1]['input_status'] == 'input overflow'
    assert [e['wav_frame'] for e in events] == [0, 7]
    assert pcm[:7] == pytest.approx([0, .125, -.25, np.float32(.9), -1, 1, 1e-5], abs=5e-10)
    assert pcm[:7] == pytest.approx(pcm[7:])


@pytest.mark.parametrize('bad', [np.nan, np.inf, -np.inf, 1.5])
def test_invalid_audio_is_flagged_without_replacing_it_with_silence(tmp_path, bad):
    take = TestRecording(tmp_path, 48000, {})
    take.audio(np.array([.25], np.float32), 0)
    take.audio(np.array([bad], np.float32), 1)
    report, events, pcm = read_take(take)
    assert report['status'] == 'partial' and report['problem'] == 'invalid_audio'
    assert pcm.tolist() == [.25]


def test_limit_keeps_exact_prefix_and_later_audio_is_not_saved(tmp_path):
    take = TestRecording(tmp_path, 8000, {})
    take.limit = 13
    take.audio(np.ones(20, np.float32)*.25, 0)
    take.audio(np.ones(20, np.float32)*.5, 3)
    report, _, pcm = read_take(take)
    assert report['problem'] == 'duration_limit' and len(pcm) == 13
    assert all(pcm == .25)


def test_queue_overload_is_visible_and_does_not_wait(tmp_path, monkeypatch):
    import queue
    take = TestRecording(tmp_path, 8000, {})
    monkeypatch.setattr(take._packets, 'put_nowait', Mock(side_effect=queue.Full))
    take.audio(np.zeros(80, np.float32), 0)
    assert not take.accepting and take.problem == 'queue_full'
    report, _, pcm = read_take(take)
    assert report['status'] == 'partial' and not len(pcm)


def test_disk_error_is_reported_and_writer_terminates(tmp_path, monkeypatch):
    monkeypatch.setattr(wave.Wave_write, 'writeframesraw', Mock(side_effect=OSError('disk full')))
    take = TestRecording(tmp_path, 8000, {})
    take.audio(np.ones(10, np.float32), 0)
    take.finish()
    assert take.done.is_set() and 'disk full' in take.error
    assert json.loads((take.folder/'session.json').read_text())['status'] == 'failed'


def test_repeat_takes_never_overwrite_each_other(tmp_path):
    a, b = TestRecording(tmp_path, 8000, {}), TestRecording(tmp_path, 8000, {})
    assert a.folder != b.folder
    a.finish()
    b.finish()


@pytest.fixture
def screen(monkeypatch):
    pygame.font.init()
    monkeypatch.setattr(PlayingScreen, '_init_midi_player', lambda *args: None)
    def start(capture):
        capture._start_time = 100.
        capture._stream = SimpleNamespace(active=True, stop=Mock(), close=Mock())
    monkeypatch.setattr(AudioCapture, 'start', start)
    monkeypatch.setattr('pickhero.audio.input.time.perf_counter', lambda: 100.)
    metadata = SongMetadata(title='Musiktest', tuning={1: 43, 2: 38, 3: 33, 4: 28})
    s = PlayingScreen(Timeline([NoteEvent(1000, 1000, 28, 4, 0)], metadata),
                      config=Config(instrument='bass_ukulele'), song_key='test')
    yield s
    s.stop_audio()
    set_look('studio')
    i18n.set_language('de')


def key(value):
    return pygame.event.Event(pygame.KEYDOWN, key=value, mod=0)


def test_normal_practice_and_opening_panel_do_not_record(screen):
    root = config_module.CONFIG_DIR/'recordings'
    screen.toggle_play()
    assert screen._audio_capture.test_recording is None
    screen.handle_event(key(pygame.K_u))
    assert screen._panel.kind == 'test_recording'
    assert not root.exists()
    screen.handle_event(key(pygame.K_ESCAPE))
    assert screen._panel is None


@pytest.mark.parametrize('label', ['acoustic', 'amplified', 'direct'])
def test_explicit_take_resumes_same_piece_after_pause(screen, label):
    screen._panel_action('record:'+label)
    rec = screen._test_recording
    take = rec.take
    assert rec._attached and screen._playing
    assert screen._audio_capture.test_recording is take
    assert take.metadata['label'] == label
    assert take.metadata['intended_notes'][0]['midi_note'] == 28
    take.audio(np.ones(10, np.float32)*.01, 20)
    screen.toggle_play()
    assert take.done.is_set() and screen._audio_capture.test_recording is None
    report = json.loads((take.folder/'session.json').read_text())
    assert not report['finish']['reached_end']
    screen.toggle_play()
    assert rec._attached and screen._audio_capture.test_recording is rec.take
    assert rec.take is not take
    assert rec.take.metadata['series_id'] == take.metadata['series_id']
    assert rec.take.metadata['series_segment'] == 2
    assert len(list(take.folder.parent.iterdir())) == 2


def test_u_stops_recording_and_folder_button_opens_only_current_take(screen, monkeypatch):
    screen._panel_action('record:acoustic')
    take = screen._test_recording.take
    screen.handle_event(key(pygame.K_u))
    assert screen._playing and take.done.is_set()
    assert not screen._test_recording.series.visible
    screen.handle_event(key(pygame.K_u))
    assert not screen._playing and take.done.is_set()
    launch = Mock()
    monkeypatch.setattr('pickhero.ui.test_recording.os.startfile', launch)
    screen._panel_action('record:folder')
    launch.assert_called_once_with(str(take.folder))


def test_recording_limit_detaches_without_interrupting_practice(screen):
    screen._panel_action('record:acoustic')
    take = screen._test_recording.take
    take.limit = 2
    take.audio(np.zeros(4, np.float32), 0)
    screen._test_recording.observe()
    assert screen._playing and screen._audio_capture.test_recording is None
    assert take.done.is_set()


def test_end_writes_final_result_but_later_unrecorded_play_cannot_replace_it(screen):
    screen._panel_action('record:acoustic')
    take = screen._test_recording.take
    screen._playback_ms = screen._end_ms
    screen._stop_audio(recording_complete=True)
    screen._log_result({'hits': 1, 'total': 1})
    saved = (take.folder/'result.json').read_bytes()
    assert json.loads(saved)['hits'] == 1
    screen._log_result({'hits': 0, 'total': 1})
    assert (take.folder/'result.json').read_bytes() == saved


def test_seek_to_end_cannot_attach_a_later_result_to_an_interrupted_take(screen):
    screen._panel_action('record:acoustic')
    take = screen._test_recording.take
    screen.seek(screen._end_ms)
    screen._log_result({'hits': 1, 'total': 1})
    assert not (take.folder/'result.json').exists()


def test_stalled_input_still_ends_after_wall_clock_limit(screen, monkeypatch):
    screen._panel_action('record:acoustic')
    take = screen._test_recording.take
    monkeypatch.setattr('pickhero.ui.test_recording.time.perf_counter', lambda: take.started_at + 181)
    screen._test_recording.observe()
    assert not screen._test_recording._attached and take.done.is_set()
    assert take.problem == 'duration_limit'


@pytest.mark.parametrize('look', ['studio', 'stage', 'hifi'])
@pytest.mark.parametrize('language', ['de', 'en'])
@pytest.mark.parametrize('size', [(320, 240), (640, 480), (1280, 720)])
def test_recording_panel_actions_and_status_fit_themes(screen, look, language, size):
    set_look(look)
    i18n.set_language(language)
    screen.handle_event(key(pygame.K_u))
    surface = pygame.Surface(size)
    screen.render(surface)
    for label in ('acoustic', 'amplified', 'direct'):
        assert surface.get_rect().contains(screen._panel.hit['record:'+label])
    screen.handle_event(pygame.event.Event(pygame.MOUSEBUTTONDOWN, button=1,
        pos=screen._panel.hit['record:acoustic'].center))
    screen.render(surface)
    assert screen._test_recording._attached
    badge = screen._test_recording.badge_rect
    assert surface.get_rect().contains(badge)
    assert not any(badge.colliderect(box) for boxes in screen._button_rects.values() for box in boxes)
    screen.handle_event(key(pygame.K_u))
    assert not screen._test_recording.series.visible
    screen.handle_event(key(pygame.K_u))
    screen.render(surface)
    assert surface.get_rect().contains(screen._panel.hit['record:folder'])


def test_callback_recording_does_not_change_detector_input_or_timestamp(tmp_path, monkeypatch):
    from pickhero.audio.detector import DetectedNote
    from pickhero.audio import input as module
    def run(take):
        capture = AudioCapture(Config())
        capture._start_time = 100.
        capture.test_recording = take
        chunks = []
        def process(chunk):
            chunks.append(chunk.copy())
            return DetectedNote(69, 440., .99, 'A4', True, 1)
        capture.detector = SimpleNamespace(hop_size=10, sample_rate=1000,
            last_signal_db=-38, last_freq=440, last_confidence=.99,
            onset_latency_ms=2, process=process)
        monkeypatch.setattr(module.time, 'perf_counter', lambda: 100.05)
        mono = np.arange(30, dtype=np.float32)/100
        capture._audio_callback(mono[:, None], 30,
            SimpleNamespace(inputBufferAdcTime=9.96, currentTime=10.), False)
        return [(n.timestamp_ms, n.note.midi_note) for n in capture.get_notes()], chunks, mono
    before, before_chunks, _ = run(None)
    take = TestRecording(tmp_path, 8000, {})
    after, after_chunks, mono = run(take)
    report, events, pcm = read_take(take)
    assert before == after
    assert all(np.array_equal(a,b) for a,b in zip(before_chunks,after_chunks))
    assert pcm == pytest.approx(mono, abs=5e-10)
    assert [e['capture_ms'] for e in events if e['kind'] == 'detected'] == pytest.approx([8,18,28])


@pytest.mark.parametrize('look', ['studio', 'stage', 'hifi'])
@pytest.mark.parametrize('language', ['de', 'en'])
def test_mouse_recording_entry_does_not_crowd_help_footer(screen, look, language):
    set_look(look)
    i18n.set_language(language)
    screen._open_help()
    surface = pygame.Surface((320, 240))
    screen.render(surface)
    panel = screen._panel
    button = panel.hit['record']
    assert panel.bounds.contains(button)
    assert not button.colliderect(panel.hit['header_close'])
    assert button.bottom < panel.body.top and panel.body.height >= 20
    screen.handle_event(pygame.event.Event(pygame.MOUSEBUTTONDOWN, button=1, pos=button.center))
    assert screen._panel.kind == 'test_recording' and not screen._test_recording.take
