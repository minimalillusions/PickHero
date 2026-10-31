"""Cancellable loading and explicit instrument-compatible track selection."""
from __future__ import annotations

from pathlib import Path

import pygame

from pickhero.i18n import t
from pickhero.ui import design, shapes
from pickhero.ui.colors import get_look
from pickhero.ui.secondary import SetupView, panel, paragraph, paragraph_height, text


class _ImportView(SetupView):
    """A content-sized dialog; all hit regions stay in window coordinates."""

    @staticmethod
    def dimensions(surface):
        small = surface.get_width() < 650 or surface.get_height() < 450
        margin, pad = (8, 12) if small else (24, 24)
        return min(720, surface.get_width() - margin * 2), pad, margin

    def begin(self, surface, title, subtitle, actions, *, primary, content_height,
              background=False):
        look = get_look()
        width, pad, margin = self.dimensions(surface)
        inner = width - pad * 2
        compact = surface.get_height() < 450
        title_h, subtitle_h = (28, 20) if compact else (36, 24)
        header_h = title_h + subtitle_h + (8 if compact else 16)
        gap, button_h = 8, 38 if compact else 42
        columns = min(len(actions), max(1, (inner + gap) // 84))
        rows = (len(actions) + columns - 1) // columns
        footer_h = rows * button_h + (rows - 1) * gap + 34
        self._intro = (subtitle if len(subtitle) > 60 or
                       design.reading_font(14).size(subtitle)[0] > inner else "")
        self.intro_height = (paragraph_height(self._intro, inner - 24, size=14) + 12
                             if self._intro else 0)
        height = min(surface.get_height() - margin * 2,
                     pad * 2 + header_h + content_height + self.intro_height + footer_h)
        self.bounds = pygame.Rect(0, 0, width, height)
        self.bounds.center = surface.get_rect().center
        rect = self.bounds
        if not background:
            surface.fill(look.bg)
        else:
            veil = pygame.Surface(surface.get_size(), pygame.SRCALPHA)
            veil.fill(design.with_alpha(design.scrim(look.bg), 205))
            surface.blit(veil, (0, 0))

        if look.key == "stage":
            cut = 12
            corners = [(rect.left + cut, rect.top), rect.topright,
                       (rect.right, rect.bottom - cut), (rect.right - cut, rect.bottom),
                       rect.bottomleft, (rect.left, rect.top + cut)]
            shapes.polygon(surface, look.panel_right, corners)
            shapes.polygon(surface, look.hairline_loud, corners, 1)
            pygame.draw.line(surface, look.hud_accent,
                             (rect.left + cut, rect.top), (rect.left + 110, rect.top), 2)
        else:
            radius = 16 if look.key == "studio" else 4
            shapes.rounded_rect(surface, look.panel_right, rect, border_radius=radius)
            shapes.rounded_rect(surface, look.hairline_loud, rect, 1, border_radius=radius)
        left = rect.left + pad
        text(surface, title.upper() if look.key == "stage" else title,
             (left, rect.top + pad, inner, title_h), size=22 if compact else 27,
             bold=look.key != "hifi", colour=look.ink_1)
        if not self._intro:
            text(surface, subtitle, (left, rect.top + pad + title_h, inner, subtitle_h),
                 size=13 if compact else 14, colour=look.ink_3)
        body_top = rect.top + pad + header_h
        footer_top = rect.bottom - pad - footer_h
        self.viewport = pygame.Rect(left, body_top, inner, max(1, footer_top - body_top))
        self._surface = surface
        self.buttons = {}
        self.order = [name for name, _ in actions]
        if self.focus not in self.order:
            self.focus = primary
        pygame.draw.line(surface, look.hairline,
                         (left, footer_top + 8), (left + inner, footer_top + 8))
        widths = [max(112, design.reading_font(14, bold=name == primary).size(label)[0] + 32)
                  for name, label in actions]
        natural = rows == 1 and sum(widths) + gap * (len(actions) - 1) <= inner
        right_start = left + inner - sum(widths[1:]) - gap * max(0, len(actions) - 2)
        for index, (name, label) in enumerate(actions):
            if natural:
                x = left if index == 0 else right_start + sum(widths[1:index]) + gap * (index - 1)
                button = pygame.Rect(x, footer_top + 20, widths[index], button_h)
            else:
                cell_width = (inner - gap * (columns - 1)) // columns
                button = pygame.Rect(left + (index % columns) * (cell_width + gap),
                                     footer_top + 20 + (index // columns) * (button_h + gap),
                                     cell_width, button_h)
            self.buttons[name] = button
            self.button(surface, button, label, primary=name == primary,
                        focus=self.keyboard and self.focus == name)
        self.clamp()
        return self.viewport

    def finish(self, surface):
        super().finish(surface)
        look = get_look()
        label = t("import.track_navigation")
        text(surface, label, (self.bounds.left + 24, self.bounds.bottom - 23,
                             self.bounds.width - 48, 18), size=11, colour=look.ink_3)


def resume_description(state):
    """Readable saved choices, shared by library and track selection."""
    def stamp(ms):
        seconds = max(0, int(ms / 1000))
        return f"{seconds // 60}:{seconds % 60:02d}"
    mode = ("loop" if state.get("loop_enabled") else "song") + ("_wait" if state.get("wait_mode") else "_timing")
    lines = [
        t("library.resume_position", bar=state.get("measure", 0) + 1,
          position=stamp(state.get("position_ms", 0))),
        t("library.resume_track", track=state.get("track_index", 0) + 1),
        t("library.resume_settings", tempo=round(state.get("tempo_factor", 1) * 100),
          mode=t("library.mode_" + mode)),
    ]
    if state.get("loop_enabled"):
        lines.append(t("library.resume_loop", start=stamp(state.get("loop_start_ms") or 0),
                       end=stamp(state.get("loop_end_ms") or 0)))
    strings = state.get("active_strings", [])
    if strings and not all(strings):
        lines.append(t("library.resume_strings", strings=", ".join(str(i + 1) for i, active in enumerate(strings) if active)))
    if state.get("max_fret") is not None:
        lines.append(t("library.resume_frets", fret=state["max_fret"]))
    if state.get("completed"):
        lines.append(t("library.resume_completed"))
    return lines


class ImportScreen:
    def __init__(self, path, *, tracks=None, selected=0, resume_available=False):
        self.path = Path(path)
        self.tracks = tracks
        self.selected = next((i for i, row in enumerate(tracks or [])
                              if row["index"] == selected and row["compatible"]),
                             next((i for i, row in enumerate(tracks or []) if row["compatible"]), 0))
        self.status = {"current": 0, "total": 0, "name": self.path.name}
        self.resume_available = resume_available
        self.resume_state = None
        self.view = _ImportView()
        self.rows = []
        self._reveal = True

    @property
    def track_index(self):
        if self.tracks and self.tracks[self.selected]["compatible"]:
            return self.tracks[self.selected]["index"]
        return None

    def handle_event(self, event):
        if event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE:
            return "cancel"
        if self.tracks is not None:
            if event.type == pygame.KEYDOWN and event.key in (pygame.K_UP, pygame.K_DOWN,
                    pygame.K_HOME, pygame.K_END, pygame.K_PAGEUP, pygame.K_PAGEDOWN):
                valid = [i for i, row in enumerate(self.tracks) if row["compatible"]]
                if valid:
                    position = valid.index(self.selected) if self.selected in valid else 0
                    step = {pygame.K_UP: -1, pygame.K_DOWN: 1, pygame.K_PAGEUP: -5,
                            pygame.K_PAGEDOWN: 5}.get(event.key, 0)
                    position = (0 if event.key == pygame.K_HOME else len(valid) - 1
                                if event.key == pygame.K_END else max(0, min(len(valid)-1, position+step)))
                    self.selected = valid[position]
                    self._reveal = True
                return "selection"
            if event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
                for rect, index in self.rows:
                    if rect.collidepoint(event.pos) and self.tracks[index]["compatible"]:
                        self.selected = index
                        return "selection"
        action = self.view.event(event, scroll_keys=self.tracks is None)
        if action == "start" and self.track_index is not None:
            return "start"
        if action == "resume" and self.resume_available:
            return "resume"
        return "cancel" if action == "cancel" else None

    def render(self, surface, *, background=False):
        look = get_look()
        completed = bool(self.resume_state and self.resume_state.get("completed"))
        actions = [("cancel", t("import.cancel"))]
        if self.tracks is not None and self.track_index is not None:
            actions.append(("start", t("import.start" if self.resume_available else "import.play")))
            if self.resume_available:
                actions.append(("resume", t("import.repeat_settings" if completed else "import.resume")))
        single = bool(self.tracks and len(self.tracks) == 1 and self.track_index is not None)
        title = t("import.ready" if single else
                  "import.tracks" if self.tracks is not None else "import.loading")
        width, pad, _ = self.view.dimensions(surface)
        content_width = width - pad * 2
        prose_width = content_width - 24
        choose = t("import.single" if single else "import.choose")
        choose_h = paragraph_height(choose, prose_width, size=14) + 16
        row_metrics = []
        for row in self.tracks or []:
            label = t("import.midi_track") if not row["strings"] else t("import.strings", count=row["strings"])
            label += " · " + t("import.compatible" if row["compatible"] else "import.incompatible")
            name_h = paragraph_height(row["name"], content_width - 86, size=18)
            label_h = paragraph_height(label, content_width - 86, size=13)
            row_metrics.append((label, name_h, label_h, max(76, name_h + label_h + 30)))
        resume_lines = resume_description(self.resume_state) if self.resume_state else []
        if resume_lines:
            # The selected row already names the track. A completed run has
            # no meaningful resume bar: its saved timestamp is the song end.
            resume_lines = ([] if completed else resume_lines[:1]) + resume_lines[2:]
            if completed:
                resume_lines[-1] = t("import.completed_help")
        resume_heights = [paragraph_height(line, content_width - 40, size=14) + 4
                          for line in resume_lines]
        resume_h = 48 + sum(resume_heights) if resume_lines else 0
        message = t("library.preparing_score") if self.status.get("phase") == "prepare" else t("import.wait")
        message_h = paragraph_height(message, prose_width)
        tracks_h = sum(item[3] + 8 for item in row_metrics)
        height = (message_h + 58 if self.tracks is None else
                  choose_h + tracks_h + (resume_h + 8 if resume_h else 0))
        area = self.view.begin(surface, title, self.path.name, actions,
                               primary="start" if len(actions) > 1 else "cancel",
                               content_height=height, background=background)
        self.rows = []
        selected_top = choose_h + sum(item[3] + 8 for item in row_metrics[:self.selected])
        reveal = ((selected_top, selected_top + min(row_metrics[self.selected][3], area.height))
                  if self._reveal and self.tracks else None)
        y = self.view.content(height, reveal=reveal)
        self._reveal = False
        clip = surface.get_clip()
        surface.set_clip(clip.clip(area))
        if self.tracks is None:
            paragraph(surface, message, (area.x+12, y, prose_width, message_h))
            done, total = self.status.get("current", 0), self.status.get("total", 0)
            text(surface, t("import.progress", current=done, total=total) if total else t("import.preparing"),
                 (area.x+12, y+message_h+14, prose_width, 28), colour=look.hud_accent)
        else:
            paragraph(surface, choose, (area.x+8, y, prose_width, choose_h), size=14)
            row_y = y + choose_h
            for index, (row, metrics) in enumerate(zip(self.tracks, row_metrics)):
                label, name_h, label_h, row_h = metrics
                box = pygame.Rect(area.x, row_y, area.width-8, row_h)
                row_y += row_h + 8
                visible = box.clip(area)
                if visible.height:
                    self.rows.append((visible, index))
                if not box.colliderect(area):
                    continue
                selected = index == self.selected
                if look.key == "stage":
                    shapes.rounded_rect(surface, look.select_fill if selected else look.panel_left,
                                        box, border_radius=4)
                    pygame.draw.line(surface, look.hud_accent if selected else look.hairline,
                                     box.topleft, box.bottomleft, 3 if selected else 1)
                else:
                    panel(surface, box, accent=selected)
                    if selected and look.key == "hifi":
                        pygame.draw.line(surface, look.hud_accent, box.topleft, box.bottomleft, 3)
                colour = look.ink_1 if row["compatible"] else look.ink_3
                text(surface, f"{row['index']+1:02d}", (box.x+12, box.y+12, 30, 25),
                     size=15, colour=look.hud_accent if selected else look.ink_3)
                paragraph(surface, row["name"], (box.x+50, box.y+12, box.width-78, name_h),
                          size=18, colour=colour)
                paragraph(surface, label, (box.x+50, box.y+16+name_h, box.width-78, label_h),
                          size=13, colour=look.ink_2 if row["compatible"] else look.ink_3)
                if selected:
                    cx, cy = box.right - 14, box.top + 23
                    pygame.draw.aalines(surface, look.hud_accent, False,
                                       [(cx-4, cy), (cx-1, cy+3), (cx+5, cy-4)])
            if resume_lines:
                box = pygame.Rect(area.x, row_y + 8, area.width - 8, resume_h)
                shapes.rounded_rect(surface, look.select_fill, box, border_radius=8 if look.key == "studio" else 3)
                text(surface, t("import.completed" if completed else "import.saved"),
                     (box.x+16, box.y+10, box.width-32, 26), size=16, bold=True, colour=look.ink_1)
                resume_y = box.y + 40
                for label, label_h in zip(resume_lines, resume_heights):
                    paragraph(surface, label, (box.x+16, resume_y, box.width-32, label_h),
                              size=14, colour=look.ink_2)
                    resume_y += label_h
        surface.set_clip(clip)
        self.view.finish(surface)
