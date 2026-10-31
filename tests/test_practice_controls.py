"""Practice controls must change the advertised state and preserve the clocks."""
import pygame
import pytest

from pickhero.config import Config
from pickhero.tabs.timeline import MeasureInfo, NoteEvent, SongMetadata, Timeline
from pickhero.ui.colors import set_look
from pickhero.ui.scrolling import PlayingScreen
from tests.test_timing_clock import clock_screen, heard


def key(value, **kwargs):
    return pygame.event.Event(pygame.KEYDOWN, key=value, mod=0, **kwargs)


def click(rect):
    return pygame.event.Event(pygame.MOUSEBUTTONDOWN, button=1, pos=rect.center)


@pytest.fixture(autouse=True)
def fonts():
    pygame.font.init()
    yield
    set_look("studio")


@pytest.mark.parametrize("look", ["studio", "stage", "hifi"])
@pytest.mark.parametrize("close_method", ["h", "escape", "mouse", "focus"])
def test_help_pauses_blocks_background_and_resumes_same_assessment(clock_screen, look, close_method):
    set_look(look)
    clock, screen, _ = clock_screen()
    screen._count_in_ms = 0
    screen.toggle_play()
    clock.now += .1
    screen.update()
    matcher = screen._matcher
    matcher.process_detected_notes([heard(-matcher.audio_offset_ms)], screen._playback_ms)
    before_hits = matcher.get_statistics()["hits"]
    screen.handle_event(key(pygame.K_h))
    position = screen._playback_ms
    assert not screen.is_playing()
    assert screen._panel.kind == "help"
    screen.render(pygame.Surface((640, 480)))
    old_wait = screen._wait_mode
    screen.handle_event(key(pygame.K_w))
    screen.handle_event(pygame.event.Event(pygame.MOUSEBUTTONDOWN, button=1, pos=(0, 0)))
    clock.now += 12
    screen.update()
    assert screen._wait_mode == old_wait
    assert screen._playback_ms == position
    if close_method == "mouse":
        result = screen.handle_event(click(screen._panel.hit["header_close"]))
    else:
        result = screen.handle_event(key({"h": pygame.K_h, "escape": pygame.K_ESCAPE,
                                          "focus": pygame.K_RETURN}[close_method]))
    assert result is None
    assert screen.is_playing()
    assert screen._matcher is matcher
    assert matcher.get_statistics()["hits"] == before_hits
    assert screen._playback_ms == position
    clock.now += .025
    screen.update()
    assert screen._playback_ms == pytest.approx(position + 25)
    assert matcher.audio_offset_ms == pytest.approx(position)


def test_help_from_paused_song_stays_paused(clock_screen):
    _, screen, _ = clock_screen()
    screen.handle_event(key(pygame.K_h))
    screen.handle_event(key(pygame.K_ESCAPE))
    assert not screen.is_playing()
    assert not screen._show_help


@pytest.mark.parametrize("look", ["studio", "stage", "hifi"])
@pytest.mark.parametrize("kind", ["help", "result", "setup"])
@pytest.mark.parametrize("size", [(320, 240), (640, 480), (1280, 720)])
def test_modal_content_scrolls_and_actions_stay_inside_small_viewport(clock_screen, look, kind, size):
    set_look(look)
    _, screen, _ = clock_screen()
    if kind == "help":
        screen._open_help()
    elif kind == "setup":
        screen._open_setup()
    else:
        screen._song_completed = True
        screen._recommendations = ["A long practice recommendation " * 12] * 3
    surface = pygame.Surface(size)
    screen.render(surface)
    panel = screen._panel
    assert panel.kind == kind
    assert panel.body.height > 0
    assert surface.get_rect().contains(panel.bounds)
    for action in panel.actions:
        rect = panel.hit[action.name]
        assert panel.bounds.contains(rect)
        assert rect.top > panel.body.bottom
    assert panel.hit["header_close"].bottom < panel.body.top
    if panel.max_scroll:
        screen.handle_event(key(pygame.K_END))
        assert panel.scroll == panel.max_scroll
        screen.handle_event(pygame.event.Event(pygame.MOUSEWHEEL, y=1))
        assert panel.scroll < panel.max_scroll
    screen.handle_event(key(pygame.K_TAB))
    assert panel.focus == 1


