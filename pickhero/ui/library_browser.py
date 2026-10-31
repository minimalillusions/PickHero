"""A common library structure with each look's typography and materials."""
from __future__ import annotations

from functools import lru_cache

import pygame

from pickhero.i18n import t
from pickhero.ui import design, library_setup, secondary, shapes
from pickhero.ui.colors import get_look
from pickhero.ui.library_catalogue import CATEGORIES, CATEGORY_KEYS
from pickhero.ui.secondary import SetupView


def text(surface, value, rect, **options):
    """Reuse measured, antialiased text while its content and style are stable."""
    rect = pygame.Rect(rect)
    if rect.width <= 0 or rect.height <= 0:
        return
    key = ("browser.text", get_look(), str(value), rect.size, tuple(sorted(options.items())))
    layer = design.sprite(key, rect.size, lambda target:
                          secondary.text(target, value, target.get_rect(), **options))
    surface.blit(layer, rect.topleft)


@lru_cache(maxsize=256)
def _paragraph_height(value, width, size, line_gap, look):
    return secondary.paragraph_height(value, width, size=size, line_gap=line_gap)


def paragraph_height(value, width, *, size=16, line_gap=3):
    return _paragraph_height(value, width, size, line_gap, get_look())


def paragraph(surface, value, rect, **options):
    rect = pygame.Rect(rect)
    if rect.width <= 0 or rect.height <= 0:
        return
    # Very long imported descriptions do not become enormous cached images.
    if rect.width * rect.height > 512_000:
        secondary.paragraph(surface, value, rect, **options)
        return
    key = ("browser.prose", get_look(), str(value), rect.size, tuple(sorted(options.items())))
    layer = design.sprite(key, rect.size, lambda target:
                          secondary.paragraph(target, value, target.get_rect(), **options))
    surface.blit(layer, rect.topleft)


def _button(surface, page, name, label, rect, *, primary=False, selected=False):
    rect = pygame.Rect(rect)
    SetupView().button(surface, rect, label, primary=primary, focus=selected)
    page.mark(name, rect)
    return rect


def _rail(surface, page, rect, value, maximum, visible, *, prefix=None):
    look = get_look()
    if rect.height < 50:
        return
    rail = pygame.Rect(rect.right - 5, rect.y + 22, 3, rect.height - 44)
    pygame.draw.rect(surface, look.hairline, rail)
    if maximum:
        size = max(8, round(rail.height * visible / (maximum + visible)))
        y = rail.y + round((rail.height-size)*value/maximum)
        pygame.draw.rect(surface, look.hud_accent, (rail.x, y, 3, size))
    if prefix and maximum:
        for suffix, y, direction in (("up", rect.y, -1), ("down", rect.bottom-20, 1)):
            box = pygame.Rect(rect.right-14, y, 16, 20)
            cx, cy = box.center
            shapes.polygon(surface, look.ink_2, [(cx-4, cy-3*direction),
                           (cx+4, cy-3*direction), (cx, cy+3*direction)])
            page.mark(f"{prefix}.{suffix}", box)


