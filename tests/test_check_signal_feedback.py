"""A microphone check explains sensitivity without silently changing it."""
from types import SimpleNamespace
from unittest.mock import Mock

import pygame
import pytest

from pickhero import i18n
from pickhero.config import Config
from pickhero.ui import check_menu, design, secondary
from pickhero.ui.colors import get_look, set_look


class Capture:
    def __init__(self, config, *, instrument):
        gate = config.audio.noise_gate_db
        self.detector = SimpleNamespace(
            noise_gate_db=instrument.noise_gate_db if gate is None else gate)
        self.detector.set_noise_gate_db = Mock(
            side_effect=lambda value: setattr(self.detector, 'noise_gate_db', value))
        self.signal = -38.0
        self.start = Mock()
        self.stop = Mock()

    def get_signal_db(self):
        return self.signal

    def get_tuner_data(self):
        return 55.0, .98

    def get_notes(self):
        return []


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    pygame.font.init()
    previous_look = get_look().key
    previous_language = i18n.current_language()
    monkeypatch.setattr(check_menu, 'AudioCapture', Capture)
    monkeypatch.setattr(check_menu, 'list_audio_devices', lambda: [])
    monkeypatch.setattr('pickhero.audio.input.best_input_device', lambda: None)
    monkeypatch.setattr(Config, 'save', Mock())
    i18n.set_language('de')
    yield
    set_look(previous_look)
    i18n.set_language(previous_language)


def screen_with_gate(gate=-33.0):
    config = Config(instrument='bass_ukulele')
    config.audio.noise_gate_db = gate
    return check_menu.CheckScreen(config)


@pytest.mark.parametrize('gate,level,verdict', [
    (-33.0, -38.0, 'quiet'),
    (-45.0, -38.0, 'good'),
    (-60.0, -50.0, 'good'),
    (-33.0, -33.0, 'good'),
    (-33.0, -2.0, 'hot'),
])
def test_level_verdict_matches_the_detector_gate_not_a_fixed_threshold(gate, level, verdict):
    screen = screen_with_gate(-33.0)
    # The detector is authoritative, even if a cached/config value differs.
    screen._capture.detector.noise_gate_db = gate
    screen._level_db = level
    text, colour = screen._signal_feedback()[0]
    assert text == i18n.t(f'check.level.{verdict}', db=f'{level:.0f}')
    assert colour == (get_look().feedback_hit if verdict == 'good' else get_look().feedback_miss)
    screen.close()


def test_missing_capture_uses_saved_gate_and_then_instrument_profile():
    screen = screen_with_gate(-33.0)
    screen.close()
    assert screen._active_gate_db() == -33.0
    screen._config.audio.noise_gate_db = None
    assert screen._active_gate_db() == screen._instrument.noise_gate_db


@pytest.mark.parametrize('gate,warning', [(-35.1, False), (-35.0, True), (-33.0, True)])
def test_high_gate_warning_explains_quiet_attacks_and_silent_room_measurement(gate, warning):
    screen = screen_with_gate(gate)
    values = [value for value, _ in screen._signal_feedback()]
    assert (i18n.t('check.signal.high_gate') in values) is warning
    assert i18n.t('check.signal.room_unknown', gate=f'{gate:.0f}') in values
    screen._room_floor = -47.0
    values = [value for value, _ in screen._signal_feedback()]
    assert i18n.t('check.signal.room', gate=f'{gate:.0f}', room='-47') in values
    screen.close()


@pytest.mark.parametrize('retry', [False, True])
def test_visiting_or_retrying_check_preserves_gate_even_after_three_seconds(monkeypatch, retry):
    clock = [100.0]
    monkeypatch.setattr(check_menu.time, 'perf_counter', lambda: clock[0])
    screen = screen_with_gate(-33.0)
    if retry:
        screen._activate('retry')
    clock[0] += check_menu.MEASURE_SECONDS + 1.0
    for _ in range(12):
        screen.update()
    assert screen._phase == 'live'
    assert screen._room_floor is None
    assert screen._room_samples == []
    assert screen._active_gate_db() == -33.0
    assert screen._config.audio.noise_gate_db == -33.0
    screen._config.save.assert_not_called()
    screen.close()


@pytest.mark.parametrize('trigger', ['keyboard', 'mouse'])
def test_explicit_room_measurement_still_updates_and_saves_gate(monkeypatch, trigger):
    clock = [100.0]
    monkeypatch.setattr(check_menu.time, 'perf_counter', lambda: clock[0])
    screen = screen_with_gate(-33.0)
    screen.render(pygame.Surface((320, 240)))
    event = (pygame.event.Event(pygame.KEYDOWN, key=pygame.K_m, mod=0)
             if trigger == 'keyboard' else pygame.event.Event(
                 pygame.MOUSEBUTTONDOWN, button=1, pos=screen._ui.buttons['measure'].center))
    screen.handle_event(event)
    assert screen._phase == 'measuring'
    screen._capture.signal = -56.0
    screen.update()
    clock[0] += check_menu.MEASURE_SECONDS + .1
    screen.update()
    assert screen._phase == 'live'
    assert screen._room_floor == -56.0
    assert screen._active_gate_db() == -56.0 + check_menu.GATE_MARGIN_DB
    assert screen._config.audio.noise_gate_db == screen._active_gate_db()
    screen._config.save.assert_called_once()
    screen.close()


@pytest.mark.parametrize('look', ['studio', 'stage', 'hifi'])
@pytest.mark.parametrize('language', ['de', 'en'])
@pytest.mark.parametrize('size', [(320, 240), (640, 480), (1280, 720)])
def test_full_feedback_wraps_and_can_be_reached_by_scrolling(monkeypatch, look, language, size):
    set_look(look)
    i18n.set_language(language)
    screen = screen_with_gate(-33.0)
    screen._level_db = -38.0
    screen._room_floor = -47.0
    surface = pygame.Surface(size)
    drawn = []
    panels = []
    original_text, original_panel = secondary.text, secondary.panel

    def record_text(surface, value, rect, **kwargs):
        drawn.append((value, pygame.Rect(rect), kwargs.get('size', 17)))
        return original_text(surface, value, rect, **kwargs)

    def record_panel(surface, rect, **kwargs):
        panels.append(pygame.Rect(rect))
        return original_panel(surface, rect, **kwargs)

    monkeypatch.setattr(secondary, 'text', record_text)
    monkeypatch.setattr(secondary, 'panel', record_panel)
    screen.render(surface)
    monitor = panels[0]
    expected = {line for lines, _ in screen._monitor_lines(monitor.width) for line in lines}
    feedback = [(value, rect, size) for value, rect, size in drawn if value in expected]
    assert {value for value, _, _ in feedback} == expected
    for value, rect, font_size in feedback:
        assert monitor.contains(rect), value
        assert design.font(font_size).size(value)[0] <= rect.width, value
    assert all(surface.get_rect().contains(rect) for rect in screen._ui.buttons.values())

    visible = set()
    # Check the actual rendered rectangles at successive wheel positions: the
    # complete warning remains reachable despite a very small viewport.
    for _ in range(50):
        visible.update(value for value, rect, _ in drawn
                       if value in expected and screen._ui.viewport.contains(rect))
        screen.handle_event(pygame.event.Event(pygame.MOUSEWHEEL, y=-1, x=0))
        drawn.clear()
        screen.render(surface)
    assert visible == expected
    screen.close()
