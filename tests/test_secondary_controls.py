"""Setup controls must remain operable without audio hardware or user files."""
from types import SimpleNamespace
from unittest.mock import Mock

import pygame
import pytest

from pickhero.audio.note_utils import midi_to_freq
from pickhero.config import Config, StringCalibration
from pickhero.instruments import BASS_UKULELE, GUITAR, with_tuning
from pickhero.ui import calibration_menu, check_menu, device_menu, secondary
from pickhero.ui.colors import get_look, set_look


class Capture:
    instances = []

    def __init__(self, config, *, instrument=None):
        self.instrument = instrument
        self.start = Mock()
        self.stop = Mock()
        self.detector = SimpleNamespace(set_noise_gate_db=Mock())
        self.signal = -30.0
        self.pitch = (55.0, .95)
        self.instances.append(self)

    def get_signal_db(self):
        return self.signal

    def get_tuner_data(self):
        return self.pitch

    def get_notes(self):
        return []


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    pygame.font.init()
    Capture.instances = []
    monkeypatch.setattr(check_menu, 'AudioCapture', Capture)
    monkeypatch.setattr(calibration_menu, 'AudioCapture', Capture)
    monkeypatch.setattr(Config, 'save', Mock())
    devices = [{'index': i, 'name': f'Input {i} ' + 'Very long audio input name ' * 30,
                'hostapi': 'Test API', 'sample_rate': 48000, 'latency_ms': 10}
               for i in range(100)]
    monkeypatch.setattr(device_menu, 'list_audio_devices', lambda: devices)
    monkeypatch.setattr(check_menu, 'list_audio_devices', lambda: devices)
    monkeypatch.setattr('pickhero.audio.input.best_input_device', lambda: 0)
    set_look('studio')
    yield
    set_look('studio')


def key(screen, code, mod=0):
    return screen.handle_event(pygame.event.Event(pygame.KEYDOWN, key=code, mod=mod))


def click(screen, action):
    return screen.handle_event(pygame.event.Event(
        pygame.MOUSEBUTTONDOWN, button=1, pos=screen._ui.buttons[action].center))


def make_screen(kind, instrument=None):
    cfg = Config(instrument='bass_ukulele')
    if kind == 'device':
        return device_menu.DeviceMenuScreen(cfg)
    if kind.startswith('check'):
        screen = check_menu.CheckScreen(cfg, instrument=instrument)
        screen._phase = 'measuring' if kind == 'check_measure' else 'live'
        if kind == 'check_error':
            screen._error = 'Device disconnected: ' + 'long error detail ' * 40
        return screen
    screen = calibration_menu.CalibrationMenuScreen(cfg, instrument=instrument)
    screen._state = kind.removeprefix('tuner_')
    if kind == 'tuner_error':
        screen._state = 'intro'
        screen._error = 'Device busy: ' + 'long error detail ' * 40
    if kind == 'tuner_confirm':
        midi = screen._tuning[screen._current_string]
        screen._results[screen._current_string] = StringCalibration(midi, midi_to_freq(midi), -70)
    return screen


KINDS = ['device', 'check_live', 'check_measure', 'check_error', 'tuner_intro',
         'tuner_noise', 'tuner_waiting', 'tuner_listen', 'tuner_confirm', 'tuner_done', 'tuner_error']


@pytest.mark.parametrize('look', ['studio', 'stage', 'hifi'])
@pytest.mark.parametrize('size', [(320, 240), (320, 480), (640, 480), (876, 706), (1280, 720)])
@pytest.mark.parametrize('kind', KINDS)
def test_every_state_keeps_all_actions_inside_window_and_content_scrollable(look, size, kind):
    set_look(look)
    screen = make_screen(kind)
    surface = pygame.Surface(size)
    screen.render(surface)
    viewport = screen._ui.viewport
    buttons = list(screen._ui.buttons.values())
    assert surface.get_rect().contains(viewport)
    assert viewport.height >= 60
    assert all(surface.get_rect().contains(rect) for rect in buttons)
    assert all(not viewport.colliderect(rect) for rect in buttons)
    assert all(not first.colliderect(second) for i, first in enumerate(buttons) for second in buttons[i+1:])
    if screen._ui.content_height > viewport.height:
        before = screen._ui.scroll
        screen.handle_event(pygame.event.Event(pygame.MOUSEWHEEL, y=-2, x=0))
        screen.render(surface)
        assert screen._ui.scroll > before
    getattr(screen, 'close', lambda: None)()


@pytest.mark.parametrize('look', ['studio', 'stage', 'hifi'])
@pytest.mark.parametrize('instrument', [BASS_UKULELE, GUITAR, with_tuning(GUITAR, {
    1: 64, 2: 59, 3: 55, 4: 50, 5: 45, 6: 40, 7: 35, 8: 30})])
