"""The heard count-in pulse and capture clock must describe the same song."""
from types import SimpleNamespace

import pytest

from pickhero.audio.detector import DetectedNote
from pickhero.audio.input import TimestampedNote
from pickhero.config import Config
from pickhero.tabs.timeline import NoteEvent, SongMetadata, Timeline
from pickhero.ui.scrolling import PlayingScreen


@pytest.fixture
def clock_screen(monkeypatch):
    clock = SimpleNamespace(now=100.0, opening_seconds=.3)

    class Capture:
        def __init__(self, *args, **kwargs):
            self.detector = SimpleNamespace(expected_delay_ms=400)
            self.sample_rate = 44100
            self.started = clock.now
            self.starts = 0
            self.notes = []

        def start(self):
            self.started = clock.now
            self.starts += 1
            self.notes.clear()
            clock.now += clock.opening_seconds

        def stop(self): pass
        def get_signal_db(self): return -25
        def get_tuner_data(self): return (55, 1)
        def capture_elapsed_ms(self): return (clock.now - self.started) * 1000
        def get_notes(self):
            result, self.notes = self.notes, []
            return result

    monkeypatch.setattr("pickhero.audio.input.AudioCapture", Capture)
    monkeypatch.setattr("pickhero.ui.scrolling.time.perf_counter", lambda: clock.now)
    monkeypatch.setattr(PlayingScreen, "_init_midi_player", lambda *args: None)
    monkeypatch.setattr("pickhero.ui.scrolling.log_error", lambda *args: None)

    def make(tempo=1, wait=False):
        config = Config(instrument="bass_ukulele", count_in_beats=4,
                        tempo_factor=tempo, wait_mode=wait)
        config.save = lambda: None
        notes = [NoteEvent(index * 500, 100, (28, 33, 38, 43)[index % 4],
                           4 - index % 4, 0) for index in range(28)]
        screen = PlayingScreen(Timeline(notes, SongMetadata(tempo=120)), config=config)
        clicks = []
        screen._midi_player = SimpleNamespace(
            play_click=lambda *args: clicks.append(clock.now), seek=lambda *args: None,
            update=lambda *args: None, pause=lambda: None)
        return clock, screen, clicks

    return make


def heard(at, midi=28):
    return TimestampedNote(DetectedNote(midi, 55, 1, "test", True), at)


@pytest.mark.parametrize("tempo", [.5, 1])
@pytest.mark.parametrize("opening_seconds", [0, .12, .3, .39])
def test_28_notes_following_audible_count_in_are_in_time_despite_device_opening(
        clock_screen, tempo, opening_seconds):
    clock, screen, clicks = clock_screen(tempo=tempo)
    clock.opening_seconds = opening_seconds
    screen.toggle_play()
    screen.update()
    capture = screen._audio_capture
    # The player's reference is the already heard click, not the screen's
    # eventual zero. This catches a stream-open stall between beats3 and4.
    song_zero = clicks[0] + 2 / tempo
    pending = [(song_zero + note.timestamp_ms / 1000 / tempo, note,
                (0, .04, .12, .24, .35)[index % 5])
               for index, note in enumerate(screen._timeline.notes)]
    for _ in range(10000):
        clock.now += 1 / 120
        for strike, note, delay in list(pending):
            if strike + delay <= clock.now:
                capture.notes.append(heard((strike - capture.started) * 1000, note.midi_note))
                pending.remove((strike, note, delay))
        screen.update()
        if screen._song_completed:
            break
    assert screen._song_completed
    assert not pending
    assert capture.starts == 1
    stats = screen._matcher.get_statistics()
    assert stats["hits"] == stats["in_time"] == 28
    assert stats["timing_median_ms"] == pytest.approx(0, abs=.001)
    assert clicks[4] - clicks[3] == pytest.approx(.5 / tempo, abs=.009)


def test_crossing_zero_keeps_frame_overshoot_and_capture_anchor(clock_screen):
    clock, screen, _ = clock_screen()
    screen.toggle_play()
    capture = screen._audio_capture
    anchor = screen._audio_anchor_capture_ms
    clock.now += 2.05
    screen.update()
    assert screen._playback_ms == pytest.approx(50)
    assert capture.starts == 1
    assert screen._audio_anchor_capture_ms == anchor
    on_time = heard(anchor + 2000)
    screen._convert_timestamps([on_time])
    assert on_time.timestamp_ms + screen._matcher.audio_offset_ms == pytest.approx(0)


@pytest.mark.parametrize("delayed", [False, True])
def test_count_in_discards_warmup_but_keeps_valid_early_first_strike(clock_screen, delayed):
    clock, screen, _ = clock_screen()
    screen.toggle_play()
    capture = screen._audio_capture
    anchor = screen._audio_anchor_capture_ms
    clock.now += 1.4
    capture.notes = [heard(anchor + 1400)]  # Warm-up, outside 500 ms pitch reach.
    screen.update()
    assert screen._matcher.hits == 0
    clock.now += .7 if delayed else .5
    capture.notes = [heard(anchor + 1900)]  # Correct first pitch, 100 ms early.
    screen.update()
    assert screen._matcher.hits == screen._matcher.in_time == 1
    assert screen._matcher.timing_errors == pytest.approx([-100])


