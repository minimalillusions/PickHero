"""A guided route through existing input tools, without opening audio itself."""
from __future__ import annotations

from dataclasses import asdict
import pygame

from pickhero.audio.note_utils import midi_to_name
from pickhero.i18n import available_languages, current_language, t
from pickhero.ui.colors import get_look
from pickhero.ui.practice_panels import Action, PracticePanel


def _next_language_name():
    """The language L switches to, in its own name: "English", "Deutsch".

    The classic switcher: whoever is stuck in a language they cannot read
    finds their own language's name, not a word in the one they cannot read.
    It is also short enough for one line in a 320 px window, which a
    "Sprache · Language: Deutsch" label was not.
    """
    languages = available_languages()
    codes = [code for code, _ in languages]
    if len(codes) < 2:
        return dict(languages).get(current_language(), current_language())
    current = current_language()
    following = codes[(codes.index(current) + 1) % len(codes)] if current in codes else codes[0]
    return dict(languages)[following]


class SetupGuideScreen:
    STAGES = ("instrument", "device", "level", "tune", "phrase")

    def __init__(self, config, *, instrument=None, can_change_instrument=True):
        self.config = config
        self.instrument = instrument or config.get_instrument()
        self.can_change_instrument = can_change_instrument
        self.stage = 0
        self.reports = {}
        self.device_selected = False
        self._settings = self._settings_key()
        self._panel = self._make_panel()

    def _settings_key(self):
        return (self.instrument.key, tuple(sorted(self.instrument.tuning.items())),
                repr(asdict(self.config.audio)), repr(self.config.calibration))

    def _invalidate_changed_settings(self):
        current = self._settings_key()
        if current != self._settings:
            self.reports.clear()
            self._settings = current

    def _make_panel(self):
        name = self.STAGES[self.stage]
        target = midi_to_name(min(self.instrument.tuning.values()))
        instructions = t(f"setup.{name}.instruction", note=target)
        if name == "instrument" and not self.can_change_instrument:
            instructions = t("setup.instrument.current_song")
        sections = [(t(f"setup.{name}.title"), [instructions])]
        status = t("setup.not_checked")
        if name == "device":
            status = t("setup.device.selected" if self.device_selected else "setup.device.pending")
        elif name in self.reports:
            report = self.reports[name]
            if report.get("error") or not report.get("reliable"):
                status = t("setup.input_problem")
            elif name == "level":
                status = t("setup.level.seen" if report.get("has_signal") else "setup.level.silent")
            elif name == "tune":
                status = t("setup.tune.seen", count=report.get("confirmed", 0),
                           total=self.instrument.string_count)
            else:
                status = t("setup.phrase.seen", count=report.get("target_hits", 0), note=target)
        if name != "instrument":
            sections.append((t("setup.observation"), [status]))
        sections.append((t("setup.route"), [t("setup.route_instruction")]))
        actions = []
        if self.stage:
            actions.append(Action("previous", t("setup.previous"), pygame.K_LEFT))
        if name == "instrument":
            if self.can_change_instrument:
                actions.append(Action("instrument", t("setup.change_instrument"), pygame.K_i))
                # On the first screen, not a stage of its own: whoever cannot
                # read this page has to find the way out of it before
                # anything else.
                actions.append(Action("language", t("setup.change_language",
                                                    name=_next_language_name()), pygame.K_l))
        else:
            actions.append(Action("open", t(f"setup.{name}.open"), pygame.K_o))
        actions.append(Action("next", t("setup.finish" if self.stage == 4 else "setup.next"), pygame.K_RIGHT))
        actions.append(Action("back", t("setup.return"), pygame.K_ESCAPE))
        panel = PracticePanel("setup_guide", t("setup.title", current=self.stage + 1),
                              self.instrument.describe(), sections, actions)
        panel.focus = next(i for i, action in enumerate(actions)
                           if action.name == ("next" if name == "instrument" else "open"))
        return panel

    def refresh(self):
        self._invalidate_changed_settings()
        self._panel = self._make_panel()

    def instrument_changed(self, instrument):
        self.instrument = instrument
        self.reports.clear()
        self.device_selected = False
        self.refresh()

    def device_changed(self):
        self.reports.clear()
        self.device_selected = True
        self.refresh()

    def record_check(self, purpose, report):
        if purpose not in ("level", "tune", "phrase"):
            return
        self._invalidate_changed_settings()
        # A report is only an observation from this visit, not a certificate.
        if purpose == "level":
            self.reports.pop("tune", None)
            self.reports.pop("phrase", None)
        elif purpose == "tune":
            self.reports.pop("phrase", None)
        self.reports[purpose] = dict(report)
        self.refresh()

    def handle_event(self, event):
        panel = self._panel
        if 0 < panel.body.height < 84:
            delta = None
            amount = max(1, panel.body.height // 2)
            if event.type == pygame.MOUSEWHEEL:
                delta = -event.y * amount
            elif event.type == pygame.KEYDOWN and event.key in (
                    pygame.K_UP, pygame.K_DOWN, pygame.K_PAGEUP, pygame.K_PAGEDOWN):
                delta = amount * (-1 if event.key in (pygame.K_UP, pygame.K_PAGEUP) else 1)
            elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
                for name, direction in (("scroll_up", -1), ("scroll_down", 1)):
                    if name in panel.hit and panel.hit[name].collidepoint(event.pos):
                        delta = direction * amount
                        break
            if delta is not None:
                panel.scroll = max(0, min(panel.max_scroll, panel.scroll + delta))
                return None
        action = panel.handle_event(event)
        if action in ("close", "back"):
            return "back"
        if action == "instrument" and self.can_change_instrument:
            return "instrument"
        if action == "language" and self.can_change_instrument:
            return "language"
        if action == "open":
            return self.STAGES[self.stage]
        if action == "previous":
            self.stage = max(0, self.stage - 1)
            self.refresh()
        elif action == "next":
            if self.stage == 4:
                return "back"
            self.stage += 1
            self.refresh()
        return None

    def render(self, surface):
        surface.fill(get_look().bg)
        self._panel.render(surface)
