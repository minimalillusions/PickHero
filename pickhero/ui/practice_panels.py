"""Responsive, modal practice panels with one mouse/keyboard action contract."""
from __future__ import annotations

from dataclasses import dataclass

import pygame

from pickhero.ui import design, shapes
from pickhero.ui.colors import get_look
from pickhero.ui.text_layout import wrap


@dataclass(frozen=True)
class Action:
    name: str
    label: str
    key: int | None = None
    compact_label: str | None = None


class PracticePanel:
    """The panel consumes events; only explicit actions reach PlayingScreen."""

    def __init__(self, kind: str, title: str, subtitle: str,
                 sections: list[tuple[str, list[str]]], actions: list[Action]):
        self.kind = kind
        self.title = title
        self.subtitle = subtitle
        self.sections = sections
        self.actions = actions
        self.header_action: Action | None = None
        self.metrics: list[tuple[str, str, str]] = []
        self.focus = 0
        self.scroll = 0
        self.max_scroll = 0
        self.hit: dict[str, pygame.Rect] = {}
        self.body = pygame.Rect(0, 0, 0, 0)
        self.bounds = pygame.Rect(0, 0, 0, 0)

    def handle_event(self, event) -> str | None:
        if event.type == pygame.MOUSEWHEEL:
            self.scroll = max(0, min(self.max_scroll, self.scroll - event.y * 42))
        elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
            for name, rect in self.hit.items():
                if rect.collidepoint(event.pos):
                    if name == "scroll_up":
                        self.scroll = max(0, self.scroll - max(42, self.body.height // 2))
                        return None
                    if name == "scroll_down":
                        self.scroll = min(self.max_scroll, self.scroll + max(42, self.body.height // 2))
                        return None
                    for index, action in enumerate(self.actions):
                        if action.name == name:
                            self.focus = index
                    return "close" if name == "header_close" else name
        elif event.type == pygame.KEYDOWN:
            key = event.key
            if self.header_action is not None and key == self.header_action.key:
                return self.header_action.name
            # An explicitly advertised action owns its key. Other panels
            # retain Home/End for reading their scrollable content.
            for action in self.actions:
                if action.key == key:
                    return action.name
            if key == pygame.K_TAB:
                step = -1 if getattr(event, "mod", 0) & pygame.KMOD_SHIFT else 1
                self.focus = (self.focus + step) % max(1, len(self.actions))
            elif key in (pygame.K_LEFT, pygame.K_RIGHT):
                self.focus = (self.focus + (-1 if key == pygame.K_LEFT else 1)) % max(1, len(self.actions))
            elif key in (pygame.K_UP, pygame.K_DOWN, pygame.K_PAGEUP, pygame.K_PAGEDOWN):
                step = 42 if key in (pygame.K_UP, pygame.K_DOWN) else max(42, self.body.height - 30)
                if key in (pygame.K_UP, pygame.K_PAGEUP):
                    step = -step
                self.scroll = max(0, min(self.max_scroll, self.scroll + step))
            elif key in (pygame.K_HOME, pygame.K_END):
                self.scroll = 0 if key == pygame.K_HOME else self.max_scroll
            elif key in (pygame.K_RETURN, pygame.K_KP_ENTER, pygame.K_SPACE):
                return self.actions[self.focus].name if self.actions else None
            elif key == pygame.K_ESCAPE or (key == pygame.K_h and self.kind == "help"):
                return "close"
        return None

    def render(self, surface: pygame.Surface) -> None:
        look = get_look()
        width, height = surface.get_size()
        small = width < 650 or height < 450
        compact = height < 360
        pad = 12 if small else (32 if look.key == "hifi" else 24)
        margin = 8 if small else {"studio": 30, "stage": 16, "hifi": 40}.get(look.key, 24)
        max_width = {"studio": 940, "stage": 1080, "hifi": 880}.get(look.key, 940)
        panel_width = max(80, min(max_width, width - 2 * margin))
        self.bounds = pygame.Rect((width - panel_width) // 2, margin,
                                  panel_width, max(120, height - 2 * margin))
        rect = self.bounds
        veil = pygame.Surface(surface.get_size(), pygame.SRCALPHA)
        veil.fill(design.with_alpha(design.scrim(look.bg), 215))
        surface.blit(veil, (0, 0))

        if look.key == "stage":
            points = [(rect.left + 15, rect.top), (rect.right, rect.top),
                      (rect.right - 15, rect.bottom), (rect.left, rect.bottom)]
            shapes.polygon(surface, look.panel_right, points)
            shapes.polygon(surface, look.hud_accent, points, 2)
            pygame.draw.line(surface, look.hud_accent, (rect.left + 16, rect.top + 7),
                             (rect.left + 16, rect.bottom - 7), 3)
            pad += 12
        else:
            radius = 16 if look.key == "studio" else 0
            shapes.rounded_rect(surface, look.popover if look.key == "studio" else look.panel_right,
                                rect, border_radius=radius)
            shapes.rounded_rect(surface, look.hairline_loud, rect, 1, border_radius=radius)

        self.hit.clear()
        close = pygame.Rect(rect.right - 42, rect.top + 8, 32, 32)
        self.hit["header_close"] = close
        pygame.draw.aaline(surface, look.ink_2, (close.left + 10, close.top + 10),
                           (close.right - 10, close.bottom - 10))
        pygame.draw.aaline(surface, look.ink_2, (close.right - 10, close.top + 10),
                           (close.left + 10, close.bottom - 10))

        left = rect.left + pad
        content_width = max(40, rect.width - 2 * pad)
        header_reserved = 0
        if self.header_action is not None:
            face = design.font(12)
            label = (self.header_action.compact_label or self.header_action.label) if compact else self.header_action.label
            button_width = min(content_width // 2, face.size(label)[0] + 16)
            button = pygame.Rect(close.left - button_width - 6, close.top, button_width, 32)
            self.hit[self.header_action.name] = button
            shapes.rounded_rect(surface, look.select_fill, button, border_radius=4)
            shapes.rounded_rect(surface, look.hud_accent, button, 1, border_radius=4)
            image = face.render(label, True, look.ink_1)
            surface.blit(image, image.get_rect(center=button.center))
            header_reserved = button_width + 12
        title_face = design.font(20 if compact else 24 if small else (36 if look.key == "hifi" else 30), bold=True)
        body_face = design.reading_font(15 if small else 17)
        caption_face = design.reading_font(12 if small else 13)
        y = rect.top + (16 if small else 24)
        for line in wrap(self.title.upper() if look.key == "stage" else self.title,
                         title_face, max(35, content_width - 32 - header_reserved)):
            image = title_face.render(line, True, look.hud_accent if look.key == "stage" else look.ink_1)
            surface.blit(image, (left, y))
            y += title_face.get_linesize()
        subtitle_lines = wrap(self.subtitle, caption_face, content_width)
        subtitle_in_body = compact or len(subtitle_lines) > 2
        if not subtitle_in_body:
            for line in subtitle_lines:
                surface.blit(caption_face.render(line, True, look.ink_3), (left, y + 3))
                y += caption_face.get_linesize()
        top = y + (8 if compact else 16)

        # Actions stay visible while the content scrolls. Labels wrap inside
        # their own cells, so long translations still have usable targets.
        count = max(1, len(self.actions))
        columns = min(count, max(1, content_width // 115))
        gap = 8
        cell_width = (content_width - gap * (columns - 1)) // columns
        action_face = design.font(12 if compact else 14 if small else 16, bold=True)
        action_lines = [wrap(action.label, action_face, max(24, cell_width - 16)) for action in self.actions]
        cell_height = max(32 if compact else 38, max((len(lines) for lines in action_lines), default=1)
                          * action_face.get_linesize() + (10 if compact else 16))
        rows = (len(self.actions) + columns - 1) // columns
        footer_height = rows * cell_height + max(0, rows - 1) * gap
        footer_y = rect.bottom - pad - footer_height
        self.body = pygame.Rect(left, top, content_width, max(8, footer_y - top - 10))
        pygame.draw.line(surface, look.hairline, (left, self.body.top - 5),
                         (left + content_width, self.body.top - 5))

        # Layout is look-specific, while every explanatory line remains in
        # the scrollable body. On small windows the subtitle joins it too.
        operations = []
        section_face = design.font(16 if small else 21, bold=True)
        sections = list(self.sections)
        if subtitle_in_body and self.subtitle:
            sections.insert(0, ("", [self.subtitle]))
        wide = content_width >= 740 and self.body.height >= 280

        def text_block(text, face, colour, x, offset, available):
            for line in wrap(text, face, max(24, available)):
                operations.append(("text", (x, offset, line, face, colour)))
                offset += face.get_linesize() + 3
            return offset

        def section_blocks(items, x, offset, available):
            for heading, paragraphs in items:
                if heading:
                    offset = text_block(heading.upper() if look.key == "stage" else heading,
                                        section_face, look.hud_accent if look.key == "stage" else look.ink_1,
                                        x, offset, available)
                    offset += 4
                for paragraph in paragraphs:
                    offset = text_block(paragraph, body_face, look.ink_2, x, offset, available)
                    offset += 8
                offset += 14
            return offset

        def metric_blocks(x, offset, available, vertical=False):
            count = len(self.metrics)
            if not count:
                return offset
            gap = 14
            metric_width = available if vertical else (available - gap * (count - 1)) // count
            heights = []
            for index, (label, value, detail) in enumerate(self.metrics):
                mx = x if vertical else x + index * (metric_width + gap)
                my = offset if not vertical else offset + sum(heights) + index * gap
                inset = 12
                value_size = 28 if small else (64 if look.key == "stage" else 46)
                value_face = design.font(value_size, bold=True)
                # Keep complete values (including "28 / 28") inside their cells.
                while value_face.size(value)[0] > metric_width - 2 * inset and value_size > 18:
                    value_size -= 2
                    value_face = design.font(value_size, bold=True)
                cursor = my + inset
                cursor = text_block(value, value_face, look.hud_accent if look.key == "stage" else look.ink_1,
                                    mx + inset, cursor, metric_width - 2 * inset)
                cursor = text_block(label, caption_face, look.ink_2,
                                    mx + inset, cursor, metric_width - 2 * inset)
                if detail:
                    cursor = text_block(detail, caption_face, look.ink_3,
                                        mx + inset, cursor + 5, metric_width - 2 * inset)
                metric_height = cursor - my + inset
                operations.insert(0, ("metric", (mx, my, metric_width, metric_height)))
                heights.append(metric_height)
            return offset + (sum(heights) + (count - 1) * gap if vertical else max(heights)) + 20

        available = content_width - 26
        if wide and look.key == "hifi" and self.metrics:
            # Instrument-panel arrangement: quiet measured values at left,
            # next practice passage and advice in a separate reading column.
            split = min(270, available // 3)
            end_left = metric_blocks(left, 0, split, vertical=True)
            end_right = section_blocks(sections, left + split + 34, 0, available - split - 34)
            content_height = max(end_left, end_right)
        elif wide and self.kind == "help":
            # Studio has two equal reference cards; Stage uses a broad controls
            # column; Hi-Fi keeps the lane reference as a restrained sidebar.
            fraction = {"studio": .5, "stage": .40, "hifi": .34}.get(look.key, .5)
            split = int((available - 30) * fraction)
            pivot = 2 if look.key == "studio" else 1
            end_left = section_blocks(sections[:pivot], left + 10, 10, split - 20)
            end_right = section_blocks(sections[pivot:], left + split + 30, 10, available - split - 30)
            if look.key == "studio":
                operations.insert(0, ("metric", (left, 0, split, end_left + 8)))
            content_height = max(end_left, end_right)
        else:
            after_metrics = metric_blocks(left, 0, available, vertical=available < 340)
            content_height = section_blocks(sections, left, after_metrics, available)

        self.max_scroll = max(0, content_height - self.body.height)
        self.scroll = max(0, min(self.scroll, self.max_scroll))
        old_clip = surface.get_clip()
        surface.set_clip(self.body.clip(old_clip))
        for kind, args in operations:
            if kind == "text":
                x, offset, line, face, colour = args
                y = self.body.top + offset - self.scroll
                if line and y + face.get_linesize() >= self.body.top and y < self.body.bottom:
                    surface.blit(face.render(line, True, colour), (x, y))
            else:
                x, offset, mw, mh = args
                cell = pygame.Rect(x, self.body.top + offset - self.scroll, mw, mh)
                if look.key == "studio":
                    shapes.rounded_rect(surface, look.select_fill, cell, border_radius=12)
                    shapes.rounded_rect(surface, look.hairline, cell, 1, border_radius=12)
                elif look.key == "stage":
                    pygame.draw.line(surface, look.hud_accent, cell.topleft, cell.bottomleft, 3)
                    pygame.draw.line(surface, look.hairline_loud, cell.bottomleft, cell.bottomright, 1)
                else:
                    pygame.draw.line(surface, look.hairline_loud, cell.bottomleft, cell.bottomright, 1)
                    for n in range(11):
                        tx = cell.left + 12 + round((cell.width - 24) * n / 10)
                        pygame.draw.line(surface, look.hairline, (tx, cell.bottom - 5),
                                         (tx, cell.bottom - (10 if n % 5 else 15)), 1)
        surface.set_clip(old_clip)
        if self.max_scroll:
            rail = pygame.Rect(self.body.right - 8, self.body.top + 22, 4, max(4, self.body.height - 44))
            pygame.draw.rect(surface, look.hairline, rail)
            thumb_height = max(8, round(rail.height * self.body.height / max(1, content_height)))
            thumb = pygame.Rect(rail.x, rail.y + round((rail.height - thumb_height) * self.scroll / self.max_scroll),
                                rail.width, thumb_height)
            pygame.draw.rect(surface, look.hud_accent, thumb)
            for name, cy, delta in (("scroll_up", self.body.top + 8, -1),
                                    ("scroll_down", self.body.bottom - 8, 1)):
                hit = pygame.Rect(self.body.right - 23, cy - 8, 23, 16)
                self.hit[name] = hit
                shapes.polygon(surface, look.ink_2, [(hit.centerx - 5, cy - delta * 3),
                               (hit.centerx + 5, cy - delta * 3), (hit.centerx, cy + delta * 3)])

        for index, action in enumerate(self.actions):
            button = pygame.Rect(left + (index % columns) * (cell_width + gap),
                                 footer_y + (index // columns) * (cell_height + gap), cell_width, cell_height)
            self.hit[action.name] = button
            active = index == self.focus
            fill = look.hud_accent if active else look.select_fill
            ink = design.on_accent(fill) if active else look.ink_1
            radius = 10 if look.key == "studio" else 2
            shapes.rounded_rect(surface, fill, button, border_radius=radius)
            if active or look.key == "stage":
                shapes.rounded_rect(surface, look.hud_accent, button, 2, border_radius=radius)
            text_y = button.centery - len(action_lines[index]) * action_face.get_linesize() // 2
            for line in action_lines[index]:
                image = action_face.render(line, True, ink)
                surface.blit(image, (button.centerx - image.get_width() // 2, text_y))
                text_y += action_face.get_linesize()