@pytest.mark.parametrize("command", ["device", "check", "calibrate"])
def test_setup_command_releases_capture_and_resume_preserves_song_state(clock_screen, command):
    clock, screen, _ = clock_screen(tempo=.5)
    screen._count_in_ms = 0
    screen.toggle_play()
    clock.now += .3
    screen.update()
    screen._loop_start_ms, screen._loop_end_ms, screen._loop_enabled = 0, 2000, True
    matcher, instrument = screen._matcher, screen._instrument
    marker_states = [matcher.get_note_state(note) for note in screen._timeline.notes]
    screen.handle_event(key(pygame.K_s))
    position = screen._playback_ms
    screen.render(pygame.Surface((640, 480)))
    assert screen.handle_event(click(screen._panel.hit[command])) == command
    assert screen._audio_capture is None
    assert not screen.is_playing()
    screen.suspend_for_setup()  # App routing may call this a second time.
    screen._config.audio.noise_gate_db = -52
    screen._config.audio_latency_offset_ms = 30
    clock.now += 9
    screen.resume_from_setup()
    assert screen._matcher is matcher
    assert screen._instrument is instrument
    assert [matcher.get_note_state(note) for note in screen._timeline.notes] == marker_states
    assert screen._playback_ms == position
    assert screen._tempo_factor == .5
    assert (screen._loop_start_ms, screen._loop_end_ms, screen._loop_enabled) == (0, 2000, True)
    assert screen._noise_gate_db == -52
    assert not screen.is_playing()
    screen.toggle_play()
    assert screen._matcher is matcher
    assert matcher.audio_offset_ms == pytest.approx(position + 15)


def test_f1_through_f12_toggle_only_existing_strings(monkeypatch):
    monkeypatch.setattr(PlayingScreen, "_init_midi_player", lambda *args: None)
    tuning = {i: 76 - 3 * i for i in range(1, 13)}
    screen = PlayingScreen(Timeline([NoteEvent(0, 100, 40, 12, 0)],
                                   SongMetadata(tuning=tuning)), config=Config())
    for number in range(1, 13):
        before = list(screen._active_strings)
        screen.handle_event(key(getattr(pygame, f"K_F{number}")))
        assert screen._active_strings[number - 1] is False
        assert sum(a != b for a, b in zip(before, screen._active_strings)) == 1
        screen.handle_event(key(getattr(pygame, f"K_F{number}")))
    assert all(screen._active_strings)


def test_loop_creates_reusable_two_bar_passage_and_clear_is_explicit(clock_screen):
    _, screen, _ = clock_screen()
    screen._timeline = Timeline(screen._timeline.notes, measures=[
        MeasureInfo(i, i * 2000, (i + 1) * 2000) for i in range(7)])
    screen.seek(4300)
    screen.handle_event(key(pygame.K_p))
    assert (screen._loop_start_ms, screen._loop_end_ms) == (4000, 8000)
    assert screen._loop_enabled
    screen.handle_event(key(pygame.K_p))
    assert not screen._loop_enabled
    screen.handle_event(key(pygame.K_p))
    assert screen._loop_enabled
    assert (screen._loop_start_ms, screen._loop_end_ms) == (4000, 8000)
    screen.handle_event(key(pygame.K_BACKSPACE))
    assert not screen._loop_enabled
    assert screen._loop_start_ms is screen._loop_end_ms is None


@pytest.mark.parametrize("look", ["studio", "stage", "hifi"])
@pytest.mark.parametrize("via", ["mouse", "keyboard"])
def test_result_weak_action_starts_bounded_section_with_count_in(clock_screen, look, via):
    set_look(look)
    clock, screen, clicks = clock_screen()
    screen._timeline = Timeline(screen._timeline.notes, SongMetadata(tempo=120), measures=[
        MeasureInfo(i, i * 2000, (i + 1) * 2000) for i in range(7)])
    screen._song_completed = True
    screen._weakest_sections = [(2, 6, 20)]
    screen.render(pygame.Surface((640, 480)))
    screen.handle_event(click(screen._panel.hit["weak"]) if via == "mouse" else key(pygame.K_l))
    assert screen.is_playing()
    assert not screen._song_completed
    assert screen._panel is None
    assert (screen._loop_start_ms, screen._loop_end_ms) == (4000, 12000)
    assert screen._assessment_start_ms == 4000
    assert screen._playback_ms == 2000
    screen.update()
    assert len(clicks) == 1
    assert screen._page(pygame.Surface((640, 480))).count_in_remaining == 4
    capture = screen._audio_capture
    clock.now += 2.025
    screen.update()
    assert screen._playback_ms == pytest.approx(4025)
    assert capture.starts == 1
    assert screen._page(pygame.Surface((640, 480))).count_in_remaining == 0
    capture.notes.append(heard((clock.now - capture.started) * 1000 - 25, midi=28))
    screen.update()
    assert screen._matcher.get_statistics()["in_time"] == 1


