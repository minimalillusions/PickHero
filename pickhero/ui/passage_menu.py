"""Named practice passages with explicit editing, opening and deletion."""
from __future__ import annotations

from datetime import datetime

import pygame

from pickhero.i18n import t
from pickhero.ui.practice_panels import Action, PracticePanel


def _stamp(value):
    try:
        return datetime.fromisoformat(value).astimezone().strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return "—"


def _time(milliseconds):
    return f"{int(milliseconds // 60000)}:{int(milliseconds // 1000) % 60:02d}"


class PassagePanel(PracticePanel):
    def __init__(self, tracker, key, state, song_title, *, input_key=None):
        self.tracker, self.key, self.current = tracker, key, state
        self.song_title = song_title
        self.input_key = input_key
        self.mode = "list"
        self.selected = 0
        self.name = ""
        self.cursor = 0
        self.select_all = False
        self.error = ""
        self.renaming = False
        self._build()

    def _items(self):
        return self.tracker.saved_passages(self.key)

    def _selected(self):
        items = self._items()
        self.selected = max(0, min(self.selected, len(items) - 1))
        return items[self.selected] if items else None

    def _build(self):
        item = self._selected()
        sections = []
        if self.mode == "name":
            shown = self.name[:self.cursor] + "|" + self.name[self.cursor:]
            sections = [(t("passage.name"), [shown, t("passage.name_help")])]
            if self.error:
                sections.append((t("passage.error"), [t("passage.error." + self.error)]))
            actions = [Action("save", t("passage.save"), pygame.K_RETURN),
                       Action("cancel", t("passage.cancel"))]
        elif self.mode == "delete":
            sections = [(item["name"] if item else "", [t("passage.delete_hint")])]
            actions = [Action("cancel", t("passage.cancel")),
                       Action("confirm_delete", t("passage.confirm_delete"), pygame.K_DELETE)]
        else:
            if item:
                state = item["state"]
                sections.append((item["name"], [t("passage.position", start=_time(state["loop_start_ms"]),
                    end=_time(state["loop_end_ms"]), tempo=round(state["tempo_factor"] * 100),
                    track=state["track_index"] + 1), t("passage.filters",
                    strings=", ".join(str(i + 1) for i, active in enumerate(state["active_strings"]) if active),
                    fret=state["max_fret"], mode=t("passage.wait" if state["wait_mode"] else "passage.timed"))]))
                comparison = self.tracker.passage_comparison(self.key, item["id"], input_key=self.input_key)
                lines = []
                for index, result in enumerate(comparison):
                    label = t("passage.now" if index == len(comparison) - 1 else "passage.before")
                    lines.append(t("passage.comparison", label=label, date=_stamp(result["completed_at"]),
                        tempo=round(result["tempo_factor"] * 100), pitch=round(result["accuracy"]),
                        timing=(f'{result["timing_percent"]:.0f} %' if result["timing_assessed"] else "—")))
                sections.append((t("passage.compare_title"), lines or [t("passage.compare_empty")]))
                sections.append(("", [t("passage.compare_hint")]))
            else:
                sections = [("", [t("passage.empty")])]
            if self.mode == "detail":
                actions = [Action("play", t("passage.open"), pygame.K_o),
                           Action("rename", t("passage.rename"), pygame.K_r),
                           Action("delete", t("passage.delete"), pygame.K_DELETE),
                           Action("cancel", t("passage.back"))]
            else:
                sections.insert(0, ("", [t("passage.index", current=self.selected + 1 if item else 0,
                                           total=len(self._items()))]))
                actions = [Action("previous", t("passage.previous"), pygame.K_LEFT),
                           Action("next", t("passage.next"), pygame.K_RIGHT),
                           Action("details", t("passage.details"), pygame.K_RETURN),
                           Action("new", t("passage.new"), pygame.K_s),
                           Action("close", t("passage.close"))]
        if self.error and self.mode != "name":
            sections.insert(0, (t("passage.error"), [t("passage.error." + self.error)]))
        # Small panels move the subtitle to the first visible body line.
        # Keep the selected name there so arrow navigation remains legible.
        subtitle = item["name"] if item and self.mode != "name" else self.song_title
        super().__init__("passages", t("passage.title"), subtitle, sections, actions)
        if self.mode == "list":
            self.focus = 2 if item else 3

    def _edit(self, renaming):
        self.renaming = renaming
        self.error = ""
        item = self._selected()
        self.name = item["name"] if renaming and item else t("passage.default_name")
        self.cursor, self.select_all = len(self.name), True
        self.mode = "name"
        if pygame.get_init():
            pygame.key.start_text_input()
        self._build()

    def close(self):
        if pygame.get_init():
            pygame.key.stop_text_input()

    def handle_event(self, event):
        if self.mode == "name":
            changed = False
            if event.type == pygame.TEXTINPUT:
                incoming = "".join(c for c in event.text if c.isprintable())
                if self.select_all:
                    self.name, self.cursor, self.select_all = "", 0, False
                incoming = incoming[:60 - len(self.name)]
                self.name = self.name[:self.cursor] + incoming + self.name[self.cursor:]
                self.cursor += len(incoming)
                changed = True
            elif event.type == pygame.KEYDOWN:
                if event.key == pygame.K_a and getattr(event, "mod", 0) & pygame.KMOD_CTRL:
                    self.select_all = True
                    return None
                if event.key in (pygame.K_BACKSPACE, pygame.K_DELETE):
                    if self.select_all:
                        self.name, self.cursor, self.select_all = "", 0, False
                    elif event.key == pygame.K_BACKSPACE and self.cursor:
                        self.name = self.name[:self.cursor - 1] + self.name[self.cursor:]
                        self.cursor -= 1
                    elif event.key == pygame.K_DELETE:
                        self.name = self.name[:self.cursor] + self.name[self.cursor + 1:]
                    changed = True
                elif event.key in (pygame.K_LEFT, pygame.K_RIGHT, pygame.K_HOME, pygame.K_END):
                    self.select_all = False
                    self.cursor = (0 if event.key == pygame.K_HOME else len(self.name)
                                   if event.key == pygame.K_END else max(0, min(len(self.name), self.cursor
                                   + (-1 if event.key == pygame.K_LEFT else 1))))
                    changed = True
            if changed:
                self.error = ""
                self._build()
                return None
        # Half a viewport keeps text reachable even with five footer buttons.
        if 0 < self.body.height < 84:
            delta = None
            if event.type == pygame.MOUSEWHEEL:
                delta = -event.y
            elif event.type == pygame.KEYDOWN and event.key in (pygame.K_UP, pygame.K_DOWN,
                                                               pygame.K_PAGEUP, pygame.K_PAGEDOWN):
                delta = -1 if event.key in (pygame.K_UP, pygame.K_PAGEUP) else 1
            elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
                for name, direction in (("scroll_up", -1), ("scroll_down", 1)):
                    if name in self.hit and self.hit[name].collidepoint(event.pos):
                        delta = direction
                        break
            if delta is not None:
                self.scroll = max(0, min(self.max_scroll, self.scroll + delta * max(1, self.body.height // 2)))
                return None
        action = super().handle_event(event)
        if action in ("close", "cancel"):
            self.close()
            self.error = ""
            if self.mode == "list":
                return "close"
            self.mode = "detail" if self.mode == "delete" or (self.mode == "name" and self.renaming) else "list"
        elif action in ("previous", "next"):
            self.selected = max(0, min(len(self._items()) - 1, self.selected + (-1 if action == "previous" else 1)))
        elif action == "new":
            self._edit(False)
            return None
        elif action in ("details", "rename", "delete", "play"):
            item = self._selected()
            if not item:
                return None
            if action == "play":
                return "open_passage:" + item["id"]
            if action == "rename":
                self._edit(True)
                return None
            self.mode = "detail" if action == "details" else "delete"
        elif action == "save":
            try:
                if self.renaming:
                    self.tracker.rename_passage(self.key, self._selected()["id"], self.name)
                else:
                    self.tracker.save_passage(self.key, self.name, self.current)
                    self.selected = len(self._items()) - 1
            except ValueError as exc:
                self.error = str(exc) if str(exc) in ("state", "limit", "duplicate", "name", "write", "capacity") else "state"
                self._build()
                return None
            self.close()
            self.mode = "detail"
        elif action == "confirm_delete":
            item = self._selected()
            if item:
                try:
                    self.tracker.delete_passage(self.key, item["id"])
                except ValueError as exc:
                    self.error = str(exc) if str(exc) in ("write", "capacity") else "state"
                    self._build()
                    return None
            self.mode = "list"
        else:
            return action
        self._build()
        return None