@pytest.mark.parametrize('kind', ['check_live', 'tuner_waiting', 'tuner_done'])
def test_last_string_remains_reachable_with_four_six_and_eight_strings(look, instrument, kind):
    set_look(look)
    screen = make_screen(kind, instrument)
    surface = pygame.Surface((320, 480))
    screen.render(surface)
    assert len(screen._instrument.tuning) == len(instrument.tuning)
    key(screen, pygame.K_END)
    screen.render(surface)
    assert screen._ui.scroll + screen._ui.viewport.height == screen._ui.content_height
    key(screen, pygame.K_HOME)
    screen.render(surface)
    assert screen._ui.scroll == 0
    getattr(screen, 'close', lambda: None)()


@pytest.mark.parametrize('kind', ['device', 'check_live', 'tuner_intro', 'tuner_confirm', 'tuner_error'])
def test_tab_and_shift_tab_reach_every_visible_action(kind):
    screen = make_screen(kind)
    surface = pygame.Surface((320, 240))
    screen.render(surface)
    start = screen._ui.focus
    reached = set()
    for _ in screen._ui.order:
        key(screen, pygame.K_TAB)
        reached.add(screen._ui.focus)
    assert reached == set(screen._ui.order)
    assert screen._ui.focus == start
    key(screen, pygame.K_TAB, pygame.KMOD_SHIFT)
    previous = screen._ui.focus
    key(screen, pygame.K_TAB)
    assert screen._ui.focus == start and previous != start
    getattr(screen, 'close', lambda: None)()


def test_device_navigation_resize_and_wheel_never_lose_rows_or_click_outside_clip():
    screen = make_screen('device')
    surface = pygame.Surface((640, 480))
    screen.render(surface)
    large_capacity = screen._visible_items
    key(screen, pygame.K_END)
    screen.render(surface)
    assert screen._selected == 100
    assert 100 in [index for rect, index in screen._row_rects]
    for rect, _ in screen._row_rects:
        assert screen._ui.viewport.contains(rect)
    small = pygame.Surface((320, 240))
    screen.render(small)
    assert screen._visible_items < large_capacity
    assert 100 in [index for rect, index in screen._row_rects]
    screen.handle_event(pygame.event.Event(pygame.MOUSEWHEEL, y=5, x=0))
    screen.render(small)
    assert screen._selected == 100 and 100 not in [i for r, i in screen._row_rects]
    key(screen, pygame.K_HOME)
    screen.render(small)
    assert screen._selected == 0 and screen._ui.scroll == 0
    key(screen, pygame.K_PAGEDOWN)
    screen.render(small)
    assert screen._selected == screen._visible_items
    screen._config.save.assert_not_called()


@pytest.mark.parametrize('action,returned', [('apply', 'selected'), ('check', 'check'), ('calibrate', 'calibrate')])
def test_device_apply_and_continue_apply_current_selection(action, returned):
    screen = make_screen('device')
    surface = pygame.Surface((640, 480))
    screen.render(surface)
    row = next(rect for rect, index in screen._row_rects if index == 1)
    assert screen.handle_event(pygame.event.Event(pygame.MOUSEBUTTONDOWN, button=1, pos=row.center)) is None
    assert screen._selected == 1
    screen._config.save.assert_not_called()
    assert click(screen, action) == returned
    assert screen._config.audio.device_index == 0
    screen._config.save.assert_called_once()


def test_device_cancel_leaves_configuration_untouched():
    screen = make_screen('device')
    key(screen, pygame.K_END)
    assert key(screen, pygame.K_ESCAPE) == 'back'
    assert screen._config.audio.device_index is None
    screen._config.save.assert_not_called()


def test_failed_automatic_recommendation_keeps_manually_selectable_devices(monkeypatch):
    monkeypatch.setattr('pickhero.audio.input.best_input_device', Mock(side_effect=OSError('automatic unavailable')))
    screen = make_screen('device')
    assert len(screen._devices) == 100
    key(screen, pygame.K_DOWN)
    assert key(screen, pygame.K_RETURN) == 'selected'
    assert screen._config.audio.device_index == 0


@pytest.mark.parametrize('kind,action,returned', [('check_live', 'device', 'device'),
    ('check_live', 'calibrate', 'calibrate'), ('tuner_waiting', 'device', 'device'),
    ('tuner_waiting', 'check', 'check')])
def test_setup_navigation_closes_capture_once_and_returns_explicit_destination(kind, action, returned):
    screen = make_screen(kind)
    if screen._capture is None:
        screen._start_capture()
    capture = screen._capture
    screen.render(pygame.Surface((640, 480)))
    assert click(screen, action) == returned
    screen.close()
    capture.stop.assert_called_once()


