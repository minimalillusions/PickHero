"""Instrument calibration screen.

Guides the user through calibrating each open string. Measures the noise floor
then collects pitch samples, thickest string first. Results are stored in
Config.calibration for octave correction in the detector.

The string order and the expected notes come from the active instrument
profile. They used to be hardcoded to a 6-string guitar, so on a 4-string bass
ukulele the wizard asked for two strings that do not exist and named the wrong
target note for the four that do.
"""

from __future__ import annotations

import math
import statistics
import time

import pygame

from pickhero.audio.input import AudioCapture
from pickhero.audio.note_utils import freq_to_midi, midi_to_freq, midi_to_name
from pickhero.config import Config, StringCalibration, log_error
from pickhero.i18n import t
from pickhero.ui import design, secondary
from pickhero.ui.colors import get_look


# Collection time per string (seconds)
COLLECT_SECONDS = 3.0
# Noise floor measurement time (seconds)
NOISE_SECONDS = 2.0

# Minimum confidence for a sample to count. This used to be a hardcoded 0.5,
# which is a number from the aubio YIN days and means nothing to the harmonic
# detector. Measured on a real bass ukulele recording, frames carrying the
# correct pitch: the low E cleared 0.5 in 3 of 206 frames, so the wizard sat
# in its waiting state forever on exactly the string that needs it most. The
# other three strings cleared it often enough to look like it worked.
# The instrument profile already states its own threshold; use that.
MIN_CONFIDENCE_FALLBACK = 0.5

# Signal must exceed noise floor by this many dB to trigger start
SPIKE_THRESHOLD_DB = 12.0
# Cooldown after entering waiting state before accepting signal (seconds)
WAITING_COOLDOWN = 0.6
# Give up waiting for a pluck after this long and say so, rather than sitting
# on a screen that never changes.
WAITING_TIMEOUT = 20.0


def _get_font(name: str, size: int) -> pygame.font.Font:
    """A font, from the one cache the app has.

    This used to call pygame.font.SysFont on every call, and every call sits
    inside a draw method. The help overlay built four fonts per frame and cost
    12.5 ms of a 16.7 ms budget on a fast machine; on the low-end hardware
    this app is for, that is a dropped frame every frame it is open.
    """
    return design.font(size, mono=name.lower() in ("consolas", "courier new",
                                                   "monospace"))