def _rows(surface, screen, page, area):
    look = get_look()
    compact = page.height < 340
    dense = compact and bool(screen._resume_state())
    row_h, pitch = (34, 38) if dense else (46, 50) if compact else (62, 68)
    scroll = page.clamp_scroll(row_h, area.height, pitch)
    old_clip = surface.get_clip()
    surface.set_clip(old_clip.clip(area))
    first = scroll//pitch
    last = min(len(page.rows), (scroll+area.height)//pitch+1)
    for i in range(first, last):
        song = page.rows[i]
        data = screen.catalogue.metadata(song, screen._instrument())
        box = pygame.Rect(area.x, area.y+i*pitch-scroll, area.width-18, row_h)
        selected = i == page.selected
        radius = 9 if look.key == "studio" else 0
        if look.key == "stage":
            shapes.skewed(surface, box, look.select_fill if selected else look.panel_left,
                          ring=look.hud_accent if selected else None, lean=.15)
        elif selected:
            shapes.rounded_rect(surface, look.select_fill, box, border_radius=radius)
            if look.key == "studio":
                shapes.rounded_rect(surface, look.hud_accent, box, width=1, border_radius=radius)
        else:
            pygame.draw.line(surface, look.hairline, box.bottomleft, box.bottomright)
        left = box.x+14
        if area.width >= 450:
            text(surface, f"{data['order'] or i+1:02}", (left, box.y+12, 30, 25), size=16,
                 colour=look.hud_accent if selected else look.ink_3)
            left += 40
        score_w = 64 if area.width >= 370 else 46
        title_w = max(1, box.right-left-score_w-20)
        text(surface, data["title"], (left, box.y+(1 if dense else 5), title_w, 20 if dense else 26),
             size=14 if dense else 16 if compact else 18,
             bold=look.key != "hifi", colour=look.ink_1)
        meta = data["subtitle"]
        if data["version"]:
            meta = f"{t(f'music.version.{data['version']}')} · {meta}"
        text(surface, meta, (left, box.y+(20 if dense else 26 if compact else 34), title_w, 14 if dense else 18),
             size=11 if dense else 12 if compact else 13, colour=look.ink_2)
        score = page.scores.get(i)
        text(surface, t("menu.progress_new") if score is None else f"{round(score)}%",
             (box.right-score_w-12, box.y+(3 if dense else 12), score_w, 24 if dense else 28), size=13 if score is None else 17,
             align="center", colour=look.ink_2 if score is None else look.hud_accent)
        if compact and page.width < 940 and selected:
            _button(surface, page, "details", t("library.resume_details"),
                    (box.right-70, box.y+(3 if dense else 7), 64, 28 if dense else 32))
            page.hit["details"][-1] = page.hit["details"][-1].clip(area)
        hit = box.clip(area)
        if hit.height:
            page.mark_row(hit, i)
    if not page.rows:
        filtered = screen._search_text or screen._filter != ("all", None)
        unavailable = not screen.catalogue.entries(screen._songs) and screen.catalogue.entries(screen._scanned_songs)
        title = page.empty_title if filtered or unavailable else t("browse.empty")
        paragraph(surface, title, area.inflate(-20, -16), colour=look.ink_1, size=18)
        hint = (page.empty_hint if filtered or unavailable else t("music.files_error")
                if screen.catalogue.category == "music" else t("browse.empty_files"))
        y = area.y+20+paragraph_height(title, area.width-20, size=18)
        paragraph(surface, hint, (area.x+10, y, area.width-30, max(1, area.bottom-y)), size=14)
    surface.set_clip(old_clip)
    total = max(0, (len(page.rows)-1)*pitch+row_h)
    _rail(surface, page, area, scroll, max(0, total-area.height), area.height)


def _detail(surface, screen, page, area, *, narrow):
    look = get_look()
    cat = screen.catalogue
    cat.detail_rect = area
    if not page.rows:
        return
    song = page.rows[page.selected]
    data = cat.metadata(song, screen._instrument())
    fill = look.panel_left
    radius = 14 if look.key == "studio" else 0
    shapes.rounded_rect(surface, fill, area, border_radius=radius)
    if look.key == "stage":
        pygame.draw.line(surface, look.hud_accent, area.topleft, area.topright, 2)
    pad = 16 if area.width < 400 else 24
    x, width = area.x+pad, area.width-2*pad
    compact = area.height < 290
    y = area.y+12
    if narrow and area.height >= 160:
        _button(surface, page, "details.back", t("browse.back"), (x, y, min(140, width), 30))
        y += 38
    # Actions are fixed; only the prose scrolls, independently of the list.
    action_h = 32 if compact else 42
    actions_top = area.bottom-pad-action_h
    # Own files too: their preview loads through the import worker first.
    has_preview = data["exercise"] is not None or data["kind"] == "files"
    play_w = (width-8)//2 if has_preview else width
    _button(surface, page, "play", t("browse.play"), (x, actions_top, play_w, action_h), primary=True)
    if has_preview:
        label = (t("lesson.step_stop") if cat._player
                 else t("browse.preview_loading") if cat.status == "loading"
                 else t("music.no_output") if cat.status == "unavailable"
                 else t("browse.preview_failed") if cat.status == "failed"
                 else t("browse.listen"))
        _button(surface, page, "preview", label,
                (x+play_w+8, actions_top, width-play_w-8, action_h))
    body = pygame.Rect(x, y, width, max(1, actions_top-y-12))
    items = [("title", data["title"]), ("meta", data["subtitle"])]
    if data["version"]:
        items.append(("version", ""))
    if data["requirements"]:
        items.append(("requirements", data["requirements"]))
    record = screen._record_for(song)
    status = t("browse.unplayed") if not record or not record.attempts else t(
        "music.history", attempts=record.attempts, accuracy=round(record.best_accuracy))
    items.extend([("status", status), ("description", data["description"])])
    if data["kind"] == "technique":
        items.append(("steps", ""))
    items.append(("expand", ""))
    if cat.expanded:
        items.extend(("instruction", value) for value in data["instructions"])
    if cat.status != "idle":
        items.append(("status", t("browse.preview_playing") if cat.status == "playing"
                      else t(f"music.demo_{cat.status}")))
    sizes = {"title": 24 if compact else 29, "meta": 14, "requirements": 15,
             "status": 14, "description": 16, "instruction": 15}
    measured = []
    for kind, value in items:
        height = (36 if kind in ("version", "steps", "expand") else
                  paragraph_height(value, width-18, size=sizes[kind]))
        measured.append((kind, value, height))
    cat.detail_maximum = max(0, sum(h+12 for _, _, h in measured)-body.height)
    cat.detail_scroll = max(0, min(cat.detail_maximum, cat.detail_scroll))
    old_clip = surface.get_clip()
    surface.set_clip(old_clip.clip(body))
    y = body.y-cat.detail_scroll
    for kind, value, height in measured:
        box = pygame.Rect(x, y, width-18, height)
        if kind == "version":
            available = {row.file.name for row in screen._songs}
            for i, version in enumerate(("start", "groove")):
                if data["piece"].filename(version) not in available:
                    continue
                rect = pygame.Rect(x+i*((width-26)//2+8), y, (width-26)//2, height)
                _button(surface, page, f"version.{version}", t(f"music.version.{version}"), rect,
                        selected=version == data["version"])
        elif kind in ("steps", "expand"):
            name = "lesson_steps" if kind == "steps" else "instructions"
            label = t("browse.steps") if kind == "steps" else t("browse.less") if cat.expanded else t("browse.more")
            _button(surface, page, name, label, box)
        else:
            paragraph(surface, value, box, size=sizes[kind],
                      colour=look.ink_1 if kind in ("title", "description") else look.ink_2)
        y += height+12
    surface.set_clip(old_clip)
    # Clip action hits as well as pixels: hidden content must not activate.
    for name in ("version.start", "version.groove", "lesson_steps", "instructions"):
        if name in page.hit:
            page.hit[name] = [r.clip(body) for r in page.hit[name] if r.colliderect(body)]
    _rail(surface, page, body, cat.detail_scroll, cat.detail_maximum, body.height, prefix="details")


def _filters(surface, screen, page):
    anchor = page.hit.get("filters", [None])[0]
    if anchor is None:
        return
    cat, look = screen.catalogue, get_look()
    options = [(label, value, count) for label, value, count, _ in page.filters
               if value[0] in ("all", "level", "folder")]
    width = min(330, page.width-24)
    card = pygame.Rect(max(12, anchor.right-width), anchor.bottom+6, width,
                       min(len(options)*36+16, max(54, page.height-anchor.bottom-70)))
    for name in list(page.hit):
        page.hit[name] = [rect for rect in page.hit[name] if not rect.colliderect(card)]
    page.row_hit[:] = [(rect, i) for rect, i in page.row_hit if not rect.colliderect(card)]
    shapes.popover(surface, card, fill=look.popover, edge=look.hairline_loud,
                   radius=10 if look.key == "studio" else 0)
    page.mark("filters.card", card)
    body = card.inflate(-16, -16)
    cat.filter_maximum = max(0, len(options)*36-body.height)
    cat.filter_scroll = max(0, min(cat.filter_maximum, cat.filter_scroll))
    old = surface.get_clip()
    surface.set_clip(old.clip(body))
    for i, (label, value, count) in enumerate(options):
        box = pygame.Rect(body.x, body.y+i*36-cat.filter_scroll, body.width-18, 34)
        if box.colliderect(body):
            name = f"filter.{value[0]}.{value[1]}"
            _button(surface, page, name, f"{label}  {count}", box, selected=screen._filter == value)
            page.hit[name][-1] = box.clip(body)
    surface.set_clip(old)
    _rail(surface, page, body, cat.filter_scroll, cat.filter_maximum, body.height, prefix="filters")


def render(surface, screen):
    look = get_look()
    width, height = surface.get_size()
    tiny = height < 380
    pad = 24 if width >= 700 else 12
    # Reuse the complete settings and scan/resume contracts of the browser.
    page = screen._page(surface)
    screen._side_rects.clear()
    surface.fill(look.panel_right)
    screen.catalogue.detail_rect = None
    setup = library_setup.button_rect(width)
    detail_open = width < 940 and screen.catalogue.detail_open and bool(page.rows)
    if tiny and detail_open:
        _button(surface, page, "details.back", t("browse.back"), (pad, 16, max(1, setup.x-pad-8), 36))
    elif tiny:
        _button(surface, page, "search", page.search_text+("_" if page.search_active else "") or t("browse.search"),
                (pad, 16, max(1, setup.x-pad-8), 36), selected=page.search_active)
    else:
        text(surface, "PICKHERO" if look.key == "stage" else "PickHero",
             (pad, 14, max(1, setup.x-pad-12), 38), size=18 if width < 400 else 24,
             bold=look.key != "hifi", colour=look.ink_1)
    if width >= 900:
        text(surface, page.instrument_short, (pad+170, 20, width-620, 28), size=14)
    tabs_y, tabs_h = 62, 32 if tiny else 40
    tab_width = (width-2*pad-16)//3
    for i, key in enumerate(CATEGORIES):
        if tiny and detail_open:
            break
        label = t(CATEGORY_KEYS[key])
        count = len(screen.catalogue.entries(screen._songs, key))
        if width >= 600:
            label += f"  {count}"
        _button(surface, page, f"category.{key}", label,
                (pad+i*(tab_width+8), tabs_y, tab_width, tabs_h),
                selected=(screen.catalogue.category or "music") == key)
    state = screen._resume_state()
    footer_h = (44 if tiny else 54) + (30 if state else 0)
    bottom = height-footer_h-10
    if screen._scan_status:
        bottom -= 22
    top = 58 if tiny and detail_open else tabs_y+tabs_h+14
    narrow = width < 940
    if detail_open:
        _detail(surface, screen, page, pygame.Rect(pad, top, width-2*pad, max(1, bottom-top)), narrow=True)
    else:
        list_w = width-2*pad if narrow else round((width-2*pad)*.56)
        search_w = max(1, list_w-(140 if list_w >= 470 else 90)-8)
        if not tiny:
            _button(surface, page, "search", page.search_text+("_" if page.search_active else "") or t("menu.search_placeholder"),
                    (pad, top, search_w, 30), selected=page.search_active)
            _button(surface, page, "sort", page.sort_label,
                    (pad+search_w+8, top, list_w-search_w-8, 30))
        y = top if tiny else top+38
        if not tiny:
            options = [(label, value, count, active) for label, value, count, active in page.filters
                       if value[0] in ("all", "progress")]
            chip_w = (list_w-4*6)//5
            for i, (label, value, count, active) in enumerate(options):
                if value[0] == "all":
                    label = t("browse.all")
                _button(surface, page, f"filter.{value[0]}.{value[1]}", label,
                        (pad+i*(chip_w+6), y, chip_w, 30), selected=active)
            _button(surface, page, "filters", t("browse.filter") if screen._filter[0] in ("all", "progress") else screen._filter_name(),
                    (pad+4*(chip_w+6), y, list_w-4*(chip_w+6), 30),
                    selected=screen._filter[0] not in ("all", "progress"))
            y += 42
        list_bottom = bottom-(40 if narrow and page.rows and not tiny else 0)
        _rows(surface, screen, page, pygame.Rect(pad, y, list_w, max(1, list_bottom-y)))
        if narrow and page.rows and not tiny:
            song = page.rows[page.selected]
            title = screen.catalogue.metadata(song, screen._instrument())["title"]
            _button(surface, page, "details", t("browse.details", title=title),
                    (pad, bottom-34, list_w, 34), primary=True)
        if not narrow:
            _detail(surface, screen, page,
                    pygame.Rect(pad+list_w+24, top, width-2*pad-list_w-24, bottom-top), narrow=False)
    footer_y = height-footer_h
    pygame.draw.line(surface, look.hairline, (pad, footer_y), (width-pad, footer_y))
    actions = []
    if state:
        from pathlib import Path
        path = Path(state["song_path"])
        song = next((row for row in screen._songs if row.file == path), None)
        title = screen.catalogue.metadata(song, screen._instrument())["title"] if song else path.stem
        actions.append(("resume", t("browse.resume", title=title)))
    else:
        actions.append(("lessons", t("browse.curriculum")))
    if width >= 600:
        actions.append(("wizard", t("passage.guide_short")))
    actions.append(("record", t("record.header")))
    actions.append(("cancel_scan", t("import.cancel_scan")) if screen._scan_job else ("rescan", t("import.rescan")))
    # Resume gets space for the actual title; utilities stay secondary.
    remaining = width-2*pad
    secondary_w = 155 if width >= 1000 else 140 if width >= 600 else 90
    first_w = remaining-(len(actions)-1)*(secondary_w+8)
    x = pad
    for i, (name, label) in enumerate(actions):
        bw = first_w if i == 0 else secondary_w
        _button(surface, page, name, label, (x, footer_y+8, bw, 30), primary=bool(state) and i == 0)
        x += bw+8
    if state:
        from pickhero.ui.import_menu import resume_description
        descriptions = resume_description(state)
        details_w = 174 if width >= 900 else 90
        _button(surface, page, "resume_details", t("library.resume_details_key") if width >= 900 else t("library.resume_details"),
                (width-pad-details_w, height-32, details_w, 26))
        text(surface, f"{title} · {descriptions[0]} · {descriptions[2]}",
             (pad, height-32, width-2*pad-details_w-12, 26), size=14)
    if screen._scan_status and not screen._setup_open:
        text(surface, screen._scan_status, (pad, footer_y-22, width-2*pad, 20), size=13)
    library_setup.button(surface, page)
    if screen.catalogue.filters_open and not page.setup_open:
        _filters(surface, screen, page)
    if page.setup_open:
        library_setup.panel(surface, page)
    screen._last_page = page
    screen._scroll_px = page.scroll_px
    screen._setup_scroll = page.setup_scroll
    screen._button_rects = page.hit
    screen._row_rects = page.row_hit
    if screen._resume_panel is not None:
        screen._resume_panel.render(surface)