def test_result_blocks_background_keys_and_replay_actually_starts(clock_screen):
    _, screen, _ = clock_screen()
    screen._song_completed = True
    screen.render(pygame.Surface((640, 480)))
    before = (screen._wait_mode, screen._audio_enabled, screen._tempo_factor)
    for value in (pygame.K_w, pygame.K_a, pygame.K_PAGEUP, pygame.K_p):
        screen.handle_event(key(value))
    assert (screen._wait_mode, screen._audio_enabled, screen._tempo_factor) == before
    assert screen._panel.kind == "result"
    screen.handle_event(click(screen._panel.hit["replay"]))
    assert screen.is_playing()
    assert not screen._song_completed
    assert screen._playback_ms == -2000


def test_result_escape_returns_menu(clock_screen):
    _, screen, _ = clock_screen()
    screen._midi_player = None
    screen._song_completed = True
    assert screen.handle_event(key(pygame.K_ESCAPE)) == "menu"


def test_home_replays_results_but_scrolls_help_to_top(clock_screen):
    _, screen, _ = clock_screen()
    screen._open_help()
    screen.render(pygame.Surface((320, 240)))
    screen.handle_event(key(pygame.K_END))
    assert screen._panel.scroll > 0
    screen.handle_event(key(pygame.K_HOME))
    assert screen._panel.scroll == 0
    assert not screen.is_playing()
    screen.handle_event(key(pygame.K_ESCAPE))
    screen._song_completed = True
    screen.handle_event(key(pygame.K_HOME))
    assert screen.is_playing()
    assert not screen._song_completed
    assert screen._panel is None
    assert screen._playback_ms == -2000


def test_positive_section_count_in_does_not_raise_quiet_input_warning(clock_screen):
    clock, screen, _ = clock_screen(tempo=.25)
    screen._timeline = Timeline(screen._timeline.notes, SongMetadata(tempo=120), measures=[
        MeasureInfo(i, i * 2000, (i + 1) * 2000) for i in range(7)])
    screen._song_completed = True
    screen._weakest_sections = [(4, 5, 20)]
    screen._loop_weakest_section()
    assert screen._playback_ms == 6000
    screen._signal_db_smooth = -100
    assert screen._quiet_warning() is None
    assert screen._quiet_since is None
    clock.now += 3
    screen.update()
    assert 6000 < screen._playback_ms < 8000
    screen._signal_db_smooth = -100
    assert screen._quiet_warning() is None
    assert screen._quiet_since is None
    # The ordinary warning remains active after the practice entry point.
    screen._playback_ms = screen._count_in_target_ms
    assert screen._quiet_warning() is None
    clock.now += 3
    assert screen._quiet_warning() is not None


@pytest.mark.parametrize("language", ["de", "en"])
def test_weak_result_accuracy_labels_only_the_assessed_bar_not_good_neighbour(
        clock_screen, monkeypatch, language):
    from pickhero import i18n
    monkeypatch.setattr(i18n, "_current", language)
    _, screen, _ = clock_screen()
    screen._timeline = Timeline([
        NoteEvent(0, 100, 28, 4, 0, measure=0),
        NoteEvent(500, 100, 28, 4, 0, measure=0),
        NoteEvent(2000, 100, 28, 4, 0, measure=1),
        NoteEvent(2500, 100, 28, 4, 0, measure=1),
    ], measures=[MeasureInfo(0, 0, 2000), MeasureInfo(1, 2000, 4000)])
    screen._count_in_ms = 0
    screen.toggle_play()
    for timestamp in (2000, 2500):
        screen._matcher.process_detected_notes([heard(timestamp)], timestamp)
    screen._matcher.finish()
    screen._playing = False
    screen._song_completed = True
    screen._weakest_sections = screen._matcher.get_weakest_sections(min_length=1)
    assert screen._weakest_sections == [(0, 0, 0.0)]
    result_text = [line for _, lines in screen._result_panel().sections for line in lines]
    assert i18n.t("play.weakest_single", bar=1, percent="0") in result_text
    assert i18n.t("play.weakest", start=1, end=2, percent="0") not in result_text
    screen._loop_weakest_section()
    assert (screen._loop_start_ms, screen._loop_end_ms) == (0, 4000)
    assert screen.is_playing()


