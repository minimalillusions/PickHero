"""A small musical choice before the optional technical curriculum."""
from __future__ import annotations

from pathlib import Path
import time

import pygame

from pickhero.audio.midi_playback import MidiPlayer
from pickhero.config import log_error
from pickhero.i18n import t
from pickhero.library import content_identity
from pickhero.music import (MUSIC_DIRNAME, PIECES, VERSIONS, build_music, build_music_demo,
                            ensure_music, music_for_path, music_practice)
from pickhero.progress import song_key
from pickhero.ui import design, shapes
from pickhero.ui.colors import get_look
from pickhero.ui.practice_panels import Action, PracticePanel, wrap
from pickhero.ui.test_recording import RecordingRequest


class MusicScreen:
    def __init__(self, config, progress, songs_dir):
        self.instrument = config.get_instrument()
        self.folder = Path(songs_dir)/MUSIC_DIRNAME
        self.selected = 0
        self.version = "start"
        self.generated = False
        self.error = False
        self._player = None
        self._demo = None
        self._demo_started = 0.
        self._status = "idle"
        self._scroll = 0
        self._maximum = 0
        self._reveal = True
        self._layout = None
        self._records = {}
        self._last_click = (None, 0.)
        try:
            self.generated = ensure_music(self.instrument, songs_dir, progress)
            for piece in PIECES:
                for version in VERSIONS:
                    path = self.folder/piece.filename(version)
                    self._records[piece.slug, version] = progress.get_best(song_key(
                        path, self.instrument.key, content_id=content_identity(path)))
        except (OSError, ValueError) as exc:
            self.error = True
            log_error(f"Original music could not be prepared: {exc}")
        saved = progress.latest_session(self.instrument.key)
        if saved:
            self.show_path(saved.get("song_path", ""))
        self._refresh()

    @property
    def path(self):
        return self.folder/PIECES[self.selected].filename(self.version)

    def show_path(self, path):
        found = music_for_path(path)
        if found is None or Path(path).resolve().parent != self.folder.resolve():
            return False
        self.close()
        self.selected = PIECES.index(found[0])
        self.version = found[1]
        self._reveal = True
        self._status = "idle"
        self._refresh()
        return True

    def _refresh(self):
        next_version = VERSIONS[1-VERSIONS.index(self.version)]
        self.panel = PracticePanel("music", t("music.title"),
                                  self.instrument.short_name + ' · ' + t(f"music.version.{self.version}"), [], [
            Action("play", t("music.play"), pygame.K_p),
            Action("demo", t("lesson.step_stop") if self._player else
                   t("music.no_output") if self._status == "unavailable" else t("music.listen"), pygame.K_d),
            Action("version", t("music.change_version", version=t(f"music.version.{next_version}")), pygame.K_v),
            Action("technique", t("music.technique"), pygame.K_t),
        ])
        if self.error:
            self.panel.sections = [("", [t("music.files_error")])]
            self.panel.actions = [Action("technique", t("music.technique"), pygame.K_t),
                                  Action("back", t("lesson.catalog_library"))]
        else:
            self.panel.header_action = Action("record", t("record.header"), pygame.K_u, "U")

    def handle_event(self, event):
        if not self.error:
            if event.type == pygame.MOUSEWHEEL:
                self._scroll = max(0, min(self._maximum, self._scroll-event.y*48))
                return None
            if event.type == pygame.KEYDOWN:
                if event.key in (pygame.K_UP, pygame.K_DOWN, pygame.K_HOME, pygame.K_END):
                    selected = (0 if event.key == pygame.K_HOME else len(PIECES)-1 if event.key == pygame.K_END
                                else max(0, min(len(PIECES)-1, self.selected+(-1 if event.key == pygame.K_UP else 1))))
                    self._select(selected)
                    return None
                if event.key in (pygame.K_PAGEUP, pygame.K_PAGEDOWN):
                    self._scroll = max(0, min(self._maximum, self._scroll+
                        (-1 if event.key == pygame.K_PAGEUP else 1)*max(40, self.panel.body.height//2)))
                    return None
        action = self.panel.handle_event(event)
        if action in ("close", "back", "technique"):
            self.close()
            self._status = "idle"
            self._last_click = (None, 0.)
            self._refresh()
            return "technique" if action == "technique" else "back"
        if action is None or self.error:
            return None
        if not action.startswith("piece:"):
            self._last_click = (None, 0.)
        if action.startswith("piece:"):
            selected = int(action.split(":")[1])
            previous, at = self._last_click
            now = time.monotonic()
            self._select(selected)
            self._last_click = selected, now
            if previous == selected and 0 <= now-at < .4:
                self.close()
                return music_practice(self.path, self.instrument)
        elif action in ("up", "down"):
            self._scroll = max(0, min(self._maximum, self._scroll+(-48 if action == "up" else 48)))
        elif action == "version":
            self.close()
            self.version = VERSIONS[1-VERSIONS.index(self.version)]
            self._status = "idle"
            self._reveal = True
            self._refresh()
        elif action in ("play", "record"):
            self.close()
            practice = music_practice(self.path, self.instrument)
            return RecordingRequest(self.path, practice) if action == "record" else practice
        elif action == "demo":
            if self._player:
                self.close()
                self._status = "idle"
            else:
                demo = build_music_demo(PIECES[self.selected], self.version, self.instrument)
                player = MidiPlayer(demo.track)
                if player.open():
                    self._player, self._demo = player, demo
                    self._demo_started = time.monotonic()
                    self._status = "playing"
                    player.update(0.)
                else:
                    player.close()
                    self._status = "unavailable"
            self._refresh()
        return None

    def _select(self, selected):
        if selected != self.selected:
            self.close()
            self.selected = selected
            self._status = "idle"
        self._reveal = True
        self._refresh()

    def update(self, dt=0.):
        if self._player is not None:
            elapsed = (time.monotonic()-self._demo_started)*1000
            self._player.update(elapsed)
            if elapsed >= self._demo.duration_ms:
                self.close()
                self._status = "done"
                self._refresh()

    def close(self):
        if self._player is not None:
            self._player.close()
        self._player = self._demo = None

    def render(self, surface):
        look = get_look()
        surface.fill(look.bg)
        self.panel.render(surface)
        if self.error:
            return
        body = self.panel.body
        compact = body.width < 480
        title_face = design.font(18 if compact else 23, bold=True)
        face = design.reading_font(14 if compact else 16)
        small = design.reading_font(12 if compact else 14)
        width = max(40, body.width-44)
        cards = []
        for piece in PIECES:
            ex = build_music(piece, self.version, self.instrument)
            strings = {step.string for bar in ex.bars for step in bar if step.string is not None}
            frets = [step.fret for bar in ex.bars for step in bar if step.string is not None]
            requirements = t("music.requirements_one" if len(strings) == 1 else "music.requirements",
                style=t(f"music.style.{piece.style}"),
                strings=len(strings), low=min(frets), high=max(frets), seconds=round(len(ex.bars)*240/piece.bpm))
            lines = wrap(requirements, small, width)
            cards.append((piece, lines, title_face.get_linesize()+len(lines)*small.get_linesize()+24))
        piece = PIECES[self.selected]
        record = self._records.get((piece.slug, self.version))
        status = t("music.fresh") if not record else t("music.history", attempts=record.attempts,
                                                         accuracy=round(record.best_accuracy))
        details = [t(f"music.{piece.slug}.{self.version}"),
                   *[t(f"music.{piece.slug}.section.{bar}") for bar in piece.section_bars],
                   t("music.practice_hint"),
                   t(f"music.demo_{self._status}"), status]
        details_blocks = [wrap(text, face, width) for text in details]
        cards_height = sum(row[2]+8 for row in cards)
        detail_height = sum(len(block)*face.get_linesize()+9 for block in details_blocks)
        self._maximum = max(0, cards_height+20+detail_height-body.height)
        if self._layout != body.size:
            self._layout = body.size
            self._reveal = True
        if self._reveal:
            top = sum(row[2]+8 for row in cards[:self.selected])
            bottom = top+cards[self.selected][2]
            if top < self._scroll or cards[self.selected][2] >= body.height:
                self._scroll = top
            elif bottom > self._scroll+body.height:
                self._scroll = bottom-body.height
            self._reveal = False
        self._scroll = max(0, min(self._maximum, self._scroll))
        old_clip = surface.get_clip()
        surface.set_clip(old_clip.clip(body))
        y = body.top-self._scroll
        for index, (piece, lines, height) in enumerate(cards):
            rect = pygame.Rect(body.left, y, body.width-20, height)
            hit = rect.clip(body)
            if hit.height:
                self.panel.hit[f"piece:{index}"] = hit
            if index == self.selected:
                radius = 9 if look.key == "studio" else 0
                shapes.rounded_rect(surface, look.select_fill, rect, border_radius=radius)
                shapes.rounded_rect(surface, look.hud_accent, rect, 1, border_radius=radius)
            else:
                pygame.draw.line(surface, look.hairline, rect.bottomleft, rect.bottomright)
            surface.blit(title_face.render(piece.title, True, look.ink_1), (rect.left+12, y+8))
            text_y = y+12+title_face.get_linesize()
            for line in lines:
                surface.blit(small.render(line, True, look.ink_2), (rect.left+12, text_y))
                text_y += small.get_linesize()
            y += height+8
        y += 10
        for block in details_blocks:
            for line in block:
                surface.blit(face.render(line, True, look.ink_2), (body.left+12, y))
                y += face.get_linesize()
            y += 9
        surface.set_clip(old_clip)
        if self._maximum:
            for name, cy, direction in (("up", body.top+8, -1), ("down", body.bottom-8, 1)):
                hit = pygame.Rect(body.right-18, cy-8, 18, 16)
                self.panel.hit[name] = hit
                shapes.polygon(surface, look.ink_2, [(hit.centerx-4, cy-direction*3),
                    (hit.centerx+4, cy-direction*3), (hit.centerx, cy+direction*3)])
            rail = pygame.Rect(body.right-9, body.top+21, 3, max(4, body.height-42))
            pygame.draw.rect(surface, look.hairline_loud, rail)
            thumb_h = max(6, round(rail.height*body.height/(body.height+self._maximum)))
            pygame.draw.rect(surface, look.hud_accent, (rail.x,
                rail.y+round((rail.height-thumb_h)*self._scroll/self._maximum), 3, thumb_h))
