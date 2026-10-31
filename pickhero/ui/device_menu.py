"""Audio input selection with responsive rows and keyboard/mouse focus."""
from __future__ import annotations

import pygame

from pickhero.audio.input import list_audio_devices
from pickhero.config import Config, log_error
from pickhero.i18n import t
from pickhero.ui import secondary
from pickhero.ui.colors import get_look

VISIBLE_ITEMS = 14  # Compatibility for callers; actual capacity comes from the viewport.


class DeviceMenuScreen:
    def __init__(self, config: Config):
        self._config = config
        self._devices = []
        self._error = None
        self._recommended = None
        self._selected = 0
        self._scroll_offset = 0
        self._visible_items = VISIBLE_ITEMS
        self._row_rects = []
        self._row_h = 74
        self._reveal_selection = True
        self._ui = secondary.SetupView()
        self._refresh_devices()

    def _refresh_devices(self):
        try:
            self._devices = list_audio_devices()
            self._error = None
        except Exception as exc:
            self._devices = []
            self._recommended = None
            self._error = f"{type(exc).__name__}: {exc}"
            log_error(f"Geraeteliste nicht lesbar: {exc}")
        try:
            from pickhero.audio.input import best_input_device
            self._recommended = best_input_device()
        except Exception:
            self._recommended = None
        current = self._config.audio.device_index
        self._selected = next((i + 1 for i, d in enumerate(self._devices)
                               if d['index'] == current), 0)
        self._ui.scroll = 0
        self._ensure_visible()
        if self._error:
            self._reveal_selection = False

    @property
    def _item_count(self):
        return len(self._devices) + 1

    def _ensure_visible(self):
        self._reveal_selection = True
        if self._selected < self._scroll_offset:
            self._scroll_offset = self._selected
        elif self._selected >= self._scroll_offset + self._visible_items:
            self._scroll_offset = self._selected - self._visible_items + 1

    def _apply_selection(self):
        self._config.audio.device_index = (None if self._selected == 0
                                           else self._devices[self._selected - 1]['index'])
        self._config.save()

    def _activate(self, action):
        if action == 'back':
            return 'back'
        if action == 'refresh':
            self._refresh_devices()
        elif action in ('apply', 'devices', 'check', 'calibrate'):
            self._apply_selection()
            return action if action in ('check', 'calibrate') else 'selected'
        return None

    def handle_event(self, event):
        if event.type == pygame.KEYDOWN:
            if event.key == pygame.K_ESCAPE:
                return 'back'
            moves = {pygame.K_UP: -1, pygame.K_DOWN: 1,
                     pygame.K_PAGEUP: -self._visible_items,
                     pygame.K_PAGEDOWN: self._visible_items}
            if event.key in moves or event.key in (pygame.K_HOME, pygame.K_END):
                if event.key == pygame.K_HOME:
                    self._selected = 0
                elif event.key == pygame.K_END:
                    self._selected = self._item_count - 1
                else:
                    self._selected = max(0, min(self._item_count - 1, self._selected + moves[event.key]))
                self._ui.focus = 'devices'
                self._ui.keyboard = True
                self._ensure_visible()
                if event.key == pygame.K_HOME:
                    self._ui.scroll = 0
                    self._reveal_selection = False
                return None
            if event.key == pygame.K_r:
                return self._activate('refresh')
            if event.key == pygame.K_c:
                return self._activate('check')
            if event.key == pygame.K_g:
                return self._activate('calibrate')
        if event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
            for rect, index in self._row_rects:
                if rect.collidepoint(event.pos):
                    self._selected = index
                    self._ui.focus = 'devices'
                    self._ui.keyboard = False
                    self._ensure_visible()
                    return None
        action = self._ui.event(event, scroll_keys=False)
        if action == 'scroll':
            self._reveal_selection = False
        if action:
            return self._activate(action)
        if event.type == pygame.KEYDOWN and event.key in (pygame.K_RETURN, pygame.K_SPACE):
            return self._activate('apply')
        return None

    def render(self, surface):
        look = get_look()
        actions = [('apply', t('secondary.apply_return')), ('check', t('menu.check')),
                   ('calibrate', t('menu.tuner')), ('refresh', t('secondary.refresh')),
                   ('back', t('secondary.back'))]
        viewport = self._ui.begin(surface, t('device.title'), t('device.subtitle'), actions,
                                  primary='apply', list_focus=True)
        self._row_h = 88 if look.key == 'stage' else 78 if look.key == 'studio' else 84
        self._visible_items = max(1, viewport.height // self._row_h)
        error_text = t('secondary.device_error', error=self._error) if self._error else ''
        error_h = (secondary.paragraph_height(error_text, viewport.width - 30) + 24
                   if self._error else 0)
        start = error_h + self._selected * self._row_h
        reveal = (start, start + min(self._row_h, viewport.height)) if self._reveal_selection else None
        y = self._ui.content(error_h + self._item_count * self._row_h, reveal=reveal)
        self._scroll_offset = max(0, (self._ui.scroll - self._ui.intro_height - error_h) // self._row_h)
        self._row_rects = []
        clip = surface.get_clip()
        surface.set_clip(viewport)
        if self._error:
            secondary.paragraph(surface, error_text,
                                (viewport.x + 12, y + 10, viewport.width - 30, error_h - 24),
                                colour=look.feedback_miss)
        for index in range(max(0, self._scroll_offset), self._item_count):
            top = y + error_h + index * self._row_h
            if top >= viewport.bottom:
                break
            rect = pygame.Rect(viewport.x, top + 3, viewport.width - 10, self._row_h - 7)
            selected = index == self._selected
            secondary.panel(surface, rect, accent=selected)
            if selected and look.key == 'hifi':
                pygame.draw.rect(surface, look.select_fill, rect.inflate(0, -2), border_radius=4)
            visible = rect.clip(viewport)
            if visible.width and visible.height:
                self._row_rects.append((visible, index))
            left = rect.x + 15
            if look.key == 'hifi':
                pygame.draw.circle(surface, look.hud_accent if selected else look.track_empty,
                                   (left + 12, rect.centery), 12)
                left += 35
            name = t('device.auto') if index == 0 else str(self._devices[index - 1]['name'])
            current = (self._config.audio.device_index is None if index == 0 else
                       self._config.audio.device_index == self._devices[index - 1]['index'])
            title_w = rect.right - left - 16
            secondary.text(surface, name, (left, rect.y + 8, title_w, 26),
                           size=18, bold=selected, colour=look.ink_1)
            if index == 0:
                chosen = next((d for d in self._devices if d['index'] == self._recommended), None)
                details = (t('secondary.auto_device', name=chosen['name']) if chosen else
                           t('secondary.auto_unavailable'))
            else:
                dev = self._devices[index - 1]
                details = t('secondary.device_details', api=dev.get('hostapi') or '?',
                            rate=f"{dev.get('sample_rate', 0):.0f}",
                            latency=f"{dev.get('latency_ms', 0) or 0:.0f}")
            secondary.text(surface, details, (left, rect.y + 36, title_w, 22), size=13,
                           colour=look.ink_3)
            if current:
                secondary.text(surface, t('secondary.active'),
                               (left, rect.bottom - 20, title_w, 18), size=11, colour=look.hud_accent)
        surface.set_clip(clip)
        self._ui.finish(surface)