@pytest.mark.parametrize("action", [pygame.K_f, pygame.K_F1, pygame.K_v, pygame.K_p])
def test_filter_or_loop_change_during_positive_count_in_excludes_previous_passage(clock_screen, action):
    _, screen, _ = clock_screen()
    screen._timeline = Timeline(screen._timeline.notes, SongMetadata(tempo=120), measures=[
        MeasureInfo(i, i * 2000, (i + 1) * 2000) for i in range(7)])
    screen._song_completed = True
    screen._weakest_sections = [(4, 5, 20)]
    screen._loop_weakest_section()
    assert screen._playback_ms == 6000
    assert screen._count_in_target_ms == 8000
    screen.handle_event(key(action))
    assert screen._assessment_start_ms == 8000
    screen._matcher.finish()
    expected = sum(8000 <= note.timestamp_ms < (12000 if screen._loop_enabled else float("inf"))
                   and screen._note_passes_filter(note) for note in screen._timeline.notes)
    assert screen._matcher.get_statistics()["total"] == expected


def test_help_during_count_in_preserves_remaining_pulse_and_target(clock_screen):
    clock, screen, clicks = clock_screen()
    screen.toggle_play()
    screen.update()
    clock.now += .5
    screen.update()
    screen.handle_event(key(pygame.K_h))
    assert screen._playback_ms == pytest.approx(-1500)
    clock.now += 10
    screen.handle_event(key(pygame.K_ESCAPE))
    clock.now += 1.5
    screen.update()
    assert screen._playback_ms == pytest.approx(0)
    assert screen._audio_capture.starts == 2
    screen._audio_capture.notes.append(heard((clock.now - screen._audio_capture.started) * 1000))
    screen.update()
    assert screen._matcher.get_statistics()["in_time"] == 1


def test_help_preserves_frozen_wait_note_and_matcher(clock_screen):
    clock, screen, _ = clock_screen(wait=True)
    screen._count_in_ms = 0
    screen.toggle_play()
    clock.now += .1
    screen.update()
    assert screen._wait_mode_frozen
    matcher = screen._matcher
    screen.handle_event(key(pygame.K_h))
    clock.now += 10
    screen.handle_event(key(pygame.K_h))
    assert screen._wait_mode_frozen
    assert screen._matcher is matcher
    screen._audio_capture.notes.append(heard(0))
    screen.update()
    assert matcher.get_statistics()["hits"] == 1
    clock.now += .01
    screen.update()
    assert not screen._wait_mode_frozen


@pytest.mark.parametrize("look", ["studio", "stage", "hifi"])
def test_arbitrarily_long_song_title_does_not_displace_result_actions(clock_screen, look):
    set_look(look)
    _, screen, _ = clock_screen()
    screen._timeline.metadata.title = "A very long imported song title " * 40
    screen._song_completed = True
    screen.render(pygame.Surface((320, 240)))
    assert 0 < screen._panel.body.height < 240
    assert screen._panel.bounds.contains(screen._panel.body)
    assert screen._panel.max_scroll > 0
    assert screen._panel.hit["replay"].top > screen._panel.body.bottom


@pytest.mark.parametrize("look", ["studio", "stage", "hifi"])
def test_visible_count_in_uses_top_layer_in_every_look(clock_screen, look, monkeypatch):
    from importlib import import_module
    set_look(look)
    _, screen, _ = clock_screen()
    screen.toggle_play()
    calls = []
    painter = import_module("pickhero.ui.paint_" + look)
    monkeypatch.setattr(painter, "count_in", lambda surface, page: calls.append(page.count_in_remaining))
    screen.render(pygame.Surface((640, 480)))
    assert calls == [4]
