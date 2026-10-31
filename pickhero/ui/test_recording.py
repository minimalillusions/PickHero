"""Explicit takes survive pauses; normal practice never writes audio."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import os
from pathlib import Path
import time

import pygame

from pickhero import config as config_module
from pickhero.audio.recording import MAX_SECONDS, TestRecording, write_json
from pickhero.config import log_error
from pickhero.curriculum import LessonPractice
from pickhero.i18n import t
from pickhero.ui import design, shapes
from pickhero.ui.colors import get_look
from pickhero.ui.practice_panels import Action, PracticePanel
from pickhero.ui.recording_series import RecordingSeries


@dataclass(frozen=True)
class RecordingRequest:
    """Open the recording chooser for a piece; this never arms capture."""
    path: Path
    practice: LessonPractice | None = None


class PracticeRecording:
    def __init__(self, screen):
        self.screen = screen
        self.take = None
        self.pending = None
        self.error = None
        self._attached = False
        self._await_result = False
        self._last_state = None
        self.badge_rect = None
        self.series = RecordingSeries()
        self._series_choice = False
        self._series_capture = False
        self._count_in_only = False

    def panel(self):
        lines = [t('record.tempo', percent=round(self.screen._tempo_factor * 100)),
                 t('record.series_explain' if self._series_choice else 'record.explain'),
                 t('record.series_limits' if self._series_choice else 'record.stop_hint'), t('record.comparison')]
        if self.take:
            status = ('failed' if self.take.error else 'partial' if self.take.problem else
                      'saved' if self.take.done.is_set() else 'saving')
            lines += [t('record.' + status), str(self.take.folder)]
            if self._count_in_only:
                lines.append(t('record.count_in_only'))
        if self.error:
            lines.append(t('record.failed'))
        prefix = 'record:series:' if self._series_choice else 'record:'
        actions = [Action(prefix + 'acoustic', t('record.acoustic')),
                   Action(prefix + 'amplified', t('record.amplified')),
                   Action(prefix + 'direct', t('record.direct'))]
        if self.take:
            actions.append(Action('record:folder', t('record.folder')))
        panel = PracticePanel('test_recording', t('record.series_title' if self._series_choice else 'record.title'),
                              self.screen._timeline.metadata.title, [('', lines)], actions)
        panel.header_action = Action('record:mode', t('record.single_mode' if self._series_choice else 'record.series_mode'),
                                     pygame.K_t, '1 · T' if self._series_choice else 'T')
        return panel

    def open(self):
        if self.series.visible:
            self.series.end()
            return
        screen = self.screen
        screen._close_panel_resources()
        self.stop(reason='recording_menu')
        screen._pause_for_panel()
        screen._show_help = False
        screen._panel = self.panel()

    def action(self, action):
        if action == 'record':
            if self.series.visible:
                self.series.end()
            else:
                self.open()
            return True
        if not action or not action.startswith('record:'):
            return False
        label = action.split(':', 1)[1]
        if label == 'mode':
            self._series_choice = not self._series_choice
            self.screen._panel = self.panel()
            return True
        if label == 'folder':
            if self.take:
                try:
                    os.startfile(str(self.take.folder))
                except OSError as exc:
                    self.error = str(exc)
                    self.screen._panel = self.panel()
            return True
        series = label.startswith('series:')
        if series:
            label = label.split(':', 1)[1]
        if label not in ('acoustic', 'amplified', 'direct'):
            return True
        if self.take and not self.take.done.is_set():
            return True
        if not self.series.start(label, single_piece=not series):
            self.error = 'previous_recording_still_saving'
            self.screen._panel = self.panel()
            return True
        self.pending, self.error = label, None
        self.screen._panel = None
        self.screen._show_help = False
        self.screen._audio_enabled = True
        # Preserve the selected passage. Normal musical practice is a full take.
        self.screen._start_practice(self.screen._loop_start_ms or 0.0, force_count_in=True)
        if self.pending is not None:  # the input failed before recording could attach
            self.pending = None
            self.error = 'input_start_failed'
            if self.series.label:
                self.series.end('error')
            self.screen._panel = self.panel()
        return True

    def context(self):
        s = self.screen
        return dict(song_ms=s._playback_ms, tempo_factor=s._tempo_factor,
                    wait_mode=s._wait_mode, wait_frozen=s._wait_mode_frozen,
                    noise_gate_db=float(s._noise_gate_db), look=get_look().key,
                    timing_window_song_ms=s._timing_window_for_marker(),
                    audio_latency_offset_ms=s._config.audio_latency_offset_ms,
                    backing_enabled=not s._backing_muted, metronome=s._config.metronome,
                    active_strings=list(s._active_strings), max_fret=s._max_fret)

    def started(self):
        self.series.update()
        if self.pending is None and self.series.label is None:
            return
        label, self.pending = self.pending or self.series.label, None
        self._series_capture = self.series.label is not None
        previous = self.series.last_take if self._series_capture else self.take
        if previous and not previous.done.is_set():
            self.error = 'previous_recording_still_saving'
            self.series.end('error')
            return
        s, capture = self.screen, self.screen._audio_capture
        try:
            notes = s._timeline.notes
            metadata = dict(label=label, instrument=s._instrument.key,
                series_id=self.series.identifier if self._series_capture else None,
                series_segment=self.series.files + 1 if self._series_capture else None,
                recording_scope='piece' if self.series.single_piece else 'series',
                tuning=dict(s._tuning), title=s._timeline.metadata.title,
                song_key=s._song_key, track_index=s._track_index,
                intended_notes=[dict(asdict(note), scored=s._note_passes_filter(note)) for note in notes[:20000]],
                intended_notes_truncated=len(notes) > 20000,
                score_duration_ms=s._timeline.duration_ms, state=self.context(),
                audio=capture.timing_diagnostics(),
                input_fingerprint=s._input_key(), calibration=capture._calibration,
                detector=dict(buf_size=capture.detector.buf_size, hop_size=capture.detector.hop_size,
                              onset_latency_ms=capture.detector.onset_latency_ms,
                              confidence_threshold=capture.detector.confidence_threshold,
                              onset_threshold=capture.detector.onset_threshold))
            self.take = TestRecording(config_module.CONFIG_DIR / 'recordings', int(capture.sample_rate), metadata)
            self._await_result = False
            self._last_state = None
            self._count_in_only = False
            capture.test_recording = self.take
            self._attached = True
            if self._series_capture:
                self.series.files += 1
                self.series.recorder = self
                self.series.last_take = self.take
            log_error(f'Testaufnahme gestartet ({label}, {metadata["recording_scope"]}): {self.take.folder}')
        except Exception as exc:
            self.error = str(exc)
            if self._series_capture:
                self.series.end('error')
            log_error(f'Testaufnahme konnte nicht starten: {exc}')

    def anchor(self):
        if self._attached:
            s = self.screen
            self.take.event('clock_mapping', capture_ms=s._audio_anchor_capture_ms,
                            song_ms=s._matcher.audio_offset_ms, tempo_factor=s._tempo_factor)

    def observe(self):
        self.series.update()
        if self.series.label and self.screen._playing and not self.screen._audio_enabled:
            self.series.end('input_off')
        if not self._attached:
            return
        if time.perf_counter() - self.take.started_at >= MAX_SECONDS:
            self.take.problem = self.take.problem or 'duration_limit'
            self.take.accepting = False
        if not self.take.accepting:
            if self._series_capture:
                self.series.end('limit' if self.take.problem == 'duration_limit' else 'error')
            else:
                self.stop(reason=self.take.problem or 'error')
            return
        state = self.context()
        # Log control changes, not every rendered frame.
        comparison = {key: value for key, value in state.items() if key != 'song_ms'}
        if comparison != self._last_state:
            self.take.event('controls', capture_ms=self.screen._audio_capture.capture_elapsed_ms(), **state)
            self._last_state = comparison

    def stop(self, *, reached_end=False, reason='interrupted'):
        if not self._attached:
            if self.series.single_piece and (reached_end or reason == 'leave_song'):
                self.series.end()
            return
        s, capture = self.screen, self.screen._audio_capture
        capture.test_recording = None
        self._attached = False
        if self.series.recorder is self:
            self.series.recorder = None
        self._await_result = reached_end
        self._count_in_only = s._playback_ms < s._count_in_target_ms
        self.take.finish(dict(state=self.context(), audio=capture.timing_diagnostics(),
                              reached_end=self._await_result, reason='completed' if reached_end else reason,
                              ended_during_count_in=self._count_in_only, issues=list(s._attempt_issues)))
        log_error(f'Testaufnahme lokal: {self.take.folder}')
        # Keep consent across pauses, but never into another finished run
        # or piece. The series recorder has already been detached above.
        if self.series.single_piece and (reached_end or reason == 'leave_song'):
            self.series.end()

    def result(self, stats):
        if self._await_result and self.take:
            self._await_result = False
            try:
                # Separate file: the audio writer can finish independently.
                write_json(self.take.folder / 'result.json', stats)
            except (ValueError, TypeError, OSError) as exc:
                log_error(f'Testaufnahme: Ergebnis nicht gespeichert: {exc}')

    def draw(self, surface):
        self.badge_rect = None
        if not self.take and not self.error:
            return
        status = ('failed' if self.error or self.take.error else
                  'partial' if self.take.problem else 'live' if self._attached else
                  'paused' if self.series.label is not None else
                  'count_in_only' if self._count_in_only else
                  'saved' if self.take.done.is_set() else 'saving')
        text = t('record.badge_' + status)
        font = design.reading_font(13)
        width = min(surface.get_width() - 16, font.size(text)[0] + 20)
        rect = pygame.Rect(surface.get_width() - width - 8, 62, width, 26)
        controls = [box for boxes in self.screen._button_rects.values() for box in boxes]
        # Stage has several control rows at the top right. Place the badge
        # below those rows, without covering setup, help or tempo buttons.
        positions = sorted({62, *(box.bottom + 4 for box in controls if box.bottom >= 62)})
        for top in positions:
            candidate = rect.copy()
            candidate.top = top
            if surface.get_rect().contains(candidate) and not any(candidate.colliderect(box) for box in controls):
                rect = candidate
                break
        self.badge_rect = rect
        look = get_look()
        shapes.rounded_rect(surface, look.panel_right, rect, border_radius=6)
        colour = look.feedback_miss if status in ('live', 'failed', 'partial') else look.hud_accent
        surface.blit(font.render(text, True, colour), (rect.x + 10, rect.y + 4))
