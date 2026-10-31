"""An explicitly enabled, in-memory recording session across practice screens."""
from __future__ import annotations

import time
from uuid import uuid4

import pygame

from pickhero.i18n import t
from pickhero.ui.colors import get_look
from pickhero.ui.secondary import SetupView, text

MAX_SERIES_SECONDS = 30 * 60
MAX_SINGLE_SECONDS = 3 * 60
BANNER_HEIGHT = 32


class RecordingSeries:
    def __init__(self):
        self.label = None
        self.identifier = None
        self.deadline = 0.
        self.files = 0
        self.recorder = None
        self.last_take = None
        self.notice = None
        self.stop_button = None
        self.single_piece = False

    @property
    def visible(self):
        return self.label is not None or self.notice is not None

    def start(self, label, *, single_piece=False):
        if label not in ('acoustic', 'amplified', 'direct'):
            raise ValueError('Unknown recording setup')
        self.end()
        if self.last_take is not None and not self.last_take.done.is_set():
            self.notice = 'error'
            return False
        self.label = label
        self.single_piece = single_piece
        self.identifier = uuid4().hex
        self.deadline = time.monotonic() + (MAX_SINGLE_SECONDS if single_piece else MAX_SERIES_SECONDS)
        self.files = 0
        self.notice = None
        self.last_take = None
        return True

    def end(self, reason=None):
        self.label = None
        recorder, self.recorder = self.recorder, None
        if recorder is not None:
            recorder.stop(reason=reason or 'series_stopped')
        self.notice = reason
        self.stop_button = None

    def update(self):
        if self.label is not None and self.last_take is not None and (self.last_take.error or self.last_take.problem):
            self.end('limit' if self.last_take.problem == 'duration_limit' else 'error')
        elif self.label is not None and time.monotonic() >= self.deadline:
            self.end('expired')

    def handle_event(self, event, *, searching=False):
        if not self.visible:
            return False
        stop = event.type == pygame.KEYDOWN and event.key == pygame.K_u and not searching
        if event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
            stop = self.stop_button is not None and self.stop_button.collidepoint(event.pos)
        if stop:
            self.end()
        return stop

    def draw(self, surface):
        """App reserves this band; it never covers a screen's controls."""
        if not self.visible:
            self.stop_button = None
            return
        look = get_look()
        width, height = surface.get_size()
        band = pygame.Rect(0, max(0, height-BANNER_HEIGHT), width, min(height, BANNER_HEIGHT))
        pygame.draw.rect(surface, look.panel_left, band)
        pygame.draw.line(surface, look.hud_accent, band.topleft, band.topright)
        self.stop_button = pygame.Rect(max(0, width-115), band.y+3, min(110, width), 26)
        SetupView().button(surface, self.stop_button,
                           t('record.series_stop' if self.label else 'record.series_dismiss'))
        prefix = 'record.single_' if self.single_piece else 'record.series_'
        if self.notice:
            label = t(prefix + self.notice)
        else:
            active = self.recorder is not None and self.recorder._attached
            label = t(prefix + ('live' if active else 'ready'))
            if width >= 650:
                label += ' · ' + t('record.setup_' + self.label)
                label += ' · ' + t('record.series_files', count=self.files)
        text(surface, label, (8, band.y+3, max(1, self.stop_button.x-16), 26),
             size=13, colour=look.hud_accent if self.label else look.feedback_miss)
