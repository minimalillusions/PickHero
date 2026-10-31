"""Live input check.

Answers one question, string by string: does the app hear you?

Everything else in this app is inference. If a song scores badly you cannot
tell whether the microphone is too quiet, the wrong device is selected, the
instrument profile is wrong, or you simply played the wrong note. This screen
removes the guessing: it shows the input level with a verdict in plain words,
the note it is hearing right now, and a checklist that ticks off each open
string as soon as it has been recognised cleanly.
"""

from __future__ import annotations

import math
import time
from numbers import Real

import pygame

from pickhero.audio.detector import GATE_MARGIN_DB, GATE_MAX_DB, GATE_MIN_DB
from pickhero.audio.input import AudioCapture, list_audio_devices
from pickhero.audio.note_utils import (
    freq_to_cents_deviation,
    midi_to_freq,
    midi_to_name,
)
from pickhero.config import Config
from pickhero.i18n import t
from pickhero.ui import design, secondary
from pickhero.ui.colors import get_look
from pickhero.ui.practice_panels import wrap

# How long to listen to the room before deciding where the gate belongs.
MEASURE_SECONDS = 3.0

# A string counts as recognised after this many frames within TOLERANCE_CENTS.
FRAMES_TO_CONFIRM = 6
TOLERANCE_CENTS = 60.0
# A high RMS level risks clipping; quietness depends on the active noise gate.
HOT_DB = -3.0
HIGH_GATE_DB = -35.0


def _get_font(name: str, size: int) -> pygame.font.Font:
    """A font, from the one cache the app has.

    This used to call pygame.font.SysFont on every call, and every call sits
    inside a draw method. The help overlay built four fonts per frame and cost
    12.5 ms of a 16.7 ms budget on a fast machine; on the low-end hardware
    this app is for, that is a dropped frame every frame it is open.
    """
    return design.font(size, mono=name.lower() in ("consolas", "courier new",
                                                   "monospace"))