class CalibrationMenuScreen:
    """Step-by-step calibration UI, driven by the instrument profile."""

    def __init__(self, config: Config, *, instrument=None):
        self._config = config
        self._instrument = instrument or config.get_instrument()
        self._ui = secondary.SetupView()
        self._rendered_state = None
        self._tuning = dict(self._instrument.tuning)
        self._min_confidence = (
            self._instrument.confidence_threshold
            if self._instrument.confidence_threshold is not None
            else MIN_CONFIDENCE_FALLBACK
        )
        self._timed_out = False
        self._error: str | None = None
        # Thickest (lowest) string first, as players expect when tuning.
        self._string_order = self._instrument.strings_low_to_high()
        self._capture: AudioCapture | None = None
        self._state = "intro"  # intro, noise, waiting, listen, confirm, done
        self._string_idx = 0  # index into self._string_order
        self._start_time: float = 0.0
        self._waiting_start: float = 0.0

        # Noise floor
        self._noise_samples: list[float] = []
        self._noise_floor_db: float = -80.0

        # Per-string collection
        self._freq_samples: list[tuple[float, float]] = []  # (freq, confidence)

        # Results per string number
        self._results: dict[int, StringCalibration] = {}

    @property
    def _current_string(self) -> int:
        """String number currently being calibrated."""
        return self._string_order[self._string_idx]

    @property
    def _current_note_name(self) -> str:
        """Expected open-string note name."""
        midi = self._tuning.get(self._current_string, 0)
        return midi_to_name(midi)

    def _primary_action(self):
        if self._error:
            return 'retry'
        return {'intro': 'start', 'confirm': 'accept', 'done': 'save'}.get(self._state, 'back')

    def _actions(self):
        primary = self._primary_action()
        labels = {'start': 'secondary.start', 'retry': 'secondary.retry',
                  'accept': 'secondary.accept', 'save': 'secondary.save_return'}
        actions = [(primary, t(labels[primary]))] if primary in labels else []
        if self._state in ('waiting', 'listen', 'confirm') and not self._error:
            actions.append(('retry', t('secondary.retry')))
        actions.extend([('check', t('menu.check')), ('device', t('menu.device')),
                        ('back', t('secondary.back'))])
        return actions

    def _activate(self, action):
        if action in ('back', 'check', 'device'):
            self._stop_capture()
            return action
        if action == 'save' and self._state == 'done' and not self._error:
            self._stop_capture()
            return 'complete'
        if self._error:
            if action in ('start', 'retry', 'accept') and self._start_capture():
                self._begin_noise()
            return None
        if action == 'start' and self._state == 'intro':
            if self._start_capture():
                self._begin_noise()
        elif action == 'retry' and self._state in ('waiting', 'listen', 'confirm'):
            self._begin_waiting()
        elif action == 'accept' and self._state == 'confirm':
            self._string_idx += 1
            if self._string_idx >= len(self._string_order):
                self._stop_capture()
                self._state = 'done'
            else:
                self._begin_waiting()
        return None

    def handle_event(self, event: pygame.event.Event) -> str | None:
        if event.type == pygame.KEYDOWN:
            shortcuts = {pygame.K_ESCAPE: 'back', pygame.K_d: 'device', pygame.K_c: 'check',
                         pygame.K_r: 'retry'}
            if event.key in shortcuts:
                return self._activate(shortcuts[event.key])
            if event.key in (pygame.K_UP, pygame.K_DOWN):
                self._ui.scroll += -48 if event.key == pygame.K_UP else 48
                self._ui.clamp()
                return None
        action = self._ui.event(event)
        if action:
            return self._activate(action)
        if event.type == pygame.KEYDOWN and event.key == pygame.K_RETURN:
            return self._activate(self._primary_action())
        return None

    def update(self) -> None:
        """Called each frame to advance calibration timers."""
        if self._capture is None:
            return
        # Calibration uses the live frequency, not note events. Drain those
        # events so leaving the tuner open cannot grow an unused queue.
        self._capture.get_notes()

        elapsed = time.perf_counter() - self._start_time

        if self._state == "noise":
            # Collect noise floor samples
            db = float(self._capture.get_signal_db())
            self._noise_samples.append(db)
            if elapsed >= NOISE_SECONDS:
                self._noise_floor_db = statistics.median(self._noise_samples) if self._noise_samples else -80.0
                self._begin_waiting()

        elif self._state == "waiting":
            # Wait for a strong pick — require cooldown + signal spike above noise floor
            wait_elapsed = time.perf_counter() - self._waiting_start
            if wait_elapsed < WAITING_COOLDOWN:
                return  # let previous string's sound fade
            db = float(self._capture.get_signal_db())
            freq, conf = self._capture.get_tuner_data()
            spike = db > self._noise_floor_db + SPIKE_THRESHOLD_DB
            if spike and freq > 0 and conf > self._min_confidence:
                self._timed_out = False
                self._begin_listen()
            elif wait_elapsed > WAITING_TIMEOUT:
                # Nothing usable arrived. Saying so beats a frozen screen.
                self._timed_out = True

        elif self._state == "listen":
            # Collect pitch samples
            freq, conf = self._capture.get_tuner_data()
            if freq > 0 and conf > self._min_confidence:
                self._freq_samples.append((float(freq), float(conf)))
            if elapsed >= COLLECT_SECONDS:
                self._finish_listen()

    # How far off still counts as in tune, and how far the needle can swing.
    IN_TUNE_CENTS = 8.0
    NEEDLE_RANGE_CENTS = 50.0

    def _heard_string(self):
        freq, confidence = (self._capture.get_tuner_data() if self._capture is not None else (0.0, 0.0))
        if freq <= 0 or confidence < self._min_confidence:
            return None, 0.0
        heard_midi = freq_to_midi(freq)
        best = min(self._tuning, key=lambda n: abs(self._tuning[n] - heard_midi))
        if abs(self._tuning[best] - heard_midi) > 1:
            return None, 0.0
        return best, 1200.0 * math.log2(freq / midi_to_freq(self._tuning[best]))

    def render(self, surface: pygame.Surface) -> None:
        look = get_look()
        state = (self._state, bool(self._error))
        if state != self._rendered_state:
            self._ui.scroll = 0
            self._ui.focus = self._primary_action()
            self._rendered_state = state
        subtitle = t('secondary.progress', current=min(self._string_idx + 1, len(self._string_order)),
                     total=len(self._string_order))
        viewport = self._ui.begin(surface, t('tuner.title', instrument=self._instrument.name), subtitle,
                                  self._actions(), primary=self._primary_action())
        row_h = 90 if look.key == 'hifi' else 74
        tuning_h = 44 + row_h * len(self._tuning)
        split = viewport.width >= (600 if look.key == 'stage' else 780)
        state_width = round((viewport.width - 10) * .43) - 8 if split else viewport.width - 10
        step = self._step_content()
        title, lines, progress = step
        text_width = state_width - 30
        state_h = max(245, 30 + secondary.paragraph_height(title, text_width, size=22) + 12
                      + sum(secondary.paragraph_height(line, text_width, size=15) + 8 for line in lines)
                      + (38 if progress is not None else 8))
        total_h = max(tuning_h, state_h) if split else state_h + 16 + tuning_h
        y = self._ui.content(total_h)
        full = pygame.Rect(viewport.x, y, viewport.width - 10, total_h)
        if split:
            width = round(full.width * .43)
            state_rect = pygame.Rect(full.x, y, width - 8, state_h)
            tuner_rect = pygame.Rect(full.x + width + 8, y, full.width - width - 8, tuning_h)
            if look.key == 'studio':
                tuner_rect = pygame.Rect(full.x, y, full.width - width - 8, tuning_h)
                state_rect = pygame.Rect(tuner_rect.right + 16, y, width - 8, state_h)
        else:
            state_rect = pygame.Rect(full.x, y, full.width, state_h)
            tuner_rect = pygame.Rect(full.x, state_rect.bottom + 16, full.width, tuning_h)
        clip = surface.get_clip(); surface.set_clip(viewport)
        self._text_area = state_rect.inflate(-28, 0)
        self._draw_step(surface, state_rect, step)
        self._draw_tuning_rows(surface, tuner_rect, row_h)
        surface.set_clip(clip)
        self._ui.finish(surface)

    def _step_content(self):
        """Use the same complete text to measure and draw a scrollable step."""
        if self._error:
            return t('tuner.mic_failed'), [self._error], None
        title, lines, progress = '', [], None
        if self._state == 'intro':
            title = t('secondary.ready')
            lines = [t('tuner.intro_strings', instrument=self._instrument.name)]
            lines.append(t('tuner.intro_helps_detection') if self._instrument.pitch_method == 'yin'
                         else t('tuner.intro_tuning_only'))
            if self._instrument.pitch_method != 'yin':
                lines.append(t('tuner.intro_not_needed'))
        elif self._state == 'noise':
            title = t('tuner.noise_wait')
            progress = (time.perf_counter() - self._start_time) / NOISE_SECONDS
            db = self._capture.get_signal_db() if self._capture else -80
            lines = [t('tuner.noise_level', db=int(db))]
        elif self._state in ('waiting', 'listen'):
            title = t('tuner.play_string', string=self._current_string, note=self._current_note_name)
            if self._state == 'waiting':
                lines = [t('tuner.nothing_heard') if self._timed_out else t('tuner.pluck_when_ready')]
                if self._capture:
                    db = self._capture.get_signal_db()
                    trigger = self._noise_floor_db + SPIKE_THRESHOLD_DB
                    if time.perf_counter() - self._waiting_start < WAITING_COOLDOWN:
                        lines.append(t('tuner.waiting_silence'))
                    else:
                        lines.append(t('tuner.signal_ok' if db > trigger else 'tuner.signal_low',
                                       db=int(db), trigger=int(trigger)))
            else:
                progress = (time.perf_counter() - self._start_time) / COLLECT_SECONDS
                lines = [t('tuner.samples', count=len(self._freq_samples)), t('secondary.listening')]
        elif self._state == 'confirm':
            cal = self._results.get(self._current_string)
            if cal:
                title = t('tuner.confirm_string', string=self._current_string, note=midi_to_name(cal.midi_note))
                cents = self.cents_off(self._current_string)
                expected = self._tuning[self._current_string]
                if cal.midi_note != expected:
                    lines = [t('tuner.wrong_string', note=midi_to_name(expected))]
                elif cents is not None:
                    key = 'tuner.confirm_in_tune' if abs(cents) <= 10 else 'tuner.confirm_sharp' if cents > 0 else 'tuner.confirm_flat'
                    lines = [t(key, freq=f'{cal.frequency:.1f}', cents=abs(round(cents)), count=abs(round(cents)))]
                else:
                    lines = [t('tuner.confirm_freq', freq=f'{cal.frequency:.1f}')]
            else:
                title = t('tuner.confirm_silent', string=self._current_string)
                lines = [t('tuner.confirm_silent_hint')]
        elif self._state == 'done':
            title = t('tuner.done_title')
            for number in self._string_order:
                cal = self._results.get(number)
                lines.append(t('tuner.summary_string', string=number, note=midi_to_name(cal.midi_note),
                               freq=f'{cal.frequency:.1f}') if cal else t('tuner.summary_missing', string=number))
        return title, lines, progress

    def _draw_step(self, surface, rect, content=None):
        look = get_look()
        secondary.panel(surface, rect, accent=True)
        x, width = rect.x + 15, rect.width - 30
        top = rect.y + 15
        title, lines, progress = content or self._step_content()
        title_h = secondary.paragraph(surface, title, (x, top, width, rect.height), size=22,
                                      colour=look.feedback_miss if self._error else look.ink_1)
        y = top + title_h + 12
        for line in lines:
            y += secondary.paragraph(surface, line, (x, y, width, rect.height), size=15) + 8
        if progress is not None:
            secondary.meter(surface, (x, rect.bottom - 24, width, 8), progress)

    def _draw_tuning_rows(self, surface, rect, row_h):
        look = get_look()
        heard, cents = self._heard_string()
        secondary.panel(surface, rect)
        secondary.text(surface, t('secondary.strings'), (rect.x + 15, rect.y + 8, rect.width - 30, 28),
                       size=18, bold=look.key != 'hifi')
        for index, number in enumerate(sorted(self._tuning)):
            row = pygame.Rect(rect.x + 14, rect.y + 44 + index * row_h, rect.width - 28, row_h - 4)
            live = number == heard
            midi = self._tuning[number]
            colour = design.string_colour(number)
            if look.key == 'hifi':
                colour = look.ink_1
            label_w = min(100, max(64, row.width // 3))
            secondary.text(surface, midi_to_name(midi), (row.x, row.y + 6, label_w, 30), size=25,
                           bold=True, colour=colour if live else look.ink_2)
            secondary.text(surface, t('tuner.target_hz', hz=f'{midi_to_freq(midi):.1f}'),
                           (row.x, row.y + 38, label_w, 22), size=12, colour=look.ink_3)
            rail = pygame.Rect(row.x + label_w + 5, row.y + 4, row.width - label_w - 7, row.height - 25)
            secondary.dial(surface, rail, cents, active=live, colour=colour)
            if live:
                status = t('tuner.in_tune') if abs(cents) <= self.IN_TUNE_CENTS else t(
                    'tuner.sharp' if cents > 0 else 'tuner.flat', cents=abs(round(cents)))
                secondary.text(surface, status, (rail.x, row.bottom - 21, rail.width, 20),
                               size=12, colour=colour, align='center')

    def get_results(self) -> dict[int, StringCalibration]:
        """Return calibration results (string_num → StringCalibration)."""
        return dict(self._results)

    # -- Internal helpers --

    def _start_capture(self) -> bool:
        """Start audio capture, or record why it failed.

        This ran unguarded inside the pygame event loop, so a microphone in
        use by another program, unplugged, or refusing the sample rate took
        the whole app down from a key press.
        """
        self._error = None
        try:
            if self._capture is None:
                self._capture = AudioCapture(self._config, instrument=self._instrument)
            self._capture.start()
            return True
        except Exception as exc:
            self._stop_capture()
            self._error = f"{type(exc).__name__}: {exc}"
            log_error(f"Kalibrierung: Audio nicht verfuegbar: {exc}")
            return False

    def close(self) -> None:
        """Release the microphone. Safe to call more than once."""
        self._stop_capture()

    def _stop_capture(self) -> None:
        """Stop audio capture."""
        if self._capture is not None:
            capture = self._capture
            self._capture = None
            try:
                capture.stop()
            except Exception as exc:
                log_error(f"Kalibrierung: Audio konnte nicht beendet werden: {exc}")

    def cents_off(self, string: int) -> float | None:
        """How far a calibrated string sits from its nominal pitch.

        This is the part of calibration a player can actually use. Rubber bass
        ukulele strings drift, and knowing that the A sits 15 cents sharp is
        worth more than any internal use the number has.
        """
        cal = self._results.get(string)
        target = self._tuning.get(string)
        if cal is None or target is None or cal.frequency <= 0:
            return None
        import math
        return 1200.0 * math.log2(cal.frequency / midi_to_freq(target))

    def _begin_noise(self) -> None:
        """Enter noise measurement state."""
        self._state = "noise"
        self._noise_samples = []
        self._start_time = time.perf_counter()

    def _begin_waiting(self) -> None:
        """Enter waiting state — show prompt, start collecting on first pick."""
        self._state = "waiting"
        self._timed_out = False
        self._results.pop(self._current_string, None)
        self._freq_samples = []
        self._waiting_start = time.perf_counter()

    def _begin_listen(self) -> None:
        """Enter listening state for current string (timer starts now)."""
        self._state = "listen"
        self._freq_samples = []
        self._start_time = time.perf_counter()

    def _finish_listen(self) -> None:
        """Process collected samples and move to confirm state."""
        string_num = self._current_string

        if self._freq_samples:
            # Keep only samples agreeing with the majority note, so one stray
            # octave reading cannot drag the median off the string.
            notes = [freq_to_midi(f) for f, _ in self._freq_samples]
            winner = max(set(notes), key=notes.count)
            freqs = [f for (f, _), n in zip(self._freq_samples, notes) if n == winner]
            median_freq = statistics.median(freqs)
            self._results[string_num] = StringCalibration(
                midi_note=winner,
                frequency=round(median_freq, 2),
                noise_floor_db=round(self._noise_floor_db, 1),
            )
        # If no samples were collected, _results won't have an entry for this string

        self._state = "confirm"