def test_pause_resume_during_count_in_reanchors_before_remaining_pulse(clock_screen):
    clock, screen, _ = clock_screen()
    screen.toggle_play()
    clock.now += .6
    screen.update()
    screen.toggle_play()
    before = screen._playback_ms
    clock.now += 5
    screen.toggle_play()
    assert screen._playback_ms == before
    capture = screen._audio_capture
    current = heard(capture.capture_elapsed_ms())
    screen._convert_timestamps([current])
    assert current.timestamp_ms + screen._matcher.audio_offset_ms == pytest.approx(before)
    clock.now += -before / 1000 + .025
    screen.update()
    assert screen._playback_ms == pytest.approx(25)
    assert capture.starts == 2


def test_enabling_audio_during_count_in_opens_before_a_fresh_count_in(clock_screen):
    clock, screen, clicks = clock_screen()
    screen._audio_enabled = False
    screen.toggle_play()
    clock.now += .7
    screen.update()
    screen._toggle_audio()
    assert screen._playback_ms == -2000
    assert screen._audio_capture.starts == 1
    screen.update()
    first_new_click = clicks[-1]
    clock.now += 2
    screen.update()
    assert screen._playback_ms == pytest.approx(0)
    assert clicks[-1] - first_new_click == pytest.approx(2)
    assert screen._audio_capture.starts == 1


def test_filter_change_refreshes_first_allowed_count_in_strike(clock_screen):
    clock, screen, _ = clock_screen()
    screen._active_strings[3] = False
    screen.toggle_play()
    assert screen._preroll_before_ms == 0
    screen._toggle_string(4)
    assert screen._preroll_before_ms == -500
    capture = screen._audio_capture
    clock.now += 1.9
    capture.notes = [heard(screen._audio_anchor_capture_ms + 1900)]
    screen.update()
    assert screen._matcher.in_time == 1


def test_first_early_note_after_song_zero_keeps_pitch_credit(clock_screen):
    clock, screen, _ = clock_screen()
    screen._active_strings[3] = False  # First eligible note is A1 at500ms.
    screen.toggle_play()
    capture = screen._audio_capture
    clock.now += 2.2
    capture.notes = [heard(screen._audio_anchor_capture_ms + 2200, midi=33)]
    screen.update()
    assert screen._matcher.hits == screen._matcher.early == 1
    assert screen._matcher.timing_errors == pytest.approx([-300])


def test_tempo_change_after_stall_advances_old_clock_and_preserves_late_reports(clock_screen):
    clock, screen, _ = clock_screen()
    screen._count_in_ms = 0
    screen.toggle_play()
    original_anchor = screen._audio_anchor_capture_ms
    clock.now += .5
    screen.update()
    clock.now += .3  # Slow event handler before the next UI update.
    screen.set_tempo_factor(.5)
    assert screen._playback_ms == pytest.approx(800)
    clock.now += .4
    # The first report refers to an old-tempo strike; the second to a new one.
    notes = [heard(original_anchor + 500, midi=33),
             heard(original_anchor + 1200, midi=38)]
    screen._convert_timestamps(notes)
    absolute = [note.timestamp_ms + screen._matcher.audio_offset_ms for note in notes]
    assert absolute == pytest.approx([500, 1000])
    screen._matcher.process_detected_notes(notes, 1000)
    assert screen._matcher.in_time == 2
    screen.update()
    assert screen._playback_ms == pytest.approx(1000)


def test_result_log_includes_capture_timing_metadata(clock_screen, monkeypatch):
    _, screen, _ = clock_screen()
    screen.toggle_play()
    screen._audio_capture.timing_diagnostics = lambda: {
        "clock_source": "adc", "stream_start_duration_ms": 300,
    }
    logged = []
    monkeypatch.setattr("pickhero.ui.scrolling.log_error", logged.append)
    screen._log_result({"hits": 28, "total": 28, "in_time": 28})
    assert len(logged) == 1
    assert 'audio_timing={"clock_source": "adc", "stream_start_duration_ms": 300}' in logged[0]


def test_slow_tempo_update_does_not_skip_a_wait_target(clock_screen, monkeypatch):
    clock, screen, _ = clock_screen(wait=True)
    screen._count_in_ms = 0
    screen.seek(900)
    screen.toggle_play()
    matcher = screen._matcher
    process = matcher.process_detected_notes

    def slow_matching(*args):
        result = process(*args)
        clock.now += .2
        return result

    monkeypatch.setattr(matcher, "process_detected_notes", slow_matching)
    screen.set_tempo_factor(.5)
    assert screen._playback_ms == pytest.approx(900)
    monkeypatch.setattr(matcher, "process_detected_notes", process)
    clock.now += .02
    screen.update()
    assert screen._playback_ms == pytest.approx(1000)
    assert screen._wait_mode_frozen
    assert matcher.first_pending_between(1000, 1000) == 1000


def test_slow_tempo_update_keeps_count_in_crossing_in_normal_update(clock_screen, monkeypatch):
    clock, screen, _ = clock_screen()
    screen.toggle_play()
    clock.now += 1.9
    screen.update()
    assert screen._playback_ms == pytest.approx(-100)
    seeks = []
    screen._midi_player.seek = seeks.append
    matcher = screen._matcher
    process = matcher.process_detected_notes

    def slow_matching(*args):
        result = process(*args)
        clock.now += .2
        return result

    monkeypatch.setattr(matcher, "process_detected_notes", slow_matching)
    screen.set_tempo_factor(.5)
    assert screen._playback_ms == pytest.approx(-100)
    monkeypatch.setattr(matcher, "process_detected_notes", process)
    clock.now += .02
    screen.update()
    assert screen._playback_ms == pytest.approx(10)
    assert seeks == [0]
    assert screen._last_beat_played == 0
    assert screen._audio_capture.starts == 1