class CheckScreen:
    """Live monitor: level, detected pitch, and a per-string checklist."""

    def __init__(self, config: Config, *, instrument=None, purpose="check"):
        self._config = config
        self._instrument = instrument or config.get_instrument()
        self._purpose = purpose
        self._target_hits = 0
        self._last_target_ms = None
        self._ui = secondary.SetupView()
        self._capture: AudioCapture | None = None
        self._device_label = self._describe_device()

        self._level_db = -120.0
        self._level_peak = -120.0
        self._freq = 0.0
        self._confidence = 0.0

        # string number -> consecutive frames seen in tune
        self._streak: dict[int, int] = {s: 0 for s in self._instrument.tuning}
        self._confirmed: dict[int, bool] = {s: False for s in self._instrument.tuning}
        self._best_cents: dict[int, float] = {}
        self._strikes = 0
        self._last_strike_at = 0.0
        self._error: str | None = None
        self._health_fault: str | None = None

        # A check is a monitor, not permission to overwrite the saved gate.
        # Only the explicit room measurement asks for silence and changes it.
        self._phase = "live"
        self._measure_until = 0.0
        self._room_samples: list[float] = []
        self._room_floor: float | None = None
        self._gate_db: float | None = None
        self._start_capture()

    def _begin_measurement(self) -> None:
        self._reset_observations()
        self._phase = "measuring"
        self._room_samples = []
        self._measure_until = time.perf_counter() + MEASURE_SECONDS

    def _finish_measurement(self) -> None:
        self._phase = "live"
        samples = sorted(v for v in self._room_samples if v > -119.0)
        if not samples:
            return
        # A high percentile of the quiet period, so an occasional creak does
        # not drag the gate up, but steady hum is accounted for.
        # 75th percentile, not the maximum: an occasional creak or a passing
        # car should not push the gate up so far that the instrument cannot
        # get through it. If it still ends up wrong, the arrow keys move it.
        floor = samples[int(len(samples) * 0.75)]
        self._room_floor = floor
        gate = min(GATE_MAX_DB, max(GATE_MIN_DB, floor + GATE_MARGIN_DB))
        self._gate_db = gate
        self._config.audio.noise_gate_db = gate
        try:
            self._config.save()
        except Exception:
            pass
        if self._capture is not None:
            self._capture.detector.set_noise_gate_db(gate)
        self._reset_observations()

    # ── setup ───────────────────────────────────────────────────────────

    def _describe_device(self) -> str:
        """Name the microphone the capture will actually open.

        With no device set, this named the Windows default. The capture does
        not use the default: it picks the lowest-latency host API for the
        device, which on Windows is usually a different entry for the same
        microphone and sometimes a different microphone entirely. So the
        screen that exists to tell you which microphone is being heard was
        naming one and listening to another.
        """
        index = self._config.audio.device_index
        try:
            devices = list_audio_devices()
        except Exception:
            return t("check.device.unknown")
        if index is None:
            try:
                from pickhero.audio.input import best_input_device
                index = best_input_device()
            except Exception:
                index = None
        if index is None:
            for dev in devices:
                if dev.get("is_default"):
                    return t("check.device.system_named", name=dev["name"])
            return t("check.device.system_default")
        for dev in devices:
            if dev["index"] == index:
                # No index in the config means the app picked this one itself,
                # so the marker says so instead of being glued on as a suffix.
                if self._config.audio.device_index is not None:
                    return str(dev["name"])
                return t("check.device.auto", name=dev["name"])
        return t("check.device.missing", index=index)

    def _reset_observations(self) -> None:
        """Require fresh evidence after an explicit change of sensitivity."""
        self._streak = {s: 0 for s in self._instrument.tuning}
        self._confirmed = {s: False for s in self._instrument.tuning}
        self._best_cents = {}
        self._strikes = self._target_hits = 0
        self._last_target_ms = None
        self._last_strike_at = 0.0
        self._level_db = self._level_peak = -120.0
        self._freq = self._confidence = 0.0
        if self._capture is not None:
            self._capture.get_notes()

    def _observe_health(self) -> None:
        """Keep a recovered fault from certifying this whole visit as clean."""
        check = getattr(self._capture, "health_issue", None)
        if self._health_fault is not None or not callable(check):
            return
        try:
            self._health_fault = check()
        except Exception:
            self._health_fault = "input_disconnected"

    def _start_capture(self) -> None:
        self._error = None
        self._health_fault = None
        self._reset_observations()
        try:
            self._capture = AudioCapture(self._config, instrument=self._instrument)
            self._capture.start()
            # Include faults between UI frames, even when the visit lasts
            # longer than AudioCapture's default recent-event window.
            set_window = getattr(self._capture, "set_health_window", None)
            if callable(set_window):
                set_window(0.0)
        except Exception as exc:
            self._error = f"{type(exc).__name__}: {exc}"
            self._stop_capture()

    def _stop_capture(self) -> None:
        if self._capture is not None:
            try:
                self._capture.stop()
            except Exception:
                pass
            self._capture = None
        # Without this the meter keeps showing the last level it saw, so a
        # stopped microphone looks like a live one sitting at a steady signal.
        self._level_db = -120.0
        self._level_peak = -120.0
        self._freq = 0.0
        self._confidence = 0.0

    def close(self) -> None:
        """Release the microphone. Safe to call more than once."""
        self._stop_capture()

    # ── loop ────────────────────────────────────────────────────────────

    def _actions(self):
        if self._error:
            return [('retry', t('secondary.retry')), ('device', t('menu.device')),
                    ('calibrate', t('menu.tuner')), ('back', t('secondary.back'))]
        return [('back', t('secondary.back')), ('device', t('menu.device')),
                ('timing', t('timing.title')),
                ('calibrate', t('menu.tuner')), ('measure', t('secondary.measure')),
                ('reset', t('secondary.reset')), ('gate_down', t('secondary.gate_down')),
                ('gate_up', t('secondary.gate_up'))]

    def _activate(self, action):
        if action == 'timing':
            # App snapshots this capture before releasing it for the probe.
            return action
        if action in ('back', 'device', 'calibrate'):
            self._stop_capture()
            return action
        if action == 'retry':
            self._stop_capture()
            self._start_capture()
            self._phase = 'live'
        elif action == 'measure':
            self._begin_measurement()
        elif action == 'gate_up':
            self._nudge_gate(1.0)
        elif action == 'gate_down':
            self._nudge_gate(-1.0)
        elif action == 'reset':
            self._reset_observations()
        return None

    def handle_event(self, event: pygame.event.Event) -> str | None:
        if event.type == pygame.MOUSEWHEEL:
            # A fixed 48-pixel step skips complete lines when the smallest
            # window leaves only about 60 pixels for this scrollable monitor.
            step = min(48, max(12, self._ui.viewport.height // 2))
            self._ui.scroll -= int(event.y) * step
            self._ui.clamp()
            return None
        if event.type == pygame.KEYDOWN:
            shortcuts = {pygame.K_ESCAPE: 'back', pygame.K_d: 'device', pygame.K_g: 'calibrate',
                         pygame.K_F3: 'timing',
                         pygame.K_m: 'measure', pygame.K_r: 'retry' if self._error else 'reset',
                         pygame.K_UP: 'gate_up', pygame.K_PLUS: 'gate_up', pygame.K_KP_PLUS: 'gate_up',
                         pygame.K_DOWN: 'gate_down', pygame.K_MINUS: 'gate_down', pygame.K_KP_MINUS: 'gate_down'}
            if event.key in shortcuts:
                return self._activate(shortcuts[event.key])
        action = self._ui.event(event)
        if action:
            return self._activate(action)
        if event.type == pygame.KEYDOWN and event.key in (pygame.K_RETURN, pygame.K_SPACE):
            return self._activate('retry' if self._error else 'back')
        return None

    def _nudge_gate(self, step: float) -> None:
        """Move the gate by hand and remember it.

        A measurement is a starting point, not an answer. If the room has an
        intermittent noise the gate may land too high and swallow the
        instrument, and the fastest fix is the person in the room watching the
        level and moving it.
        """
        current = self._gate_db
        if current is None:
            current = self._config.audio.noise_gate_db
        if current is None:
            current = self._instrument.noise_gate_db
        gate = min(GATE_MAX_DB, max(GATE_MIN_DB, current + step))
        self._gate_db = gate
        self._config.audio.noise_gate_db = gate
        try:
            self._config.save()
        except Exception:
            pass
        if self._capture is not None:
            self._capture.detector.set_noise_gate_db(gate)
        if gate != current:
            self._reset_observations()

    def update(self) -> None:
        if self._capture is None:
            return
        self._observe_health()
        self._level_db = self._capture.get_signal_db()
        if self._phase == "measuring":
            self._room_samples.append(self._level_db)
            self._capture.get_notes()          # keep the queue from growing
            if time.perf_counter() >= self._measure_until:
                self._finish_measurement()
            return
        self._level_peak = max(self._level_peak - 0.5, self._level_db)
        self._freq, self._confidence = self._capture.get_tuner_data()

        for ts_note in self._capture.get_notes():
            if ts_note.note.is_onset:
                self._strikes += 1
                self._last_strike_at = time.perf_counter()
                # The setup phrase requests separate, exact open-string
                # attacks. A ringing pitch or its octave cannot tick it off.
                if (ts_note.note.midi_note == min(self._instrument.tuning.values())
                        and ts_note.note.confidence >= (self._instrument.confidence_threshold or .5)
                        and (self._last_target_ms is None
                             or ts_note.timestamp_ms - self._last_target_ms >= 750)):
                    self._target_hits += 1
                    self._last_target_ms = ts_note.timestamp_ms

        target = self._nearest_string()
        tolerance = 15.0 if self._purpose == "tune" else TOLERANCE_CENTS
        for string in self._instrument.tuning:
            if target is not None and string == target[0] and abs(target[1]) <= tolerance:
                self._streak[string] += 1
                prev = self._best_cents.get(string)
                if prev is None or abs(target[1]) < abs(prev):
                    self._best_cents[string] = target[1]
                if self._streak[string] >= FRAMES_TO_CONFIRM:
                    self._confirmed[string] = True
            else:
                self._streak[string] = 0

    def _nearest_string(self) -> tuple[int, float] | None:
        """Which open string the current pitch is closest to, and by how much."""
        threshold = (self._instrument.confidence_threshold or .5) if self._purpose == "tune" else .10
        if not math.isfinite(self._freq) or self._freq <= 0 or self._confidence < threshold:
            return None
        midi, cents = freq_to_cents_deviation(self._freq)
        best = None
        for string, open_midi in self._instrument.tuning.items():
            # Accept the octave too: a weak fundamental can read an octave out,
            # and for a checklist that is still the right string.
            for shift in ((0,) if self._purpose == "tune" else (0, 12, -12)):
                if midi + shift == open_midi:
                    off = cents
                    if best is None or abs(off) < abs(best[1]):
                        best = (string, off)
        return best

    def diagnostic_snapshot(self) -> dict:
        """Read observations before closing; never write settings or progress."""
        self._observe_health()
        issue = self._health_fault
        reliable = (self._capture is not None and self._error is None
                    and issue is None and self._phase == "live")
        return {"error": self._error or issue, "reliable": reliable,
                "has_signal": self._level_peak > self._active_gate_db() and self._strikes > 0,
                "confirmed": sum(self._confirmed.values()), "target_hits": self._target_hits}

    # ── drawing ─────────────────────────────────────────────────────────

    def _active_gate_db(self) -> float:
        """Report the detector's threshold, including before room measurement."""
        detector = getattr(self._capture, 'detector', None)
        gate = getattr(detector, 'noise_gate_db', None)
        if isinstance(gate, Real):
            return float(gate)
        for gate in (self._gate_db, self._config.audio.noise_gate_db,
                     self._instrument.noise_gate_db):
            if gate is not None:
                return float(gate)
        raise ValueError('Instrument has no noise gate')

    def _signal_feedback(self) -> list[tuple[str, tuple[int, int, int]]]:
        """Explain level and threshold without claiming that pitch is correct."""
        look = get_look()
        gate = self._active_gate_db()
        if self._level_db > HOT_DB:
            verdict, colour = 'check.level.hot', look.feedback_miss
        elif self._level_db < gate:
            verdict, colour = 'check.level.quiet', look.feedback_miss
        else:
            verdict, colour = 'check.level.good', look.feedback_hit
        rows = [(t(verdict, db=f'{self._level_db:.0f}'), colour)]
        room_key = ('check.signal.room' if self._room_floor is not None
                    else 'check.signal.room_unknown')
        rows.append((t(room_key, gate=f'{gate:.0f}',
                       room=f'{self._room_floor:.0f}' if self._room_floor is not None else ''),
                     look.ink_2))
        if gate >= HIGH_GATE_DB:
            rows.append((t('check.signal.high_gate'), look.feedback_miss))
        rows.append((t('check.strikes', count=self._strikes), look.ink_3))
        return rows

    def _monitor_lines(self, width: int):
        face = design.reading_font(14)
        return [(wrap(value, face, max(1, width - 32)), colour)
                for value, colour in self._signal_feedback()]

    def _monitor_height(self, width: int) -> int:
        line_h = design.reading_font(14).get_linesize() + 3
        return 184 + sum(len(lines) * line_h + 8 for lines, _ in self._monitor_lines(width)) + 8

    def _checklist_summary(self):
        done = sum(self._confirmed.values())
        total = len(self._confirmed)
        key = 'check.all_found' if done == total else 'check.some_found' if done else 'check.none_found'
        return t(key, count=done, total=total)

    def _checklist_height(self, width):
        head_h = secondary.paragraph_height(t('check.checklist.head'), width - 28, size=14)
        summary_h = secondary.paragraph_height(self._checklist_summary(), width - 28)
        return 40 + head_h + 12 + 64 * len(self._instrument.tuning) + 5 + summary_h + 16

    def render(self, surface: pygame.Surface) -> None:
        look = get_look()
        subtitle = t('check.instrument', name=self._instrument.describe())
        title = t('check.title')
        if self._purpose == "tune":
            title = t("setup.tune.title")
            subtitle = t("setup.tune.live_instruction")
        elif self._purpose == "phrase":
            title = t("setup.phrase.live_title", count=self._target_hits)
            subtitle = t("setup.phrase.live_instruction", note=midi_to_name(min(self._instrument.tuning.values())))
        viewport = self._ui.begin(surface, title, subtitle, self._actions(),
                                  primary='retry' if self._error else 'back')
        split = look.key == 'stage' and viewport.width >= 560
        split = split or (look.key == 'studio' and viewport.width >= 780)
        full_width = viewport.width - 10
        split_width = round(full_width * (.42 if look.key == 'stage' else .50))
        monitor_h = self._monitor_height(split_width - 8 if split else full_width)
        rows_h = self._checklist_height(full_width - split_width - 8 if split else full_width)
        content_h = max(monitor_h, rows_h) if split else monitor_h + 16 + rows_h
        if self._error:
            content_h = max(210, secondary.paragraph_height(
                t('check.audio_failed', error=self._error), full_width - 28) + 24)
        elif self._phase == 'measuring':
            message = t('check.measuring.quiet') + ' ' + t('check.measuring.hint')
            content_h = max(210, 104 + secondary.paragraph_height(message, full_width - 32) + 42)
        y = self._ui.content(content_h)
        clip = surface.get_clip(); surface.set_clip(viewport)
        full = pygame.Rect(viewport.x, y, viewport.width - 10, content_h)
        if self._error:
            secondary.panel(surface, full, accent=True)
            secondary.paragraph(surface, t('check.audio_failed', error=self._error),
                                full.inflate(-28, -24), colour=look.feedback_miss)
        elif self._phase == 'measuring':
            secondary.panel(surface, full, accent=True)
            left = max(0, self._measure_until - time.perf_counter())
            secondary.text(surface, f'{left:.0f}', (full.x + 16, full.y + 18, full.width - 32, 70),
                           size=54, bold=True, colour=look.hud_accent, align='center')
            secondary.paragraph(surface, t('check.measuring.quiet') + ' ' + t('check.measuring.hint'),
                                (full.x + 16, full.y + 104, full.width - 32, full.height - 146))
            secondary.meter(surface, (full.x + 18, full.bottom - 22, full.width - 36, 7),
                            1 - left / MEASURE_SECONDS)
        else:
            if split:
                width = split_width
                monitor = pygame.Rect(full.x, y, width - 8, monitor_h)
                checklist = pygame.Rect(full.x + width + 8, y, full.width - width - 8, rows_h)
            else:
                monitor = pygame.Rect(full.x, y, full.width, monitor_h)
                checklist = pygame.Rect(full.x, y + monitor_h + 16, full.width, rows_h)
            self._draw_monitor(surface, monitor)
            self._draw_strings(surface, checklist)
        surface.set_clip(clip)
        self._ui.finish(surface)

    def _draw_monitor(self, surface, rect):
        look = get_look()
        secondary.panel(surface, rect, accent=look.key == 'stage')
        pad = 16
        x, width = rect.x + pad, rect.width - pad * 2
        secondary.text(surface, self._device_label, (x, rect.y + 9, width, 24), size=14, colour=look.ink_3)
        active = self._freq > 0 and self._confidence >= .10
        midi, cents = freq_to_cents_deviation(self._freq) if active else (0, 0)
        name = midi_to_name(midi) if active else '--'
        target = self._nearest_string()
        tint = design.string_colour(target[0]) if target is not None else look.hud_accent
        if look.key == 'hifi':
            secondary.dial(surface, (x + width // 2, rect.y + 44, width // 2, 76), cents,
                           active=active, colour=look.ink_1)
            secondary.text(surface, name, (x, rect.y + 40, width // 2, 76), size=46, colour=look.ink_1)
        elif look.key == 'stage':
            secondary.text(surface, t('secondary.heard').upper(), (x, rect.y + 37, width, 20), size=12)
            secondary.text(surface, name, (x, rect.y + 59, width, 57), size=43, bold=True,
                           colour=tint, align='center')
        else:
            secondary.text(surface, t('secondary.heard'), (x, rect.y + 38, width, 22), size=13)
            secondary.text(surface, name, (x, rect.y + 61, width, 58), size=44, bold=True, colour=tint)
        detail = (t('check.pitch.detail', hz=f'{self._freq:.1f}', cents=f'{cents:+.0f}')
                  if active else t('check.pitch.none'))
        secondary.text(surface, detail, (x, rect.y + 121, width, 24), size=14)
        gate = self._active_gate_db()
        level_colour = self._signal_feedback()[0][1]
        secondary.meter(surface, (x, rect.y + 161, width, 13), (self._level_db + 90) / 90,
                        colour=level_colour, target=(gate + 90) / 90)
        line_h = design.reading_font(14).get_linesize() + 3
        y = rect.y + 184
        for lines, colour in self._monitor_lines(rect.width):
            for line in lines:
                secondary.text(surface, line, (x, y, width, line_h), size=14, colour=colour)
                y += line_h
            y += 8

    def _draw_strings(self, surface, rect):
        look = get_look()
        secondary.panel(surface, rect)
        secondary.text(surface, t('secondary.strings'),
                       (rect.x + 14, rect.y + 9, rect.width - 28, 28), size=17, bold=True)
        head_h = secondary.paragraph(surface, t('check.checklist.head'),
                                     (rect.x + 14, rect.y + 40, rect.width - 28, rect.height), size=14)
        y = rect.y + 40 + head_h + 12
        for number, midi in sorted(self._instrument.tuning.items()):
            tint = design.string_colour(number)
            if look.key == 'hifi':
                pygame.draw.circle(surface, tint if self._confirmed[number] else look.track_empty,
                                   (rect.x + 27, y + 24), 12)
            else:
                pygame.draw.rect(surface, tint, (rect.x + 15, y + 12, 4, 30), border_radius=2)
            x = rect.x + 48 if look.key == 'hifi' else rect.x + 31
            secondary.text(surface, f'{number}   {midi_to_name(midi)}   {midi_to_freq(midi):.1f} Hz',
                           (x, y + 1, rect.right - x - 14, 26), size=18, bold=True)
            key = ('check.string.found' if self._confirmed[number] else
                   'check.string.listening' if self._streak[number] else 'check.string.pending')
            status = t(key, cents=f'{self._best_cents.get(number, 0):+.0f}')
            secondary.text(surface, status, (x, y + 29, rect.right - x - 14, 22), size=13,
                           colour=look.feedback_hit if self._confirmed[number] else look.ink_3)
            y += 64
        secondary.paragraph(surface, self._checklist_summary(),
                            (rect.x + 14, y + 5, rect.width - 28, rect.bottom - y - 5))