@pytest.mark.parametrize('kind', ['check_live', 'tuner_intro'])
def test_drop_d_override_reaches_capture_and_display_without_changing_saved_profile(kind):
    drop_d = with_tuning(BASS_UKULELE, {1: 43, 2: 38, 3: 33, 4: 26})
    screen = make_screen(kind, drop_d)
    if kind == 'tuner_intro':
        key(screen, pygame.K_RETURN)
    assert screen._capture.instrument is drop_d
    assert screen._instrument.tuning[4] == 26
    assert screen._config.get_instrument().tuning[4] == 28
    screen._config.save.assert_not_called()
    screen.close()


def test_check_mouse_gate_controls_keep_safety_bounds_and_match_keyboard():
    screen = make_screen('check_live')
    screen.render(pygame.Surface((320, 240)))
    screen._gate_db = check_menu.GATE_MIN_DB
    click(screen, 'gate_down')
    assert screen._gate_db == check_menu.GATE_MIN_DB
    click(screen, 'gate_up')
    assert screen._gate_db == check_menu.GATE_MIN_DB + 1
    key(screen, pygame.K_DOWN)
    assert screen._gate_db == check_menu.GATE_MIN_DB
    screen._gate_db = check_menu.GATE_MAX_DB
    click(screen, 'gate_up')
    assert screen._gate_db == check_menu.GATE_MAX_DB
    screen._capture.detector.set_noise_gate_db.assert_called_with(check_menu.GATE_MAX_DB)
    screen.close()


def test_check_buttons_measure_reset_and_keyboard_space_activate_same_action():
    screen = make_screen('check_live')
    screen.render(pygame.Surface((320, 480)))
    screen._confirmed = dict.fromkeys(screen._confirmed, True)
    screen._strikes = 8
    screen._ui.focus = 'reset'
    key(screen, pygame.K_SPACE)
    assert not any(screen._confirmed.values()) and screen._strikes == 0
    click(screen, 'measure')
    assert screen._phase == 'measuring'
    screen.close()


@pytest.mark.parametrize('kind', ['check_live', 'tuner_intro'])
def test_start_failure_cleans_capture_and_visible_retry_recovers(monkeypatch, kind):
    failed = Capture(Config())
    failed.start.side_effect = OSError('device busy')
    healthy = Capture(Config())
    module = check_menu if kind.startswith('check') else calibration_menu
    monkeypatch.setattr(module, 'AudioCapture', Mock(side_effect=[failed, healthy]))
    screen = make_screen(kind)
    if kind == 'tuner_intro':
        key(screen, pygame.K_RETURN)
    assert screen._capture is None and 'device busy' in screen._error
    failed.stop.assert_called_once()
    screen.render(pygame.Surface((320, 240)))
    assert 'retry' in screen._ui.buttons
    click(screen, 'retry')
    assert screen._error is None and screen._capture is healthy
    healthy.start.assert_called_once()
    screen.close()
    healthy.stop.assert_called_once()


def test_tuner_start_accept_retry_and_complete_are_mouse_accessible():
    screen = make_screen('tuner_intro')
    surface = pygame.Surface((640, 480))
    screen.render(surface)
    click(screen, 'start')
    assert screen._state == 'noise'
    capture = screen._capture
    screen._state = 'confirm'
    screen._results[screen._current_string] = StringCalibration(28, midi_to_freq(28), -70)
    screen.render(surface)
    click(screen, 'retry')
    assert screen._state == 'waiting'
    for _ in screen._string_order:
        screen._state = 'confirm'
        screen.render(surface)
        screen._ui.focus = 'accept'
        key(screen, pygame.K_SPACE)
    assert screen._state == 'done'
    screen.render(surface)
    assert click(screen, 'save') == 'complete'
    capture.stop.assert_called_once()


def test_hifi_uses_dark_ink_for_active_tuning_labels(monkeypatch):
    set_look('hifi')
    screen = make_screen('tuner_waiting')
    screen._capture = Capture(screen._config)
    drawn = []
    real_text = secondary.text
    def record(surface, value, rect, **kwargs):
        drawn.append((value, kwargs.get('colour')))
        return real_text(surface, value, rect, **kwargs)
    monkeypatch.setattr(secondary, 'text', record)
    screen.render(pygame.Surface((876, 706)))
    assert ('A1', get_look().ink_1) in drawn
    screen.close()


def test_layouts_have_distinct_compositions_at_desktop_size():
    rectangles = []
    for look in ('studio', 'stage', 'hifi'):
        set_look(look)
        screen = make_screen('check_live')
        screen.render(pygame.Surface((1280, 720)))
        rectangles.append(tuple(screen._ui.viewport))
        screen.close()
    assert len(set(rectangles)) == 3
