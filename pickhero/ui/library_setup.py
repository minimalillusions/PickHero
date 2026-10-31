"""Library settings keep their position and order when the look changes."""
from __future__ import annotations

import pygame

from pickhero.i18n import available_languages, current_language, t
from pickhero.ui import shapes
from pickhero.ui.colors import get_look, look_name_key
from pickhero.ui.secondary import SetupView, text


def button_rect(width):
    pad = 24 if width >= 700 else 12
    return pygame.Rect(width - pad - min(190, width - 2 * pad), 16,
                       min(190, width - 2 * pad), 36)


def button(surface, page):
    rect = button_rect(page.width)
    SetupView().button(surface, rect, f"{t('menu.setup')} · {t('key.s')}",
                       focus=page.setup_open)
    page.mark("setup", rect)
    return rect


def language_name():
    """The active language, named in itself: "Deutsch", "English".

    In itself because the person who most needs this row is the one who
    cannot read the language the app is currently in.
    """
    code = current_language()
    return dict(available_languages()).get(code, code)


def setup_rows(page):
    look = get_look()
    return [
        ("instrument", t("menu.instrument"),
         f"{page.instrument_short or page.instrument_name} · {page.tuning_label}", "I"),
        ("device", t("menu.device"), page.device_name, "D"),
        ("check", t("menu.check"),
         t("menu.mic_ready") if page.device_ready else t("menu.mic_missing"), "C"),
        ("calibrate", t("menu.tuner"), "A = 440 Hz", "G"),
        ("theme", t("menu.theme"), t(look_name_key(look.key)), "T"),
        ("language", t("menu.language"), language_name(), "L"),
        ("wizard", t("setup.guide"), "", "F2"),
        ("timing", t("timing.title"), "", ""),
        ("sort", page.sort_label or t("menu.sort"), "", "N"),
        ("folder", t("menu.folder"), "", "O"),
    ]


def panel(surface, page):
    """Draw one shared, scrollable property panel in the current visual style."""
    look = get_look()
    anchor = button_rect(page.width)
    width = min(420, page.width - 2 * (page.width - anchor.right))
    rows = setup_rows(page)
    row_h, head_h = 52, 42
    height = min(head_h + len(rows) * row_h + 12,
                 max(64, page.height - anchor.bottom - 20))
    card = pygame.Rect(anchor.right - width, anchor.bottom + 8, width, height)
    # Withdraw covered controls before adding panel controls. Even controls
    # entirely inside the card must not receive a click through its background.
    for name in list(page.hit):
        page.hit[name] = [r for r in page.hit[name] if not r.colliderect(card)]
        if not page.hit[name]:
            del page.hit[name]
    page.row_hit[:] = [(r, i) for r, i in page.row_hit if not r.colliderect(card)]
    page.mark("setup.card", card)
    radius = 12 if look.key == "studio" else 2
    shapes.popover(surface, card, fill=look.popover,
                   edge=look.hud_accent if look.key == "stage" else look.hairline_loud,
                   radius=radius)
    text(surface, t("menu.setup"), (card.x + 16, card.y + 8, card.width - 112, 26),
         size=18, bold=look.key != "hifi", colour=look.ink_1)
    close = pygame.Rect(card.right - 90, card.y + 8, 74, 26)
    text(surface, "Esc · ×", close, size=13, align="center")
    page.mark("setup", close)
    body = pygame.Rect(card.x + 10, card.y + head_h, card.width - 34,
                       max(1, card.height - head_h - 10))
    page.setup_maximum = max(0, len(rows) * row_h - body.height)
    page.setup_scroll = min(max(0, page.setup_scroll), page.setup_maximum)
    previous = surface.get_clip()
    surface.set_clip(previous.clip(body))
    for index, (name, label, value, key) in enumerate(rows):
        row = pygame.Rect(body.x, body.y + index * row_h - page.setup_scroll,
                          body.width, row_h)
        visible = row.clip(body)
        if not visible.height:
            continue
        pygame.draw.line(surface, look.hairline, row.topleft, row.topright)
        text(surface, label, (row.x + 6, row.y + (5 if value else 13), row.width - 42, 24),
             size=14 if value else 15, colour=look.ink_2 if value else look.ink_1)
        if value:
            text(surface, value, (row.x + 6, row.y + 27, row.width - 42, 21),
                 size=15, colour=look.ink_1)
        text(surface, key, (row.right - 30, row.y + 13, 24, 24), size=12, align="center")
        page.mark(name, visible)
    surface.set_clip(previous)
    if page.setup_maximum:
        for name, y, direction in (("setup.up", body.top, -1),
                                   ("setup.down", body.bottom - 22, 1)):
            rect = pygame.Rect(card.right - 24, y, 20, 22)
            cx, cy = rect.center
            shapes.polygon(surface, look.hud_accent,
                           [(cx - 4, cy - direction * 3), (cx + 4, cy - direction * 3),
                            (cx, cy + direction * 3)])
            page.mark(name, rect)
        rail = pygame.Rect(card.right - 16, body.top + 25, 3, max(1, body.height - 50))
        pygame.draw.rect(surface, look.hairline_loud, rail)
        thumb = max(6, round(rail.height * body.height / (len(rows) * row_h)))
        offset = round((rail.height - thumb) * page.setup_scroll / page.setup_maximum)
        pygame.draw.rect(surface, look.hud_accent, (rail.x, rail.y + offset, 3, thumb))
