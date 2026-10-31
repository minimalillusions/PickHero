"""Responsive setup pages, with shared input rules and three visual compositions."""
from __future__ import annotations

import math

import pygame

from pickhero.i18n import t
from pickhero.ui import design, shapes
from pickhero.ui.colors import get_look
from pickhero.ui.text_layout import wrap


def _font(size, *, bold=False):
    return (design.reading_font(size, bold=bold) if size <= 18
            else design.font(size, bold=bold))


def fitted(text: str, width: int, size: int, *, bold: bool = False) -> str:
    face = _font(size, bold=bold)
    text = str(text)[:2000]
    if face.size(text)[0] <= width:
        return text
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if face.size(text[:mid] + "…")[0] <= width:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo] + "…" if width > face.size("…")[0] else ""


def text(surface, value, rect, *, size=17, bold=False, colour=None, align="left"):
    """Fit one line to its own rectangle, including at narrow window sizes."""
    rect = pygame.Rect(rect)
    look = get_look()
    face = _font(size, bold=bold)
    image = face.render(fitted(value, rect.width, size, bold=bold), True,
                        colour or look.ink_2)
    x = rect.x if align == "left" else rect.centerx - image.get_width() // 2
    surface.blit(image, (x, rect.y + max(0, (rect.height - image.get_height()) // 2)))


def paragraph_height(value, width, *, size=16, line_gap=3):
    """Full measured height; callers reserve this inside their scroll area."""
    face = _font(size)
    return len(wrap(value, face, width)) * (face.get_linesize() + line_gap)


def paragraph(surface, value, rect, *, size=16, colour=None, lines=None, align="left", line_gap=3):
    """Draw complete prose. A line limit is reserved for fixed-size controls."""
    rect = pygame.Rect(rect)
    face = _font(size)
    line_h = face.get_linesize() + line_gap
    wrapped = wrap(value, face, rect.width)
    if lines is not None:
        wrapped = wrapped[:min(lines, max(1, rect.height // line_h))]
    for row, current in enumerate(wrapped):
        text(surface, current, (rect.x, rect.y + row * line_h, rect.width, line_h),
             size=size, colour=colour, align=align)
    return len(wrapped) * line_h


class SetupView:
    """Keyboard focus, a clipped scroll area, and always-visible action buttons."""

    def __init__(self):
        self.scroll = 0
        self.content_height = 0
        self.viewport = pygame.Rect(0, 0, 1, 1)
        self.buttons: dict[str, pygame.Rect] = {}
        self.focus: str | None = None
        self.order: list[str] = []
        self.keyboard = False
        self.intro_height = 0
        self._intro = ""
        self._surface = None

    def begin(self, surface, title, subtitle, actions, *, primary="back", list_focus=False):
        look = get_look()
        width, height = surface.get_size()
        pad = 24 if width >= 700 else 12
        gap, button_h = 7, 32 if height < 360 else 35
        min_button_w = 72 if height < 360 else 118
        columns = max(1, min(len(actions), (width - 2 * pad + gap) // min_button_w))
        rows = math.ceil(len(actions) / columns)
        footer_h = rows * (button_h + gap) + 28
        footer_top = max(54, height - footer_h)
        compact = footer_top < 170
        self._surface = surface
        self._intro = ""
        surface.fill(look.panel_right)
        self.buttons = {}
        self.order = (["devices"] if list_focus else []) + [a[0] for a in actions]
        if self.focus not in self.order:
            self.focus = "devices" if list_focus else primary

        # Hi-Fi uses a quiet title column; Stage a slanted full-width marquee;
        # Studio a compact toolbar above cards. These are different layouts.
        if look.key == "hifi" and width >= 760:
            side = min(280, width // 3)
            surface.fill(look.panel_left, (0, 0, side, footer_top))
            pygame.draw.line(surface, look.hairline_loud, (side, 20), (side, footer_top - 12))
            text(surface, "PickHero", (24, 16 if compact else 26, side - 48, 28), size=17)
            title_y = 52 if compact else 100
            paragraph(surface, title, (24, title_y, side - 48, min(135, footer_top - title_y - 10)), size=24 if compact else 30, lines=3,
                      colour=look.ink_1)
            subtitle_h = paragraph_height(subtitle, side - 48)
            if footer_top >= 300 and subtitle_h <= min(140, footer_top - 260):
                paragraph(surface, subtitle, (24, 248, side - 48, subtitle_h))
            else:
                self._intro = subtitle
            self.viewport = pygame.Rect(side + pad, 24, width - side - pad * 2,
                                        max(1, footer_top - 36))
        elif look.key == "stage":
            head = pygame.Rect(pad, 8 if compact else 12, width - pad * 2, 43 if compact else 76)
            shapes.skewed(surface, head, look.panel_left, ring=look.hud_accent)
            text(surface, title.upper(), (pad + 20, 12 if compact else 18, width - pad * 2 - 40, 32),
                 size=20 if compact else 25, bold=True, colour=look.ink_1)
            if not compact and len(subtitle) <= 60 and _font(14).size(subtitle)[0] <= width - pad * 2 - 40:
                text(surface, subtitle, (pad + 20, 53, width - pad * 2 - 40, 24), size=14)
            else:
                self._intro = subtitle
            content_top = 61 if compact else 102
            self.viewport = pygame.Rect(pad, content_top, width - pad * 2, max(1, footer_top - content_top - 10))
        else:
            text(surface, title, (pad, 8 if compact else 14, width - pad * 2, 36), size=22 if compact else 27,
                 bold=look.key == "studio", colour=look.ink_1)
            if not compact and len(subtitle) <= 60 and _font(14).size(subtitle)[0] <= width - pad * 2:
                text(surface, subtitle, (pad, 53, width - pad * 2, 24), size=14)
            else:
                self._intro = subtitle
            line_y = 48 if compact else 84
            pygame.draw.line(surface, look.hairline, (pad, line_y), (width - pad, line_y))
            content_top = line_y + 12
            self.viewport = pygame.Rect(pad, content_top, width - pad * 2, max(1, footer_top - content_top - 10))

        pygame.draw.line(surface, look.hairline_loud, (pad, footer_top), (width - pad, footer_top))
        button_w = max(1, (width - pad * 2 - gap * (columns - 1)) // columns)
        for index, (name, label) in enumerate(actions):
            row, col = divmod(index, columns)
            rect = pygame.Rect(pad + col * (button_w + gap), footer_top + 8 + row * (button_h + gap),
                               button_w, button_h)
            self.buttons[name] = rect
            focus = self.focus == name and self.keyboard
            self.button(surface, rect, label, primary=name == primary, focus=focus)
        text(surface, t("secondary.navigation"), (pad, height - 22, width - pad * 2, 19),
             size=11, colour=look.ink_3)
        self.intro_height = (paragraph_height(self._intro, self.viewport.width - 28, size=14) + 14
                             if self._intro else 0)
        self.clamp()
        return self.viewport

    def button(self, surface, rect, label, *, primary=False, focus=False):
        look = get_look()
        fill = look.hud_accent if primary else look.panel_left
        ink = design.on_accent(fill) if primary else look.ink_1
        if look.key == "stage":
            shapes.skewed(surface, rect, fill, ring=look.hud_accent if focus else look.hairline_loud,
                          ring_width=2 if focus else 1, lean=.18)
        elif look.key == "hifi":
            shapes.rounded_rect(surface, fill, rect, border_radius=rect.height // 2)
            shapes.rounded_rect(surface, look.ink_1 if focus else look.hairline_loud, rect,
                             width=2 if focus else 1, border_radius=rect.height // 2)
        else:
            shapes.rounded_rect(surface, fill, rect, border_radius=8)
            shapes.rounded_rect(surface, look.hud_accent if focus else look.hairline_loud,
                             rect, width=2 if focus else 1, border_radius=8)
        if _font(14, bold=primary).size(label)[0] > rect.width - 18:
            size = 11 if rect.width < 112 else 12
            if any(_font(size).size(word)[0] > rect.width - 8 for word in label.split()):
                size = 10
            paragraph(surface, label, rect.inflate(-8, -2), size=size, colour=ink,
                      lines=2, align="center", line_gap=0)
        else:
            text(surface, label, rect.inflate(-18, -2), size=14, bold=primary, colour=ink, align="center")

    def clamp(self):
        self.scroll = max(0, min(int(self.scroll), max(0, self.content_height - self.viewport.height)))

    def content(self, height, *, reveal=None):
        self.content_height = max(0, int(height)) + self.intro_height
        if reveal is not None:
            top, bottom = reveal
            top += self.intro_height
            bottom += self.intro_height
            self.scroll = min(self.scroll, top)
            self.scroll = max(self.scroll, bottom - self.viewport.height)
        self.clamp()
        if self._intro and self._surface is not None:
            old_clip = self._surface.get_clip()
            self._surface.set_clip(self.viewport.clip(old_clip))
            paragraph(self._surface, self._intro,
                      (self.viewport.x + 8, self.viewport.y - self.scroll,
                       self.viewport.width - 28, self.intro_height - 14), size=14)
            self._surface.set_clip(old_clip)
        return self.viewport.y + self.intro_height - self.scroll

    def finish(self, surface):
        look = get_look()
        if self.content_height > self.viewport.height:
            track = pygame.Rect(self.viewport.right - 4, self.viewport.y, 4, self.viewport.height)
            shapes.rounded_rect(surface, look.track_empty, track, border_radius=2)
            thumb_h = max(16, int(track.height * track.height / self.content_height))
            thumb_h = min(track.height, thumb_h)
            y = track.y + int((track.height - thumb_h) * self.scroll /
                             max(1, self.content_height - track.height))
            shapes.rounded_rect(surface, look.hud_accent, (track.x, y, 4, thumb_h), border_radius=2)
        if self.focus == "devices" and self.keyboard:
            shapes.rounded_rect(surface, look.hud_accent, self.viewport, 1, border_radius=4)

    def event(self, event, *, scroll_keys=True):
        if event.type == pygame.MOUSEWHEEL:
            self.scroll -= int(event.y) * 48
            self.clamp()
            return "scroll"
        if event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
            self.keyboard = False
            for name, rect in self.buttons.items():
                if rect.collidepoint(event.pos):
                    self.focus = name
                    return name
        if event.type != pygame.KEYDOWN:
            return None
        if event.key == pygame.K_TAB:
            step = -1 if getattr(event, "mod", 0) & pygame.KMOD_SHIFT else 1
            if self.order:
                at = self.order.index(self.focus) if self.focus in self.order else -1
                self.focus = self.order[(at + step) % len(self.order)]
            self.keyboard = True
            return "focus"
        if event.key in (pygame.K_RETURN, pygame.K_SPACE):
            return self.focus
        if scroll_keys and event.key in (pygame.K_PAGEUP, pygame.K_PAGEDOWN, pygame.K_HOME, pygame.K_END):
            if event.key == pygame.K_HOME:
                self.scroll = 0
            elif event.key == pygame.K_END:
                self.scroll = self.content_height
            else:
                self.scroll += self.viewport.height * (-1 if event.key == pygame.K_PAGEUP else 1)
            self.clamp()
            return "scroll"
        return None


def panel(surface, rect, *, accent=False):
    look = get_look()
    if look.key == "stage":
        shapes.skewed(surface, rect, look.panel_left,
                      ring=look.hud_accent if accent else look.hairline_loud, lean=.035)
    elif look.key == "hifi":
        pygame.draw.line(surface, look.hairline_loud, rect.topleft, rect.topright)
    else:
        shapes.rounded_rect(surface, look.panel_left, rect, border_radius=12)
        shapes.rounded_rect(surface, look.hud_accent if accent else look.hairline,
                         rect, width=1, border_radius=12)


def meter(surface, rect, fraction, *, colour=None, target=None):
    look = get_look()
    rect = pygame.Rect(rect)
    fraction = min(1.0, max(0.0, fraction))
    colour = colour or look.hud_accent
    if look.key == "stage":
        # A segmented signal rack belongs beside the highway's neon capsules.
        count = max(4, rect.width // 12)
        step = rect.width / count
        for i in range(count):
            cell = pygame.Rect(rect.x + round(i * step), rect.y, max(2, round(step) - 3), rect.height)
            shapes.rounded_rect(surface, colour if i < fraction * count else look.track_empty, cell)
    else:
        shapes.rounded_rect(surface, look.track_empty, rect, border_radius=rect.height // 2)
        fill = rect.copy(); fill.width = round(rect.width * fraction)
        if fill.width:
            shapes.rounded_rect(surface, colour, fill, border_radius=min(rect.height // 2, fill.width // 2))
    if target is not None:
        x = rect.x + round(rect.width * max(0, min(1, target)))
        pygame.draw.line(surface, look.ink_1, (x, rect.top - 3), (x, rect.bottom + 3), 2)


def dial(surface, rect, cents, *, active, colour=None):
    """Hi-Fi's instrument dial; other looks use a horizontal tuning rail."""
    look = get_look()
    rect = pygame.Rect(rect)
    colour = colour or look.hud_accent
    if look.key == "hifi":
        radius = max(12, min(rect.width // 2 - 8, rect.height - 8))
        centre = (rect.centerx, rect.bottom - 4)
        arc = pygame.Rect(centre[0] - radius, centre[1] - radius, radius * 2, radius * 2)
        pygame.draw.arc(surface, look.hairline_loud, arc, 0, math.pi, 2)
        for off in (-50, -25, 0, 25, 50):
            angle = math.pi / 2 - off / 50 * math.pi / 2
            a = (centre[0] + round(math.cos(angle) * (radius - 5)), centre[1] - round(math.sin(angle) * (radius - 5)))
            b = (centre[0] + round(math.cos(angle) * radius), centre[1] - round(math.sin(angle) * radius))
            pygame.draw.line(surface, look.ink_3, a, b, 1)
        if active:
            angle = math.pi / 2 - max(-50, min(50, cents)) / 50 * math.pi / 2
            tip = (centre[0] + round(math.cos(angle) * (radius - 9)), centre[1] - round(math.sin(angle) * (radius - 9)))
            pygame.draw.line(surface, colour, centre, tip, 2)
        return
    track = pygame.Rect(rect.x + 4, rect.centery - 4, rect.width - 8, 8)
    shapes.rounded_rect(surface, look.track_empty, track, border_radius=4)
    zone = pygame.Rect(track.centerx - max(4, track.width // 10), track.y, max(8, track.width // 5), track.height)
    shapes.rounded_rect(surface, design.mix(colour, look.panel_right, .55), zone, border_radius=3)
    pygame.draw.line(surface, look.ink_3, (track.centerx, rect.y), (track.centerx, rect.bottom), 1)
    if active:
        x = track.centerx + round(max(-50, min(50, cents)) / 100 * track.width)
        pygame.draw.polygon(surface, colour, [(x, rect.y), (x + 5, rect.centery),
                                             (x, rect.bottom), (x - 5, rect.centery)])
